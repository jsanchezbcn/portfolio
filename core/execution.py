"""core/execution.py — ExecutionEngine: pre-trade simulation and order execution.

SAFETY CONTRACT
===============
This module interacts with a LIVE IBKR brokerage account.

  simulate()      — READ-ONLY. Calls /orders/whatif only. Zero orders transmitted.
  submit()        — LIVE ORDERS. Not yet implemented (T030).
                    When implemented, MUST ONLY be called after explicit multi-step
                    user confirmation in the UI. NEVER auto-triggered.
  flatten_risk()  — LIVE ORDERS (batch). Not yet implemented (T066).
                    Requires a separate confirmation dialog before any call.

The owner of this repository has requested that NO order is ever submitted
without an explicit, user-initiated confirmation step.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yaml

from models.order import (
    Order,
    OrderStatus,
    PortfolioGreeks,
    SimulationResult,
)

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
    # submit() — LIVE ORDER — NOT YET IMPLEMENTED (T030)
    # ------------------------------------------------------------------

    def submit(self, account_id: str, order: Order) -> Order:
        """Submit a live order to IBKR.

        ⚠ SAFETY: This method MUST only be called after the user has completed
        a 2-step confirmation in the UI (preview → explicit "Confirm & Submit"
        button click).  It must NEVER be called from an auto-trigger, loop,
        or timer.

        Not yet implemented — stub raises ``NotImplementedError`` until T030.
        """
        raise NotImplementedError(
            "submit() is not yet implemented.  "
            "Live execution requires T030 implementation + explicit UI confirmation.  "
            "Do NOT wire this to any auto-trigger."
        )

    # ------------------------------------------------------------------
    # flatten_risk() — BATCH LIVE ORDERS — NOT YET IMPLEMENTED (T066)
    # ------------------------------------------------------------------

    def flatten_risk(self, account_id: str, positions: list) -> list[Order]:
        """Generate buy-to-close market orders for all short option legs.

        ⚠ SAFETY: Requires a dedicated confirmation dialog before any call.

        Not yet implemented — stub raises ``NotImplementedError`` until T066.
        """
        raise NotImplementedError(
            "flatten_risk() is not yet implemented.  "
            "Requires T066–T073 implementation + explicit panel confirmation."
        )

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
