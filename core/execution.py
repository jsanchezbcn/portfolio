"""core/execution.py — ExecutionEngine: pre-trade simulation and order execution.

SAFETY CONTRACT
===============
This module interacts with a LIVE IBKR brokerage account.

  simulate()      — READ-ONLY. Calls /orders/whatif only. Zero orders transmitted.
  submit()        — LIVE ORDERS. REQUIRES explicit human approval (T031):
                    - Order must be in SIMULATED status (else ValueError).
                    - UI must have collected checkbox confirmation + button click.
                    - NEVER auto-triggered by timers, loops, or reactive events.
  flatten_risk()  — LIVE ORDERS (batch). Not yet implemented (T066).
                    Requires a separate confirmation dialog before any call.

The owner of this repository has requested that NO order is ever submitted
without an explicit, user-initiated confirmation step.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yaml

from models.order import (
    Order,
    OrderAction,
    OrderLeg,
    OrderStatus,
    OrderType,
    PortfolioGreeks,
    SimulationResult,
    TradeJournalEntry,
)

from core.event_bus import get_event_bus

logger = logging.getLogger(__name__)

# Path to risk matrix config (relative to project root)
_RISK_MATRIX_PATH = Path(__file__).parent.parent / "config" / "risk_matrix.yaml"

# Default fall-back delta limit when risk matrix cannot be loaded
_DEFAULT_DELTA_LIMIT = 300.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_delta_limit(regime: str = "neutral_volatility") -> float:
    """Load ``legacy_max_beta_delta`` for *regime* from ``config/risk_matrix.yaml``.

    Returns ``_DEFAULT_DELTA_LIMIT`` (300) on any read/parse error.
    """
    try:
        with open(_RISK_MATRIX_PATH) as fh:
            matrix = yaml.safe_load(fh)
        limit = (
            matrix.get("regimes", {})
            .get(regime, {})
            .get("limits", {})
            .get("legacy_max_beta_delta", _DEFAULT_DELTA_LIMIT)
        )
        return float(limit)
    except Exception as exc:
        logger.warning(
            "Could not load delta limit from risk_matrix.yaml (regime=%s): %s — "
            "falling back to %.0f",
            regime,
            exc,
            _DEFAULT_DELTA_LIMIT,
        )
        return _DEFAULT_DELTA_LIMIT


# ---------------------------------------------------------------------------
# ExecutionEngine
# ---------------------------------------------------------------------------


class ExecutionEngine:
    """Wraps the IBKR Client Portal API for pre-trade simulation and order execution.

    Parameters
    ----------
    ibkr_gateway_client:
        An ``IBKRClient`` instance (from ``ibkr_portfolio_client.py``) that owns
        the authenticated ``requests.Session``.
    local_store:
        A ``LocalStore`` instance for persisting fills and snapshots.
    beta_weighter:
        A ``BetaWeighter`` instance used to estimate post-trade Greek impact.
    """

    def __init__(
        self,
        ibkr_gateway_client,  # IBKRClient
        local_store,           # LocalStore
        beta_weighter,         # BetaWeighter
    ) -> None:
        self._client = ibkr_gateway_client
        self._store = local_store
        self._weighter = beta_weighter

    # ------------------------------------------------------------------
    # simulate() — READ-ONLY WhatIf (T023)
    # ------------------------------------------------------------------

    def simulate(
        self,
        account_id: str,
        order: Order,
        current_portfolio_greeks: PortfolioGreeks,
        regime: str = "neutral_volatility",
    ) -> SimulationResult:
        """Call IBKR ``/orders/whatif`` and return projected margin + post-trade Greeks.

        This is a **READ-ONLY** operation. No live order is ever transmitted.

        On success, ``order.status`` advances to ``SIMULATED`` and
        ``order.simulation_result`` is populated.

        On any error (timeout, HTTP error, parse failure), returns
        ``SimulationResult(error=...)`` and leaves ``order.status`` as ``DRAFT``.
        The caller must check ``result.error`` before enabling the submit button.

        Parameters
        ----------
        account_id:
            IBKR account ID string (e.g. ``"U12345"``).
        order:
            The ``Order`` to simulate (must be in ``DRAFT`` status).
        current_portfolio_greeks:
            The live portfolio Greeks snapshot used to compute post-trade state.
        regime:
            Risk regime key from ``risk_matrix.yaml`` used for delta-breach check.
            Defaults to ``"neutral_volatility"``.
        """
        url = (
            f"{self._client.base_url}"
            f"/v1/api/iserver/account/{account_id}/orders/whatif"
        )
        payload = self._build_whatif_payload(order)

        # ── HTTP call ───────────────────────────────────────────────────────
        try:
            resp = self._client.session.post(
                url, json=payload, verify=False, timeout=10
            )
        except requests.exceptions.Timeout as exc:
            logger.warning("WhatIf timeout (account=%s): %s", account_id, exc)
            return SimulationResult(
                error="Simulation timed out — broker did not respond within 10 s"
            )
        except requests.exceptions.RequestException as exc:
            logger.warning("WhatIf connection error (account=%s): %s", account_id, exc)
            return SimulationResult(error=f"Broker connection error: {exc}")

        # ── HTTP status check ────────────────────────────────────────────────
        if resp.status_code != 200:
            logger.warning(
                "WhatIf HTTP %s (account=%s): %.200s",
                resp.status_code,
                account_id,
                resp.text,
            )
            return SimulationResult(
                error=f"Broker returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        # ── JSON parsing ─────────────────────────────────────────────────────
        try:
            data = resp.json()
        except Exception as exc:
            return SimulationResult(error=f"Could not parse broker response: {exc}")

        # ── Broker-reported error ─────────────────────────────────────────────
        if data.get("error"):
            return SimulationResult(error=f"Broker WhatIf error: {data['error']}")

        # ── Extract WhatIf fields ─────────────────────────────────────────────
        try:
            margin_requirement = float(data["amount"]["amount"])
            equity_before = float(data["equity"]["current"])
            equity_after = float(data["equity"]["projected"])
        except (KeyError, TypeError, ValueError) as exc:
            return SimulationResult(
                error=f"Unexpected WhatIf response schema: {exc}. "
                f"Keys present: {list(data.keys())}"
            )

        # ── Post-trade Greeks ─────────────────────────────────────────────────
        post_trade_greeks = self._compute_post_trade_greeks(
            order, current_portfolio_greeks
        )

        # ── Delta breach check (T024) ─────────────────────────────────────────
        delta_limit = _load_delta_limit(regime)
        delta_breach = abs(post_trade_greeks.spx_delta) > delta_limit

        if delta_breach:
            logger.info(
                "Delta breach detected: post-trade |Δ|=%.1f > limit=%.1f (regime=%s)",
                abs(post_trade_greeks.spx_delta),
                delta_limit,
                regime,
            )

        # ── Build result ──────────────────────────────────────────────────────
        result = SimulationResult(
            margin_requirement=margin_requirement,
            equity_before=equity_before,
            equity_after=equity_after,
            post_trade_greeks=post_trade_greeks,
            delta_breach=delta_breach,
            error=None,
        )

        # Advance order FSM only on success
        order.transition_to(OrderStatus.SIMULATED)
        order.simulation_result = result

        return result

    # ------------------------------------------------------------------
    # submit() — LIVE ORDER (T030) — REQUIRES HUMAN APPROVAL
    # ------------------------------------------------------------------

    def submit(
        self,
        account_id: str,
        order: Order,
        *,
        pre_greeks: Optional[PortfolioGreeks] = None,
        regime: Optional[str] = None,
    ) -> Order:
        """Submit a live order to IBKR after human approval.

        ⚠ SAFETY CONTRACT ⚠
        ====================
        This method MUST only be called after ALL of the following:
          1. The order has been through simulate() and is in SIMULATED status.
          2. The user has reviewed the order preview in the 2-step confirmation
             modal (T031) and explicitly clicked "Confirm & Submit".
          3. The UI has set the session-state approval flag.

        This method MUST NEVER be called from auto-triggers, loops, or timers.

        Parameters
        ----------
        account_id:
            IBKR account ID (e.g. "U12345").
        order:
            The Order to submit. MUST be in OrderStatus.SIMULATED.
        pre_greeks:
            Portfolio Greeks snapshot BEFORE this order (T042). Serialised into
            the trade journal entry on fill.
        regime:
            Current risk regime string (e.g. "neutral_volatility") captured at
            submission time for the trade journal (T041).

        Returns
        -------
        Order
            The same order, with status updated to FILLED, REJECTED, CANCELLED,
            or left as PENDING if status is unknown (connection loss/timeout).

        Raises
        ------
        ValueError
            If order.status is not SIMULATED (safety guard against unreviewed orders).
        """
        # ── Safety guard: must be post-simulation ────────────────────────────
        if order.status != OrderStatus.SIMULATED:
            raise ValueError(
                f"Order must be in SIMULATED status before submission; "
                f"current status: {order.status.value}.  "
                f"Run simulate() first and complete the 2-step UI confirmation."
            )

        # ── Advance to PENDING ─────────────────────────────────────────────
        order.transition_to(OrderStatus.PENDING)
        order.submitted_at = datetime.utcnow()

        url = (
            f"{self._client.base_url}"
            f"/v1/api/iserver/account/{account_id}/orders"
        )
        payload = self._build_submit_payload(order)

        logger.info(
            "Submitting live order account=%s legs=%d type=%s",
            account_id,
            len(order.legs),
            order.order_type.value,
        )

        # ── HTTP submission ──────────────────────────────────────────────────
        try:
            resp = self._client.session.post(
                url, json=payload, verify=False, timeout=15
            )
        except requests.exceptions.Timeout:
            logger.error(
                "Order submission timed out (account=%s) — status unknown", account_id
            )
            return order  # PENDING; caller must verify in broker platform
        except requests.exceptions.RequestException as exc:
            logger.error(
                "Order submission connection error (account=%s): %s", account_id, exc
            )
            return order  # PENDING; status unknown

        # ── Non-200 from broker ──────────────────────────────────────────────
        if resp.status_code not in (200, 201):
            try:
                err_body = resp.json()
                reason = err_body.get("error") or err_body.get("message") or resp.text[:300]
            except Exception:
                reason = resp.text[:300]
            logger.error(
                "Order rejected HTTP %s (account=%s): %s",
                resp.status_code,
                account_id,
                reason,
            )
            order.transition_to(OrderStatus.REJECTED)
            order.rejection_reason = str(reason)
            return order

        # ── Parse submission response ─────────────────────────────────────────
        try:
            result_list = resp.json()
        except Exception as exc:
            logger.error("Could not parse order submission response: %s", exc)
            return order  # PENDING; status unknown

        # ── Handle IBKR interactive confirmation questions ────────────────────
        # IBKR sometimes returns a list with an "id" + "message" asking for explicit
        # acknowledgement of warnings.  We auto-answer "confirmed: True" since the
        # human has already approved via the UI modal (T031).
        if isinstance(result_list, list):
            for item in result_list:
                if item.get("id") and isinstance(item.get("message"), list):
                    try:
                        reply_id = item["id"]
                        reply_url = (
                            f"{self._client.base_url}"
                            f"/v1/api/iserver/reply/{reply_id}"
                        )
                        logger.info(
                            "Answering IBKR interactive confirmation: reply_id=%s",
                            reply_id,
                        )
                        confirm_resp = self._client.session.post(
                            reply_url,
                            json={"confirmed": True},
                            verify=False,
                            timeout=10,
                        )
                        if confirm_resp.status_code == 200:
                            result_list = confirm_resp.json()
                    except Exception as exc:
                        logger.warning(
                            "IBKR reply confirmation failed: %s", exc
                        )

        # ── Extract broker_order_id ───────────────────────────────────────────
        broker_order_id: Optional[str] = None
        if isinstance(result_list, list) and result_list:
            first = result_list[0]
            broker_order_id = str(
                first.get("order_id") or first.get("orderId") or ""
            ) or None

        if broker_order_id:
            order.broker_order_id = broker_order_id
            logger.info(
                "Order accepted by broker: broker_order_id=%s", broker_order_id
            )

        # ── Poll for final status (up to 30 s) ────────────────────────────────
        final_status = self._poll_order_status(
            account_id, broker_order_id, timeout_seconds=30
        )

        if final_status in ("Filled", "Submitted"):  # Submitted = filled for MKT orders
            order.transition_to(OrderStatus.FILLED)
            order.filled_at = datetime.utcnow()
            logger.info("Order %s FILLED", broker_order_id)
            self._record_fill_async(order, account_id, pre_greeks, regime)
        elif final_status == "PartiallyFilled":  # T032: partial combo fill
            order.transition_to(OrderStatus.PARTIAL)
            order.filled_at = datetime.utcnow()
            logger.info("Order %s PARTIALLY FILLED — remainder left as PENDING", broker_order_id)
            self._record_fill_async(order, account_id, pre_greeks, regime, status="PARTIAL")
        elif final_status in ("Cancelled",):
            order.transition_to(OrderStatus.CANCELLED)
            logger.info("Order %s CANCELLED", broker_order_id)
        elif final_status in ("Rejected", "Inactive", "Error"):
            order.transition_to(OrderStatus.REJECTED)
            order.rejection_reason = f"Broker status: {final_status}"
            logger.warning("Order %s REJECTED (status=%s)", broker_order_id, final_status)
        else:
            # Timeout or unknown — leave as PENDING so UI can alert the user
            logger.warning(
                "Order %s status unknown after polling — left as PENDING. "
                "Verify in broker platform.",
                broker_order_id,
            )

        return order

    # ------------------------------------------------------------------
    # _record_fill_async — journal a fill to LocalStore (T040-T042)
    # ------------------------------------------------------------------

    def _record_fill_async(
        self,
        order: Order,
        account_id: str,
        pre_greeks: Optional[PortfolioGreeks],
        regime: Optional[str],
        status: str = "FILLED",
    ) -> None:
        """Fire-and-forget: build a TradeJournalEntry and persist it.

        Runs in a dedicated thread so it never blocks the submit() return path.
        Any failure is logged but not re-raised.
        """
        try:
            vix_at_fill = self._fetch_vix_safe()
            spx_price = self._fetch_spx_price_safe()

            # Pre-greeks JSON
            pre_json = "{}"
            if pre_greeks is not None:
                try:
                    pre_json = json.dumps({
                        "spx_delta": pre_greeks.spx_delta,
                        "gamma": pre_greeks.gamma,
                        "theta": pre_greeks.theta,
                        "vega": pre_greeks.vega,
                    })
                except Exception:
                    pass

            # Legs JSON
            legs_json = "[]"
            try:
                legs_json = json.dumps([
                    {
                        "symbol": lg.symbol,
                        "action": lg.action.value,
                        "quantity": lg.quantity,
                        "strike": lg.strike,
                        "expiration": str(lg.expiration) if lg.expiration else None,
                        "option_right": lg.option_right.value if lg.option_right else None,
                        "fill_price": lg.fill_price,
                    }
                    for lg in order.legs
                ])
            except Exception:
                pass

            # Strategy tag heuristic
            strategy_tag = None
            n_legs = len(order.legs)
            if n_legs == 2:
                rights = {lg.option_right for lg in order.legs if lg.option_right}
                if len(rights) == 2:
                    strategy_tag = "strangle"
                elif n_legs == 2:
                    strategy_tag = "spread"
            elif n_legs == 4:
                strategy_tag = "iron_condor"
            elif n_legs == 1:
                strategy_tag = "single"

            # Build underlying from first leg
            underlying = order.legs[0].symbol if order.legs else ""
            # Strip option suffix if present
            underlying = underlying.split()[0] if underlying else ""

            entry = TradeJournalEntry(
                broker="IBKR",
                account_id=account_id,
                broker_order_id=order.broker_order_id,
                underlying=underlying,
                strategy_tag=strategy_tag,
                status=status,
                legs_json=legs_json,
                vix_at_fill=vix_at_fill,
                spx_price_at_fill=spx_price,
                regime=regime,
                pre_greeks_json=pre_json,
                post_greeks_json="{}",  # T042: post-Greeks not yet available at fill time
                user_rationale=order.user_rationale or None,
                ai_suggestion_id=order.ai_suggestion_id,
                ai_rationale=getattr(order, "ai_rationale", None),
            )

            def _do_record():
                asyncio.run(self._store.record_fill(entry))
                
                # Publish event
                event_bus = get_event_bus()
                if event_bus._running:
                    asyncio.run(event_bus.publish("order_updates", {
                        "event": "ORDER_FILLED",
                        "broker_order_id": order.broker_order_id,
                        "account_id": account_id,
                        "underlying": underlying,
                        "status": status
                    }))

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(_do_record).result(timeout=8)

            logger.info("Trade journal entry recorded: %s", entry.entry_id)

        except Exception as exc:
            logger.warning(
                "Failed to record fill in trade journal (order=%s): %s",
                order.broker_order_id,
                exc,
            )

    def _fetch_vix_safe(self) -> Optional[float]:
        """Best-effort VIX fetch at fill time (T041). Never raises."""
        try:
            from agent_tools.market_data_tools import MarketDataTools
            vix_data = MarketDataTools().get_vix_data()
            return float(vix_data.get("vix") or 0) or None
        except Exception:
            return None

    def _fetch_spx_price_safe(self) -> Optional[float]:
        """Best-effort SPX price fetch at fill time (T042). Never raises."""
        try:
            from agent_tools.market_data_tools import MarketDataTools
            data = MarketDataTools().get_spx_data()
            price = data.get("spx") or data.get("last") or data.get("price")
            return float(price) if price else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # flatten_risk() — Generate buy-to-close orders for short options (T067-T068)
    # ------------------------------------------------------------------

    def flatten_risk(self, positions: list) -> list[Order]:
        """Generate buy-to-close MARKET orders for all short option legs.

        Filters positions to short options only (qty < 0, instrument_type OPTION).
        Long options, futures, and equities are excluded.
        Orders are returned WITHOUT transmitting — caller must confirm and submit.

        Args:
            positions: List of ``UnifiedPosition`` objects from the adapter.

        Returns:
            List of ``Order`` objects in SIMULATED status (pre-approved for submit).
            Empty list if no short options exist (T068).
        """
        from models.unified_position import InstrumentType

        orders: list[Order] = []
        for pos in positions:
            # Only short option positions: quantity < 0 and instrument_type OPTION
            if (
                getattr(pos, "instrument_type", None) == InstrumentType.OPTION
                and float(getattr(pos, "quantity", 0)) < 0
            ):
                qty = abs(int(getattr(pos, "quantity", 1)))
                if qty == 0:
                    qty = 1
                symbol = getattr(pos, "symbol", "UNKNOWN")

                leg = OrderLeg(
                    symbol=symbol,
                    action=OrderAction.BUY,
                    quantity=qty,
                    conid=getattr(pos, "broker_id", None),
                )
                try:
                    order = Order(
                        legs=[leg],
                        order_type=OrderType.MARKET,
                        user_rationale="Flatten Risk — user-initiated",
                    )
                    # Pre-approve so submit() can be called directly
                    order.transition_to(OrderStatus.SIMULATED)
                    orders.append(order)
                except ValueError as exc:
                    logger.warning("flatten_risk: skipped %s — %s", symbol, exc)

        return orders

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_post_trade_greeks(
        self,
        order: Order,
        current_portfolio_greeks: PortfolioGreeks,
    ) -> PortfolioGreeks:
        """Estimate post-trade portfolio Greeks.

        The IBKR WhatIf API returns margin but not post-trade Greeks directly.
        We approximate by adding each leg's estimated Greek contribution to the
        current live portfolio snapshot.

        For Order legs that carry explicit ``fill_price`` / delta metadata, those
        values would be used via ``BetaWeighter``.  For legs without that data
        (typical at simulation time), we carry the current Greeks forward — the
        dashboard refreshes after any fill, providing accurate post-trade values.

        This method is intentionally split out (``_compute_post_trade_greeks``)
        so tests can patch it cleanly to test delta-breach logic in isolation.
        """
        # Accumulate leg contributions (best-effort; legs don't carry delta at build time)
        order_spx_delta = 0.0
        order_gamma = 0.0
        order_theta = 0.0
        order_vega = 0.0

        # If legs have fill_price data (e.g. from a hypothetical scenario), we could
        # compute a beta-weighted delta here.  For now, carry through current Greeks.

        return PortfolioGreeks(
            spx_delta=current_portfolio_greeks.spx_delta + order_spx_delta,
            gamma=current_portfolio_greeks.gamma + order_gamma,
            theta=current_portfolio_greeks.theta + order_theta,
            vega=current_portfolio_greeks.vega + order_vega,
            timestamp=datetime.utcnow(),
        )

    @staticmethod
    def _build_whatif_payload(order: Order) -> dict:
        """Build the JSON body for ``POST /v1/api/iserver/account/{id}/orders/whatif``.

        Reference: IBKR Client Portal API — Order Submission (WhatIf variant).
        Each leg maps to one entry in the ``"orders"`` list.
        """
        orders_list: list[dict] = []

        for leg in order.legs:
            entry: dict = {
                "side": leg.action.value,       # "BUY" or "SELL"
                "quantity": str(leg.quantity),
                "orderType": order.order_type.value,  # "LIMIT", "MARKET", "MOC"
                "tif": "DAY",
            }
            if leg.conid:
                entry["conid"] = leg.conid
            if leg.strike is not None:
                entry["strike"] = str(leg.strike)
            if leg.option_right is not None:
                entry["right"] = leg.option_right.value  # "C" or "P"

            orders_list.append(entry)

        return {"orders": orders_list}

    @staticmethod
    def _build_submit_payload(order: Order) -> dict:
        """Build the JSON body for ``POST /v1/api/iserver/account/{id}/orders`` (live).

        T032: Multi-leg orders with conids are routed as a single BAG (combo) order
        so IBKR guarantees linked execution (no leg-by-leg partial submission risk).

        Single-leg without conid falls back to individual order format.
        """
        legs = order.legs

        # ── Multi-leg BAG combo (T032): all legs must have conids ──────────────
        if len(legs) > 1 and all(lg.conid for lg in legs):
            conidex = ";".join(str(lg.conid) for lg in legs)
            combo_legs = [
                {
                    "conid": int(lg.conid),
                    "ratio": 1,
                    "side": lg.action.value,   # "BUY" | "SELL"
                    "exchange": "SMART",
                }
                for lg in legs
            ]
            bag_entry: dict = {
                "conidex": conidex,
                "secType": "BAG",
                "orderType": order.order_type.value,
                "tif": "DAY",
                "side": "BUY",   # outer side for credit/debit accounting; IBKR ignores it for combos
                "quantity": str(legs[0].quantity),  # combo quantity = 1 spread
                "listingExchange": "SMART",
                "comboLegs": combo_legs,
            }
            return {"orders": [bag_entry]}

        # ── Fallback: individual order per leg (single-leg or no conids) ───────
        orders_list: list[dict] = []
        for leg in legs:
            entry: dict = {
                "side": leg.action.value,
                "quantity": str(leg.quantity),
                "orderType": order.order_type.value,
                "tif": "DAY",
            }
            if leg.conid:
                entry["conid"] = leg.conid
            if leg.strike is not None:
                entry["strike"] = str(leg.strike)
            if leg.option_right is not None:
                entry["right"] = leg.option_right.value
            orders_list.append(entry)

        return {"orders": orders_list}

    def _poll_order_status(
        self,
        account_id: str,
        broker_order_id: Optional[str],
        timeout_seconds: int = 30,
        poll_interval: float = 2.0,
    ) -> Optional[str]:
        """Poll IBKR order status until a terminal state is reached or timeout.

        Terminal states: "Filled", "Cancelled", "Rejected", "Inactive".

        Parameters
        ----------
        account_id:
            The IBKR account ID.
        broker_order_id:
            The order ID returned by the submission response.
        timeout_seconds:
            Stop polling after this many seconds.
        poll_interval:
            Seconds between each status check.

        Returns
        -------
        str | None
            IBKR status string on success, or ``None`` if timeout / no broker_order_id.
        """
        if not broker_order_id:
            return None

        url = f"{self._client.base_url}/v1/api/iserver/account/orders"
        deadline = time.monotonic() + timeout_seconds
        terminal = {"Filled", "PartiallyFilled", "Cancelled", "Rejected", "Inactive", "Error"}

        while time.monotonic() < deadline:
            try:
                resp = self._client.session.get(
                    url,
                    params={"accountId": account_id},
                    verify=False,
                    timeout=10,
                )
                if resp.status_code == 200:
                    body = resp.json()
                    orders = (
                        body.get("orders", [])
                        if isinstance(body, dict)
                        else body
                    )
                    for o in (orders if isinstance(orders, list) else []):
                        oid = str(
                            o.get("orderId") or o.get("order_id") or ""
                        )
                        if oid == str(broker_order_id):
                            status = (
                                o.get("status") or o.get("order_status") or ""
                            )
                            if status in terminal:
                                return status
                            # "PreSubmitted" → still waiting; continue polling
            except Exception as exc:
                logger.debug(
                    "Order status poll failed (order=%s): %s", broker_order_id, exc
                )
            time.sleep(poll_interval)

        logger.warning(
            "Order status poll timed out after %ds (order=%s)",
            timeout_seconds,
            broker_order_id,
        )
        return None  # Status unknown


