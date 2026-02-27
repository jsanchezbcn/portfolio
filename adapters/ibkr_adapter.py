from __future__ import annotations

import asyncio
import calendar
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from adapters.base_adapter import BrokerAdapter
from ibkr_portfolio_client import IBKRClient
from models.order import PortfolioGreeks
from models.unified_position import InstrumentType, UnifiedPosition
from risk_engine.beta_weighter import BetaWeighter
import json
import ssl

LOGGER = logging.getLogger(__name__)

class IBKRAdapter(BrokerAdapter):
    """IBKR-backed adapter that normalizes positions and enriches Greeks."""

    def __init__(self, client: IBKRClient | None = None) -> None:
        """Initialize adapter and read Greeks cache controls from environment."""

        self.client = client or IBKRClient()
        self.last_greeks_status: dict[str, Any] = {}
        disable_cache_raw = str(os.getenv("GREEKS_DISABLE_CACHE", "")).strip().lower()
        self.disable_tasty_cache = disable_cache_raw in {"1", "true", "yes", "on"}
        force_refresh_raw = str(os.getenv("GREEKS_FORCE_REFRESH_ON_MISS", "1")).strip().lower()
        self.force_refresh_on_miss = force_refresh_raw in {"1", "true", "yes", "on"}
        self._stock_option_greeks_cache_path = Path(
            os.getenv("STOCK_OPTION_GREEKS_CACHE_FILE", ".stock_option_greeks_cache.json")
        )
        self._stock_option_greeks_cache: dict[str, dict[str, Any]] = self._load_stock_option_greeks_cache()
        # BetaWeighter provides the SPX beta waterfall used by compute_portfolio_greeks().
        # Session is injected later if a Tastytrade session is available.
        self._beta_weighter = BetaWeighter(tastytrade_session=None, ibkr_client=self.client)

    async def fetch_positions(self, account_id: str) -> list[UnifiedPosition]:
        """Fetch IBKR positions and convert to unified schema.

        In SOCKET mode (IB_API_MODE=SOCKET) positions are retrieved via ib_async
        directly from TWS/IB Gateway so the Client Portal REST API is not required.
        Falls back to Client Portal if the socket fetch returns nothing.
        """
        _api_mode = os.getenv("IB_API_MODE", "PORTAL").split("#")[0].strip().upper()
        if _api_mode == "SOCKET":
            try:
                socket_positions = await self._fetch_positions_via_tws_socket(account_id)
                if socket_positions:
                    return socket_positions
                LOGGER.warning(
                    "TWS socket positions returned 0 items for %s – falling back to Client Portal",
                    account_id,
                )
            except Exception as _exc:
                LOGGER.warning("TWS socket positions failed (%s); falling back to Client Portal", _exc)

        try:
            raw_positions = await asyncio.to_thread(self.client.get_positions, account_id)
        except Exception as exc:
            if _api_mode != "SOCKET":
                LOGGER.warning(
                    "Client Portal positions failed for %s (%s); attempting TWS socket fallback",
                    account_id,
                    exc,
                )
                try:
                    socket_positions = await self._fetch_positions_via_tws_socket(account_id)
                    if socket_positions:
                        return socket_positions
                except Exception as socket_exc:
                    LOGGER.warning("TWS socket fallback positions failed: %s", socket_exc)
            raise ConnectionError(f"Unable to fetch IBKR positions for account {account_id}.") from exc

        if _api_mode != "SOCKET" and not raw_positions:
            try:
                socket_positions = await self._fetch_positions_via_tws_socket(account_id)
                if socket_positions:
                    LOGGER.info(
                        "Client Portal returned 0 positions for %s; using %d TWS socket positions",
                        account_id,
                        len(socket_positions),
                    )
                    return socket_positions
            except Exception as socket_exc:
                LOGGER.debug("TWS socket fallback after empty portal positions failed: %s", socket_exc)

        transformed: list[UnifiedPosition] = []

        for position in raw_positions:
            try:
                transformed.append(self._to_unified_position(position))
            except Exception as exc:
                LOGGER.warning("Skipping unparseable IBKR position payload: %s", exc)
                continue

        return transformed

    async def fetch_greeks(self, positions: list[UnifiedPosition]) -> list[UnifiedPosition]:
        """Enrich option positions with Greeks using cached/live Tastytrade data."""

        updated_positions: list[UnifiedPosition] = []
        prefetch_targets = self._build_prefetch_targets(positions)
        prefetch_results: dict[str, int] = {}
        missing_greeks_details: list[dict[str, Any]] = []

        if not self.disable_tasty_cache:
            for underlying, request_set in prefetch_targets.items():
                try:
                    fetched = await self.client.options_cache.fetch_and_cache_options_for_underlying(
                        underlying,
                        only_options=request_set,
                        force_refresh=True,
                    )
                    prefetch_results[underlying] = len(fetched)
                except Exception as exc:
                    LOGGER.warning("Prefetch failed for %s: %s", underlying, exc)
                    prefetch_results[underlying] = 0

        cache_miss_count = 0
        stock_option_cache_hits = 0

        def _normalize_greeks_source(value: Any) -> str:
            if isinstance(value, str):
                return value.strip().lower()
            return "none"

        def _coerce_broker_conid(value: Any) -> int | None:
            if value in (None, "", "N/A"):
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        # ------------------------------------------------------------------
        # IBKR-first: batch-fetch option Greeks from the market-data snapshot
        # API for all positions that lack native greeks (typical for FOP/OPT
        # positions where the /portfolio endpoint returns None for all greeks).
        # Tastytrade is used ONLY as a secondary fallback for the remainder.
        # ------------------------------------------------------------------
        _options_needing_greeks: list[tuple[UnifiedPosition, int]] = []
        for pos in positions:
            if pos.instrument_type != InstrumentType.OPTION:
                continue
            if abs(float(pos.quantity or 0.0)) <= 1e-9:
                continue
            if _normalize_greeks_source(getattr(pos, "greeks_source", None)) != "none":
                continue
            conid = _coerce_broker_conid(getattr(pos, "broker_id", None))
            if conid is None:
                continue
            _options_needing_greeks.append((pos, conid))
        ibkr_snapshot_hits = 0
        ibkr_snapshot_errors: list[str] = []
        if _options_needing_greeks:
            _conids = [conid for _, conid in _options_needing_greeks]
            _api_mode = os.getenv("IB_API_MODE", "PORTAL").split("#")[0].strip().upper()
            if _api_mode == "SOCKET":
                # TWS socket mode: use ib_async modelGreeks instead of Client Portal
                # snapshot REST API (which returns 401/400 or 429 when portal is not authenticated).
                # Pass full position objects so contracts can be built from metadata.
                try:
                    _ibkr_greeks = await self._fetch_greeks_via_tws_socket(_options_needing_greeks)
                except Exception as _exc:
                    LOGGER.warning("TWS socket greeks batch failed: %s", _exc)
                    _ibkr_greeks = {}
                # NOTE: Client Portal snapshot fallback intentionally disabled in SOCKET mode.
                # Hitting localhost:5001 in socket mode causes 429 rate-limit errors that
                # blow the 35s timeout before SPX price can be fetched.
                # Positions missing modelGreeks from TWS will fall through to the
                # Tastytrade path below (or remain without Greeks in IBKR-only mode).
                _remaining_conids: list[int] = []
                LOGGER.debug(
                    "SOCKET mode: TWS modelGreeks enriched %d/%d options (no Portal fallback)",
                    len(_ibkr_greeks), len(_options_needing_greeks),
                )
            else:
                try:
                    _ibkr_greeks = await asyncio.to_thread(
                        self.client.get_market_greeks_batch, _conids
                    )
                except Exception as _exc:
                    LOGGER.warning("IBKR snapshot greeks batch failed: %s", _exc)
                    _ibkr_greeks = {}
                _remaining_pairs = [
                    (pos, conid) for pos, conid in _options_needing_greeks if conid not in _ibkr_greeks
                ]
                if _remaining_pairs:
                    try:
                        _socket_greeks = await self._fetch_greeks_via_tws_socket(_remaining_pairs)
                        if _socket_greeks:
                            _ibkr_greeks.update(_socket_greeks)
                            LOGGER.info(
                                "TWS socket fallback enriched %d/%d remaining options after Client Portal snapshot",
                                len(_socket_greeks),
                                len(_remaining_pairs),
                            )
                    except Exception as _exc:
                        LOGGER.debug("TWS socket fallback for portal greeks failed: %s", _exc)
            for pos, cid in _options_needing_greeks:
                g = _ibkr_greeks.get(cid)
                if not g:
                    continue
                # Per-contract greeks from snapshot; scale by qty × multiplier
                mult = pos.contract_multiplier if pos.contract_multiplier > 0 else 1.0
                dg_mult = mult  # delta & gamma
                tv_mult = mult  # theta & vega
                delta = self._safe_optional_float(g.get("delta"))
                gamma = self._safe_optional_float(g.get("gamma"))
                theta = self._safe_optional_float(g.get("theta"))
                vega  = self._safe_optional_float(g.get("vega"))
                if delta is None and theta is None and vega is None:
                    ibkr_snapshot_errors.append(pos.symbol)
                    continue
                pos.delta = (delta or 0.0) * pos.quantity * dg_mult
                pos.gamma = (gamma or 0.0) * pos.quantity * dg_mult
                pos.theta = (theta or 0.0) * pos.quantity * tv_mult
                pos.vega  = (vega  or 0.0) * pos.quantity * tv_mult
                pos.iv    = self._safe_optional_float(g.get("iv"))
                pos.greeks_source = g.get("source", "ibkr_snapshot")
                self._update_stock_option_greeks_cache(pos)
                ibkr_snapshot_hits += 1

        LOGGER.debug(
            "Greeks enrichment: %d/%d enriched, %d failed (source: %s)",
            ibkr_snapshot_hits,
            len(_options_needing_greeks),
            len(ibkr_snapshot_errors),
            "tws_socket" if os.getenv("IB_API_MODE", "PORTAL").split("#")[0].strip().upper() == "SOCKET" else "ibkr_snapshot",
        )

        def _derive_spx_proxy_from_positions(_positions: list[UnifiedPosition]) -> float:
            """Best-effort SPX proxy using live ES/MES marks from current positions.

            SPX and ES/MES are tightly coupled for portfolio beta-weighted delta.
            When index market data is unavailable (common IBKR permission edge),
            use futures marks so risk computation remains actionable.
            """
            for _p in _positions:
                if _p.instrument_type != InstrumentType.FUTURE:
                    continue
                _sym = str(_p.symbol or "").upper()
                if not (_sym.startswith("ES") or _sym.startswith("MES")):
                    continue
                _qty = float(_p.quantity or 0.0)
                _mult = float(_p.contract_multiplier or 0.0)
                _mv = float(_p.market_value or 0.0)
                if abs(_qty) > 1e-9 and _mult > 0:
                    _px = abs(_mv / (_qty * _mult))
                    if 4000 < _px < 9000:
                        return float(_px)
            return 0.0

        # Fetch SPX price once for the entire batch (used by BetaWeighter, T016)
        # In SOCKET mode: request SPX Index via ib_async TWS socket first;
        # the Client Portal REST path is only tried as a fallback.
        _api_mode_for_spx = os.getenv("IB_API_MODE", "PORTAL").split("#")[0].strip().upper()
        try:
            if _api_mode_for_spx == "SOCKET":
                _spx_price_for_batch = await self._fetch_spx_price_via_tws_socket()
                if not _spx_price_for_batch or _spx_price_for_batch <= 0:
                    LOGGER.debug("TWS SPX price=0, falling back to client.get_spx_price()")
                    _spx_price_for_batch = await asyncio.to_thread(self.client.get_spx_price)
            else:
                _spx_price_for_batch = await asyncio.to_thread(self.client.get_spx_price)
        except Exception as _exc:
            LOGGER.debug("Could not fetch SPX price for BetaWeighter batch: %s", _exc)
            _spx_price_for_batch = 0.0

        # Final fallback: derive SPX proxy from ES/MES futures marks in current positions.
        if not _spx_price_for_batch or float(_spx_price_for_batch) <= 0:
            _proxy_spx = _derive_spx_proxy_from_positions(positions)
            if _proxy_spx > 0:
                _spx_price_for_batch = _proxy_spx
                LOGGER.info("Using ES/MES-derived SPX proxy price: %.2f", _proxy_spx)

        _spx_price_candidate = self._safe_optional_float(_spx_price_for_batch)
        _spx_price_for_batch_num = (
            float(_spx_price_candidate)
            if isinstance(_spx_price_candidate, (int, float))
            else 0.0
        )
        if _spx_price_for_batch_num <= 0:
            # Keep SPX beta-weighted delta actionable even when live SPX quote fails.
            _spx_price_for_batch_num = float(getattr(self.client, "spx_price", 0.0) or 0.0)
        if _spx_price_for_batch_num <= 0:
            _spx_price_for_batch_num = 6475.0

        # If Tastytrade session is available, let BetaWeighter source live stock betas.
        try:
            _tt_session = getattr(getattr(self.client, "options_cache", None), "session", None)
            if _tt_session is not None and getattr(self._beta_weighter, "_session", None) is None:
                self._beta_weighter._session = _tt_session
        except Exception:
            pass

        for position in positions:
            if position.instrument_type == InstrumentType.FUTURE:
                # Recompute SPX delta with the current batch SPX price so that a
                # failed get_spx_price() during fetch_positions() doesn't freeze
                # spx_delta at 0.0 for the life of the session.
                if _spx_price_for_batch_num > 0 and abs(float(position.quantity or 0.0)) > 1e-9:
                    mult = position.contract_multiplier if (position.contract_multiplier or 0) > 0 else 1.0
                    qty = float(position.quantity)
                    # Derive the futures market price from market_value:
                    # market_value = qty * mult * price  →  price = mv / (qty * mult)
                    if qty and mult:
                        _fut_px = abs(float(position.market_value) / (qty * mult))
                    else:
                        _fut_px = 0.0
                    position.underlying_price = _fut_px if _fut_px > 0 else _spx_price_for_batch_num
                    bw_result = await self._beta_weighter.compute_spx_equivalent_delta(
                        position, _spx_price_for_batch_num
                    )
                    position.spx_delta = bw_result.spx_equivalent_delta
                    position.beta_unavailable = bw_result.beta_unavailable
                updated_positions.append(position)
                continue

            if position.instrument_type != InstrumentType.OPTION:
                # EQUITY / other: refresh with current SPX price and current stock price.
                if _spx_price_for_batch_num > 0 and abs(float(position.quantity or 0.0)) > 1e-9:
                    qty = float(position.quantity)
                    _eq_px = abs(float(position.market_value) / qty) if qty else 0.0
                    if _eq_px <= 0:
                        try:
                            _eq_px = abs(float(getattr(position, "avg_price", 0.0) or 0.0))
                        except Exception:
                            _eq_px = 0.0
                    if _eq_px > 0:
                        position.underlying_price = _eq_px
                    bw_result = await self._beta_weighter.compute_spx_equivalent_delta(
                        position, _spx_price_for_batch_num
                    )
                    position.spx_delta = bw_result.spx_equivalent_delta
                    position.beta_unavailable = bw_result.beta_unavailable
                updated_positions.append(position)
                continue

            # Ignore stale option rows with zero quantity to avoid false
            # "missing greeks" noise and unnecessary retry/fallback work.
            if abs(float(position.quantity or 0.0)) <= 1e-9:
                updated_positions.append(position)
                continue

            # Ensure underlying_price is populated for SPX beta-weighting.
            # IBKR /portfolio often omits underlier price for options.
            _under_px_candidate = self._safe_optional_float(getattr(position, "underlying_price", None))
            _under_px = (
                float(_under_px_candidate)
                if isinstance(_under_px_candidate, (int, float))
                else None
            )
            if (_under_px is None or _under_px <= 0) and _spx_price_for_batch_num > 0:
                under = str(position.underlying or "").upper().lstrip("/")
                if under in {"SPX", "SPXW", "XSP", "ES", "MES"}:
                    position.underlying_price = float(_spx_price_for_batch_num)
                elif position.strike is not None and position.strike > 0:
                    # Last-resort fallback when underlier quote is not available.
                    position.underlying_price = float(position.strike)

            if position.greeks_source in {"ibkr_native", "ibkr_snapshot", "tws_socket"}:
                # Already have IBKR-sourced greeks — still need SPX delta via BetaWeighter
                if _spx_price_for_batch_num > 0:
                    bw_result = await self._beta_weighter.compute_spx_equivalent_delta(
                        position, _spx_price_for_batch_num
                    )
                    position.spx_delta = bw_result.spx_equivalent_delta
                    position.beta_unavailable = bw_result.beta_unavailable
                updated_positions.append(position)
                continue

            if not position.expiration or not position.strike or not position.option_type:
                updated_positions.append(position)
                continue

            # When disable_tasty_cache=True (IBKR-only mode), skip the prefetch
            # cache but still attempt a live Tastytrade fetch as last resort for
            # positions where IBKR returned no greeks data.
            candidates = self._candidate_tasty_underlyings(position)
            if not candidates:
                updated_positions.append(position)
                continue
            greeks: dict[str, Any] = {"source": "cache_miss"}
            selected_underlying = candidates[0] if candidates else ""
            for tasty_underlying in candidates:
                try:
                    greeks = await self.client.get_tastytrade_option_greeks(
                        tasty_underlying,
                        position.expiration.strftime("%Y-%m-%d"),
                        position.strike,
                        position.option_type,
                        use_cache=not self.disable_tasty_cache,
                        force_refresh_on_miss=self.force_refresh_on_miss or self.disable_tasty_cache,
                    )
                except Exception as exc:
                    LOGGER.warning("Greeks lookup failed for %s (%s): %s", position.symbol, tasty_underlying, exc)
                    greeks = {"source": "error"}
                if str(greeks.get("source") or "") not in {
                    "cache_miss",
                    "error",
                    "session_error",
                    "live_miss",
                    "cache_and_live_miss",
                }:
                    selected_underlying = tasty_underlying
                    break

            source = str(greeks.get("source") or "")
            if source in {"cache_miss", "error", "session_error", "live_miss", "cache_and_live_miss"}:
                if self._apply_stock_option_cache(position):
                    stock_option_cache_hits += 1
                    if _spx_price_for_batch_num > 0:
                        bw_result = await self._beta_weighter.compute_spx_equivalent_delta(
                            position, _spx_price_for_batch_num
                        )
                        position.spx_delta = bw_result.spx_equivalent_delta
                        position.beta_unavailable = bw_result.beta_unavailable
                    updated_positions.append(position)
                    continue

                cache_miss_count += 1
                raw_contract_multiplier = self._safe_optional_float(getattr(position, "contract_multiplier", None))
                contract_multiplier = raw_contract_multiplier if raw_contract_multiplier and raw_contract_multiplier > 0 else 1.0
                missing_greeks_details.append(
                    {
                        "symbol": position.symbol,
                        "underlying": position.underlying or "",
                        "expiry": position.expiration.isoformat() if position.expiration else "",
                        "strike": float(position.strike) if position.strike is not None else None,
                        "option_type": position.option_type or "",
                        "quantity": float(position.quantity),
                        "contract_multiplier": contract_multiplier,
                        "lookup_candidates": ", ".join(candidates),
                        "lookup_used": selected_underlying,
                        "reason": source,
                    }
                )
                position.greeks_source = "none"
                updated_positions.append(position)
                continue

            per_contract_delta = self._safe_float(greeks.get("delta"))
            per_contract_gamma = self._safe_float(greeks.get("gamma"))
            per_contract_theta = self._safe_float(greeks.get("theta"))
            per_contract_vega = self._safe_float(greeks.get("vega"))

            raw_contract_multiplier = self._safe_optional_float(getattr(position, "contract_multiplier", None))
            contract_multiplier = raw_contract_multiplier if raw_contract_multiplier and raw_contract_multiplier > 0 else 1.0
            delta_gamma_multiplier = self._option_delta_gamma_multiplier(position, contract_multiplier)
            theta_vega_multiplier = self._option_theta_vega_multiplier(position, contract_multiplier)

            position.delta = per_contract_delta * position.quantity * delta_gamma_multiplier
            position.gamma = per_contract_gamma * position.quantity * delta_gamma_multiplier
            position.theta = per_contract_theta * position.quantity * theta_vega_multiplier
            position.vega = per_contract_vega * position.quantity * theta_vega_multiplier
            position.iv = self._safe_optional_float(greeks.get("impliedVol"))
            position.greeks_source = "tastytrade"
            self._update_stock_option_greeks_cache(position)

            # Use BetaWeighter for accurate SPX-equivalent delta (T016).
            # underlying_price is already populated on the position (T005 fix).
            if _spx_price_for_batch_num > 0:
                bw_result = await self._beta_weighter.compute_spx_equivalent_delta(
                    position, _spx_price_for_batch_num
                )
                position.spx_delta = bw_result.spx_equivalent_delta
                position.beta_unavailable = bw_result.beta_unavailable  # T019 flag
            else:
                # Fallback to legacy internal formula when SPX price is unavailable
                price = position.market_value / position.quantity if position.quantity else 0.0
                _fallback_under_px = self._safe_optional_float(getattr(position, "underlying_price", None))
                if _fallback_under_px is not None and _fallback_under_px > 0:
                    price = float(_fallback_under_px)
                elif position.strike is not None and position.strike > 0:
                    price = float(position.strike)
                position.spx_delta = self.client.calculate_spx_weighted_delta(
                    symbol=position.underlying,
                    position_qty=position.quantity,
                    price=price,
                    underlying_delta=per_contract_delta,
                    multiplier=contract_multiplier,
                )
            updated_positions.append(position)

        self.last_greeks_status = {
            "prefetch_targets": {key: len(value) for key, value in prefetch_targets.items()},
            "prefetch_results": prefetch_results,
            "ibkr_snapshot_total": len(_options_needing_greeks),
            "ibkr_snapshot_hits": ibkr_snapshot_hits,
            "ibkr_snapshot_errors": ibkr_snapshot_errors,
            "stock_option_cache_hits": stock_option_cache_hits,
            "cache_miss_count": cache_miss_count,
            "missing_greeks_details": missing_greeks_details,
            "last_session_error": getattr(self.client.options_cache, "last_session_error", None),
            "disable_tasty_cache": self.disable_tasty_cache,
            "force_refresh_on_miss": self.force_refresh_on_miss,
            "tasty_fallback_enabled": not self.disable_tasty_cache,
            "spx_price": _spx_price_for_batch_num,  # T020: exposed for dashboard SPX price check
            "spx_price_source": getattr(self.client, "_last_spx_source", "unknown"),
        }

        return updated_positions

    async def compute_portfolio_greeks(
        self,
        positions: list[UnifiedPosition],
        spx_price: float | None = None,
    ) -> PortfolioGreeks:
        """Aggregate all positions into a PortfolioGreeks snapshot using BetaWeighter.

        If *spx_price* is not supplied it is fetched from the IBKR client.  When
        the price cannot be determined all deltas are 0 and a CRITICAL log is emitted
        so the dashboard can show an error state (T020).
        """
        if spx_price is None or spx_price <= 0:
            try:
                spx_price = await asyncio.to_thread(self.client.get_spx_price)
            except Exception as exc:
                LOGGER.error("SPX price unavailable — portfolio delta cannot be computed: %s", exc)
                spx_price = 0.0
        return await self._beta_weighter.compute_portfolio_spx_delta(positions, spx_price)

    async def _fetch_positions_via_tws_socket(self, account_id: str) -> list[UnifiedPosition]:
        """Fetch portfolio positions from TWS via ib_async reqAccountUpdates.

        Returns a list of UnifiedPosition objects built directly from the ib_async
        PortfolioItem stream.  Greeks are intentionally left at zero so the
        subsequent fetch_greeks() call can enrich them via modelGreeks.
        """
        try:
            from ib_async import IB
        except ImportError:
            LOGGER.warning("ib_async not installed – cannot fetch positions via TWS socket")
            return []

        host      = os.getenv("IB_SOCKET_HOST", "127.0.0.1")
        port      = int(os.getenv("IB_SOCKET_PORT", "7496"))
        client_id = int(os.getenv("IB_POSITIONS_CLIENT_ID", "11"))

        ib = IB()
        result: list[UnifiedPosition] = []
        try:
            await ib.connectAsync(host=host, port=port, clientId=client_id)
            LOGGER.info(
                "IBKRAdapter positions socket connected (clientId=%d, account=%s)",
                client_id, account_id,
            )

            # ib_async automatically sends reqPositions on connect.  Positions
            # arrive via positionEvent and are stored in ib._positions.
            # Give TWS up to 2 s to deliver the full snapshot, then fall back
            # to whatever arrived so far.
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)
                if ib.positions():   # positions have started arriving
                    break
            await asyncio.sleep(0.5)  # allow remaining items to flush

            # Use Position stream directly (robust in SOCKET mode and available
            # immediately after connect). These objects do not include market
            # value fields, but are sufficient for downstream Greeks enrichment.
            for p in ib.positions():
                if account_id and p.account and p.account != account_id:
                    continue
                try:
                    pos = self._position_to_unified_position(p)
                    if pos is not None:
                        result.append(pos)
                except Exception as exc:
                    sym = getattr(p.contract, "symbol", "?")
                    LOGGER.warning("Skipping TWS position %s: %s", sym, exc)
            LOGGER.info(
                "TWS socket positions (Position): %d positions for account %s",
                len(result), account_id,
            )

        except Exception as exc:
            LOGGER.warning("TWS socket positions fetch failed: %s", exc)
            return []
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

        return result

    def _position_to_unified_position(self, p: Any) -> UnifiedPosition | None:
        """Convert an ib_async Position (reqPositions) to UnifiedPosition.

        Position has: account, contract, position (qty), avgCost.
        Market value and unrealizedPNL are NOT available – set to 0.
        """
        # Reuse the PortfolioItem converter by building a thin wrapper
        class _FakeItem:
            contract     = p.contract
            position     = p.position
            averageCost  = p.avgCost
            marketValue  = 0.0
            unrealizedPNL = 0.0
            marketPrice  = 0.0
            account      = p.account

        return self._portfolio_item_to_unified_position(_FakeItem())

    def _portfolio_item_to_unified_position(self, item: Any) -> UnifiedPosition | None:
        """Convert an ib_async PortfolioItem to UnifiedPosition.

        PortfolioItem fields used:
            item.contract  – Contract(conId, symbol, secType, lastTradeDateOrContractMonth,
                              strike, right, multiplier, localSymbol)
            item.position  – signed quantity
            item.averageCost, item.marketValue, item.unrealizedPNL, item.marketPrice
        """
        c         = item.contract
        sec_type  = (c.secType or "").upper()
        qty       = float(item.position or 0.0)
        avg_cost  = float(item.averageCost  or 0.0)
        mkt_value = float(item.marketValue  or 0.0)
        upnl      = float(item.unrealizedPNL or 0.0)
        mkt_price = float(item.marketPrice  or 0.0)
        conid     = str(c.conId or "")

        # Multiplier from contract string
        try:
            mult = float(c.multiplier) if c.multiplier else 0.0
        except (TypeError, ValueError):
            mult = 0.0

        if sec_type in ("OPT", "FOP"):
            underlying = (c.symbol or "").upper()
            expiry_str = c.lastTradeDateOrContractMonth or ""
            right      = (c.right or "C").upper()
            option_type = "call" if right == "C" else "put"
            strike     = float(c.strike or 0.0)

            expiration: date | None = None
            if expiry_str:
                try:
                    expiration = datetime.strptime(expiry_str[:8], "%Y%m%d").date()
                except (ValueError, TypeError):
                    pass

            dte = (expiration - date.today()).days if expiration else None

            # Determine contract multiplier (contract field → symbol default)
            if mult <= 0:
                _mult_defaults: dict[str, float] = {
                    "ES": 50.0, "MES": 5.0, "NQ": 20.0, "MNQ": 2.0,
                    "RTY": 50.0, "M2K": 10.0, "YM": 5.0, "MYM": 0.5,
                    "SPX": 100.0, "SPXW": 100.0, "NDX": 100.0,
                }
                mult = _mult_defaults.get(underlying, 100.0)  # default for equity opts

            # Use localSymbol as display symbol if available (matches portal format)
            symbol = c.localSymbol or (
                f"{underlying} {expiry_str} {strike:.0f} {right[0]}"
            )

            return UnifiedPosition(
                symbol              = symbol,
                instrument_type     = InstrumentType.OPTION,
                broker              = "ibkr",
                quantity            = qty,
                contract_multiplier = mult,
                avg_price           = avg_cost / mult if mult > 0 else avg_cost,
                market_value        = mkt_value,
                unrealized_pnl      = upnl,
                underlying          = underlying,
                strike              = strike,
                expiration          = expiration,
                option_type         = option_type,
                days_to_expiration  = dte,
                delta               = 0.0,
                gamma               = 0.0,
                theta               = 0.0,
                vega                = 0.0,
                greeks_source       = "none",
                broker_id           = conid,
            )

        elif sec_type == "FUT":
            ticker = (c.symbol or "").upper()
            _fut_mult_defaults: dict[str, float] = {
                "ES": 50.0, "MES": 5.0, "NQ": 20.0, "MNQ": 2.0,
                "RTY": 50.0, "M2K": 10.0, "YM": 5.0, "MYM": 0.5,
                "GC": 100.0, "MGC": 10.0, "SI": 5000.0, "CL": 1000.0,
            }
            if mult <= 0:
                mult = _fut_mult_defaults.get(ticker, 1.0)
            spx_delta = self.client.calculate_spx_weighted_delta(
                symbol           = ticker,
                position_qty     = qty,
                price            = mkt_price,
                underlying_delta = 1.0,
                multiplier       = mult,
            )
            return UnifiedPosition(
                symbol              = c.localSymbol or ticker,
                instrument_type     = InstrumentType.FUTURE,
                broker              = "ibkr",
                quantity            = qty,
                contract_multiplier = mult,
                avg_price           = avg_cost / mult if mult > 0 else avg_cost,
                market_value        = mkt_value,
                unrealized_pnl      = upnl,
                delta               = qty * mult,
                spx_delta           = spx_delta,
                greeks_source       = "ibkr_native",
                broker_id           = conid,
            )

        elif sec_type == "STK":
            ticker = (c.symbol or "").upper()
            spx_delta = self.client.calculate_spx_weighted_delta(
                symbol           = ticker,
                position_qty     = qty,
                price            = mkt_price,
                underlying_delta = 1.0,
                multiplier       = 1.0,
            )
            return UnifiedPosition(
                symbol              = ticker,
                instrument_type     = InstrumentType.EQUITY,
                broker              = "ibkr",
                quantity            = qty,
                contract_multiplier = 1.0,
                avg_price           = avg_cost,
                market_value        = mkt_value,
                unrealized_pnl      = upnl,
                delta               = qty,
                spx_delta           = spx_delta,
                greeks_source       = "ibkr_native",
                broker_id           = conid,
            )

        else:
            LOGGER.debug(
                "_portfolio_item_to_unified_position: skipping unhandled secType=%s symbol=%s",
                sec_type, c.symbol,
            )
            return None

    async def _fetch_spx_price_via_tws_socket(self) -> float:
        """Fetch current SPX index price via ib_async TWS socket.

        Used in SOCKET mode so BetaWeighter delta computation does not rely on
        Client Portal REST API (which requires portal auth not needed by TWS).
        Returns 0.0 on any failure; caller should fall back to client.get_spx_price().
        """
        try:
            from ib_async import IB, Contract
        except ImportError:
            return 0.0

        host            = os.getenv("IB_SOCKET_HOST", "127.0.0.1")
        port            = int(os.getenv("IB_SOCKET_PORT", "7496"))
        client_id       = int(os.getenv("IB_SPX_PRICE_CLIENT_ID", "14"))
        market_data_type = int(os.getenv("IB_GREEKS_MARKET_DATA_TYPE", "3"))

        ib = IB()
        try:
            await ib.connectAsync(host=host, port=port, clientId=client_id)
            try:
                ib.reqMarketDataType(market_data_type)
            except Exception:
                pass

            spx_contract = Contract(secType="IND", symbol="SPX", exchange="CBOE", currency="USD")
            ticker = ib.reqMktData(spx_contract, "", False, False)

            # Poll up to 5 s for a valid SPX price
            deadline = asyncio.get_running_loop().time() + 5.0
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.3)
                _last  = getattr(ticker, "last",  None)
                _close = getattr(ticker, "close", None)
                _price = _last if (_last and _last > 0) else _close
                if _price and 4000 < _price < 9_000:
                    break

            try:
                ib.cancelMktData(spx_contract)
            except Exception:
                pass

            raw_last  = float(getattr(ticker, "last",  0.0) or 0.0)
            raw_close = float(getattr(ticker, "close", 0.0) or 0.0)
            price = raw_last if (raw_last > 0) else raw_close
            if 4000 < price < 9_000:
                LOGGER.info("SPX price from TWS socket: %.2f", price)
                # Cache on client so client.get_spx_price() reuses it immediately
                try:
                    from datetime import datetime as _dt
                    self.client.spx_price = price
                    self.client.spx_price_timestamp = _dt.now()
                except Exception:
                    pass
                return float(price)

        except Exception as exc:
            LOGGER.debug("TWS socket SPX price fetch failed: %s", exc)
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass
        return 0.0

    async def _fetch_account_summary_via_tws_socket(self, account_id: str) -> dict:
        """Fetch account summary (NLV, margin, buying power) from TWS via ib_async.

        Returns a dict in the same format as IBKRClient.get_account_summary():
          {tag_lower: {"amount": value_str, "currency": ccy}}
        so the dashboard _to_float() helper can parse it unchanged.
        """
        try:
            from ib_async import IB
        except ImportError:
            LOGGER.warning("ib_async not installed – cannot fetch account summary via TWS socket")
            return {}

        host = os.getenv("IB_SOCKET_HOST", "127.0.0.1")
        port = int(os.getenv("IB_SOCKET_PORT", "7496"))
        client_id = int(os.getenv("IB_ACCT_SUMMARY_CLIENT_ID", "13"))

        ib = IB()
        result: dict = {}
        try:
            await ib.connectAsync(host=host, port=port, clientId=client_id)
            # Subscribe to account updates and wait for values to arrive
            await ib.reqAccountUpdatesAsync(account=account_id)
            await asyncio.sleep(1.5)  # let AccountValue events arrive
            values = ib.accountValues(account=account_id)
            for av in values:
                key = av.tag.lower() if av.tag else ""
                if key:
                    result[key] = {"amount": av.value, "currency": av.currency}
            LOGGER.info(
                "TWS account summary: fetched %d tags for account %s",
                len(result), account_id,
            )
        except Exception as exc:
            LOGGER.warning("TWS account summary fetch failed: %s", exc)
            return {}
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass
        return result

    def get_account_summary(self, account_id: str) -> dict:
        """Sync wrapper: fetch account summary from TWS socket (SOCKET mode) or
        Client Portal REST (PORTAL mode), with automatic fallback."""
        _api_mode = os.getenv("IB_API_MODE", "PORTAL").split("#")[0].strip().upper()
        if _api_mode == "SOCKET":
            try:
                summary = asyncio.run(self._fetch_account_summary_via_tws_socket(account_id))
                if summary:
                    return summary
                LOGGER.warning("TWS socket account summary empty; falling back to Client Portal")
            except RuntimeError:
                # Already inside a running event loop – use asyncio.to_thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(asyncio.run, self._fetch_account_summary_via_tws_socket(account_id))
                    try:
                        summary = fut.result(timeout=20)
                        if summary:
                            return summary
                    except Exception as exc:
                        LOGGER.warning("TWS socket account summary (thread) failed: %s", exc)
            except Exception as exc:
                LOGGER.warning("TWS socket account summary sync failed: %s", exc)
        # Fall back to Client Portal REST API
        return self.client.get_account_summary(account_id)

    async def _fetch_greeks_via_tws_socket(
        self,
        options: list[tuple["UnifiedPosition", int]],
    ) -> dict[int, dict]:
        """Fetch per-contract Greeks from TWS via ib_async modelGreeks.

        Builds ib_async Contract objects directly from position metadata so
        there is no portfolio() sync wait or qualifyContractsAsync round-trip
        (the previous approach left 192/266 positions unenriched because the
        8-second portfolio sync only returned a subset of open positions and
        qualifyContractsAsync silently failed for the rest).

        Returns {conid: {'delta','gamma','theta','vega','iv','source'}} for
        every contract that returned modelGreeks within the polling window.
        """
        try:
            from ib_async import IB, Contract
        except ImportError:
            LOGGER.warning("ib_async not installed – cannot fetch Greeks via TWS socket")
            return {}

        # ── Exchange lookup for futures options ───────────────────────────────
        _FOP_EXCHANGE: dict[str, str] = {
            "ES": "CME",  "MES": "CME", "NQ": "CME",  "MNQ": "CME",
            "RTY": "CME", "M2K": "CME",
            "YM": "CBOT", "MYM": "CBOT",
            "GC": "COMEX", "MGC": "COMEX", "SI": "COMEX", "HG": "COMEX",
            "CL": "NYMEX", "QM": "NYMEX", "NG": "NYMEX",
            "ZB": "CBOT",  "ZN": "CBOT",  "ZF": "CBOT", "ZT": "CBOT",
            "6E": "CME",  "6B": "CME",  "6J": "CME", "6A": "CME", "6C": "CME",
            "ZC": "CBOT",  "ZS": "CBOT",  "ZW": "CBOT",
            "PL": "NYMEX",
        }

        def _build_contract(pos: "UnifiedPosition", cid: int) -> Contract:
            """Construct a fully-specified Contract from position metadata."""
            right    = "C" if (pos.option_type or "").lower().startswith("c") else "P"
            expiry   = str(pos.expiration).replace("-", "")[:8] if pos.expiration else ""
            undl     = (pos.underlying or "").upper().split()[0]  # strip any suffix
            mult = str(int(pos.contract_multiplier)) if pos.contract_multiplier else ""
            if undl in _FOP_EXCHANGE:
                sec_type = "FOP"
                exchange = _FOP_EXCHANGE[undl]
                return Contract(
                    conId    = cid,
                    secType  = sec_type,
                    symbol   = undl,
                    lastTradeDateOrContractMonth = expiry,
                    strike   = float(pos.strike or 0.0),
                    right    = right,
                    exchange = exchange,
                    currency = "USD",
                    multiplier = mult,
                )
            # For equity/index options, include full option metadata so TWS can
            # resolve option-computation ticks reliably in SOCKET mode.
            return Contract(
                conId=cid,
                secType="OPT",
                symbol=undl,
                lastTradeDateOrContractMonth=expiry,
                strike=float(pos.strike or 0.0),
                right=right,
                exchange="SMART",
                currency="USD",
                multiplier=mult,
            )

        # ── Filter: skip truly expired / zero-qty (belt-and-suspenders) ──────
        active = [
            (pos, cid) for pos, cid in options
            if abs(float(pos.quantity or 0.0)) > 1e-9
            and pos.strike
            and pos.expiration
            and (pos.days_to_expiration is None or int(pos.days_to_expiration) >= 0)
        ]
        if not active:
            return {}

        host      = os.getenv("IB_SOCKET_HOST", "127.0.0.1")
        port      = int(os.getenv("IB_SOCKET_PORT", "7496"))
        client_id = int(os.getenv("IB_GREEKS_CLIENT_ID", "12"))
        poll_secs = float(os.getenv("IB_GREEKS_POLL_SECS", "45"))
        market_data_type = int(os.getenv("IB_GREEKS_MARKET_DATA_TYPE", "3"))

        ib = IB()
        built_contracts: list[Contract] = []
        result: dict[int, dict] = {}
        try:
            await ib.connectAsync(host=host, port=port, clientId=client_id)
            try:
                ib.reqMarketDataType(market_data_type)
            except Exception:
                pass
            LOGGER.info(
                "IBKRAdapter Greeks socket connected (clientId=%d, %d options to enrich)",
                client_id, len(active),
            )

            contract_by_cid: dict[int, Contract] = {}
            for pos, cid in active:
                c = _build_contract(pos, cid)
                built_contracts.append(c)
                contract_by_cid[cid] = c

            # Track conids that fired Error 10091 (subscription required) — remove
            # them from pending immediately so the 45s poll window is not wasted.
            no_subscription_conids: set[int] = set()

            def _on_error(reqId: int, errorCode: int, errorString: str, contract: Any, *args: Any) -> None:  # noqa: ANN401
                if errorCode == 10091 and contract is not None:
                    cid = getattr(contract, "conId", None)
                    if cid:
                        no_subscription_conids.add(int(cid))

            ib.errorEvent += _on_error

            base_types = [market_data_type, 1, 2, 3, 4]
            requested_types: list[int] = []
            for mdt in base_types:
                if mdt not in requested_types:
                    requested_types.append(mdt)

            pending_conids = {cid for _, cid in active}
            enriched = 0
            for mdt in requested_types:
                if not pending_conids:
                    break

                try:
                    ib.reqMarketDataType(mdt)
                except Exception:
                    pass

                tickers: list[tuple[int, Any]] = []
                for c, (pos, cid) in zip(built_contracts, active, strict=False):
                    if cid not in pending_conids:
                        continue
                    ticker = ib.reqMktData(c, "", False, False)
                    tickers.append((cid, ticker))

                loop = asyncio.get_running_loop()
                deadline = loop.time() + poll_secs
                pending_idx = set(range(len(tickers)))
                while pending_idx and loop.time() < deadline:
                    await asyncio.sleep(0.2)
                    # Remove contracts that got 10091 — no subscription, won't deliver
                    pending_idx = {
                        i for i in pending_idx
                        if getattr(tickers[i][1], "modelGreeks", None) is None
                        and tickers[i][0] not in no_subscription_conids
                    }

                resolved: set[int] = set()
                for cid, ticker in tickers:
                    g = getattr(ticker, "modelGreeks", None)
                    if g is None:
                        continue
                    result[cid] = {
                        "delta": g.delta,
                        "gamma": g.gamma,
                        "theta": g.theta,
                        "vega": g.vega,
                        "iv": g.impliedVol,
                        "source": "tws_socket",
                    }
                    resolved.add(cid)

                if resolved:
                    enriched += len(resolved)
                    pending_conids -= resolved

                # Drop 10091 contracts from future MDT passes — won't get data without subscription
                if no_subscription_conids:
                    dropped = pending_conids & no_subscription_conids
                    if dropped:
                        LOGGER.debug(
                            "Dropping %d conids with Error 10091 (no subscription): will fall through to Tastytrade",
                            len(dropped),
                        )
                        pending_conids -= dropped

                for cid, ticker in tickers:
                    if cid in resolved:
                        try:
                            contract = contract_by_cid.get(cid)
                            if contract is not None:
                                ib.cancelMktData(contract)
                        except Exception:
                            pass

            for pos, cid in active:
                if cid in result:
                    continue
                LOGGER.debug(
                    "No modelGreeks for conid %d (%s) after %.0fs timeout across market data types",
                    cid,
                    pos.symbol,
                    poll_secs,
                )

            LOGGER.info(
                "TWS socket Greeks: %d/%d enriched via modelGreeks%s",
                enriched, len(active),
                f", {len(no_subscription_conids)} skipped (Error 10091 – no subscription)" if no_subscription_conids else "",
            )
        except Exception as exc:
            LOGGER.warning("TWS socket Greek fetch failed: %s", exc)
        finally:
            for c in built_contracts:
                try:
                    ib.cancelMktData(c)
                except Exception:
                    pass
            try:
                ib.disconnect()
            except Exception:
                pass

        return result

    def _build_prefetch_targets(self, positions: list[UnifiedPosition]) -> dict[str, set[tuple[str, float, str]]]:
        """Build per-underlying option requests used for cache prewarming."""

        targets: dict[str, set[tuple[str, float, str]]] = {}
        for position in positions:
            if (
                position.instrument_type != InstrumentType.OPTION
                or not position.underlying
                or position.expiration is None
                or position.strike is None
                or not position.option_type
            ):
                continue

            for tasty_underlying in self._candidate_tasty_underlyings(position):
                targets.setdefault(tasty_underlying, set()).add(
                    (
                        position.expiration.strftime("%Y-%m-%d"),
                        float(position.strike),
                        position.option_type,
                    )
                )
        return targets

    @staticmethod
    def to_stream_snapshot_payload(position: UnifiedPosition, account_id: str) -> dict[str, Any]:
        return {
            "broker": "ibkr",
            "account_id": account_id,
            "contract_key": position.symbol,
            "underlying": position.underlying,
            "expiration": position.expiration.isoformat() if position.expiration else None,
            "strike": position.strike,
            "option_type": position.option_type,
            "quantity": position.quantity,
            "delta": position.delta,
            "gamma": position.gamma,
            "theta": position.theta,
            "vega": position.vega,
            "iv": position.iv,
            "event_time": datetime.utcnow().isoformat(),
        }

    def _candidate_tasty_underlyings(self, position: UnifiedPosition) -> list[str]:
        """Return candidate underlyings used for Tastytrade Greeks lookup."""

        primary = self._resolve_tasty_underlying(position)
        candidates = [primary] if primary else []

        base_underlying = str(position.underlying or "").upper().lstrip("/")
        if base_underlying and base_underlying not in candidates:
            candidates.append(base_underlying)
        if self._is_futures_option(position) and base_underlying:
            base_candidate = f"/{base_underlying}"
            if base_candidate not in candidates:
                candidates.append(base_candidate)

        symbol_text = str(position.symbol or "").upper().strip()
        if symbol_text:
            parts = symbol_text.replace("(", " ").replace(")", " ").replace("-", " ").split()
            if parts:
                root = parts[0].lstrip("/")
                if root and root not in candidates:
                    candidates.append(root)
                if self._is_futures_option(position):
                    fut_root = f"/{root}"
                    if fut_root not in candidates:
                        candidates.append(fut_root)

        seen: set[str] = set()
        deduped: list[str] = []
        for candidate in candidates:
            normalized = str(candidate or "").strip().upper()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)

        return deduped

    def _resolve_tasty_underlying(self, position: UnifiedPosition) -> str:
        """Map a normalized option position into a Tastytrade underlying symbol."""

        underlying = str(position.underlying or "").upper().lstrip("/")
        if not underlying:
            symbol_text = str(position.symbol or "").upper().strip()
            if symbol_text:
                parts = symbol_text.replace("(", " ").replace(")", " ").replace("-", " ").split()
                if parts:
                    underlying = parts[0].lstrip("/")

        if not self._is_futures_option(position):
            return underlying

        symbol_text = str(position.symbol or "")
        match = re.search(r"\(([^)]+)\)", symbol_text)
        if match:
            weekly_root = match.group(1).strip().upper()
            if weekly_root:
                return f"/{weekly_root}"

        return underlying

    def _to_unified_position(self, position: dict[str, Any]) -> UnifiedPosition:
        """Transform IBKR payload into UnifiedPosition."""

        quantity = float(position.get("position", 0.0))
        avg_price = float(position.get("avgCost", 0.0))
        market_value = float(position.get("mktValue", 0.0))
        unrealized_pnl = float(position.get("unrealizedPnl", 0.0))
        contract_multiplier = self._extract_contract_multiplier(position)

        if self.client.is_option_contract(position):
            underlying, expiry, strike, option_type, _ = self.client._extract_option_details(position)
            expiration = self._parse_expiration(expiry)
            days_to_expiration = (expiration - date.today()).days if expiration else None
            native_greeks = self._extract_native_greeks(position)
            contract_multiplier = contract_multiplier or 1.0
            delta_gamma_multiplier = contract_multiplier if contract_multiplier > 0 else 1.0
            theta_vega_multiplier = contract_multiplier if contract_multiplier > 0 else 1.0

            native_delta = native_greeks.get("delta")
            native_gamma = native_greeks.get("gamma")
            native_theta = native_greeks.get("theta")
            native_vega = native_greeks.get("vega")
            has_native = any(value is not None for value in (native_delta, native_gamma, native_theta, native_vega))

            return UnifiedPosition(
                symbol=str(position.get("contractDesc") or position.get("ticker") or ""),
                instrument_type=InstrumentType.OPTION,
                broker="ibkr",
                quantity=quantity,
                contract_multiplier=contract_multiplier,
                avg_price=avg_price,
                market_value=market_value,
                unrealized_pnl=unrealized_pnl,
                underlying=underlying,
                strike=strike,
                expiration=expiration,
                option_type=option_type,
                days_to_expiration=days_to_expiration,
                delta=(native_delta or 0.0) * quantity * delta_gamma_multiplier,
                gamma=(native_gamma or 0.0) * quantity * delta_gamma_multiplier,
                theta=(native_theta or 0.0) * quantity * theta_vega_multiplier,
                vega=(native_vega or 0.0) * quantity * theta_vega_multiplier,
                greeks_source="ibkr_native" if has_native else "none",
                broker_id=str(position.get("conid", "") or ""),
            )

        ticker = str(position.get("ticker") or position.get("contractDesc") or "UNKNOWN")
        asset_class = str(position.get("assetClass") or "").upper()
        conid_raw = position.get("conid", "")
        multiplier = contract_multiplier if contract_multiplier > 0 else 1.0
        is_futures = asset_class == "FUT"
        base_delta = quantity * multiplier if is_futures else quantity
        spx_delta = self.client.calculate_spx_weighted_delta(
            symbol=ticker,
            position_qty=quantity,
            price=float(position.get("mktPrice", 0.0)),
            underlying_delta=1.0,
            multiplier=multiplier if is_futures else 1.0,
        )

        return UnifiedPosition(
            symbol=ticker,
            instrument_type=InstrumentType.FUTURE if is_futures else InstrumentType.EQUITY,
            broker="ibkr",
            quantity=quantity,
            contract_multiplier=contract_multiplier,
            avg_price=avg_price,
            market_value=market_value,
            unrealized_pnl=unrealized_pnl,
            delta=base_delta,
            spx_delta=spx_delta,
            greeks_source="ibkr_native",
            broker_id=str(conid_raw or ""),
        )

    def _extract_native_greeks(self, position: dict[str, Any]) -> dict[str, float | None]:
        candidates = {
            "delta": ["delta", "positionDelta", "netDelta", "optDelta", "modelDelta"],
            "gamma": ["gamma", "positionGamma", "optGamma", "modelGamma"],
            "theta": ["theta", "positionTheta", "optTheta", "modelTheta"],
            "vega": ["vega", "positionVega", "optVega", "modelVega"],
        }

        extracted: dict[str, float | None] = {"delta": None, "gamma": None, "theta": None, "vega": None}
        for greek_name, keys in candidates.items():
            for key in keys:
                if key not in position:
                    continue
                value = self._safe_optional_float(position.get(key))
                if value is not None:
                    extracted[greek_name] = value
                    break
        return extracted

    def _extract_contract_multiplier(self, position: dict[str, Any]) -> float:
        raw_multiplier = position.get("multiplier")
        try:
            if raw_multiplier not in (None, ""):
                parsed = float(raw_multiplier)
                if parsed > 0:
                    return parsed
        except (TypeError, ValueError):
            pass

        asset_class = str(position.get("assetClass") or "").upper()
        ticker = str(position.get("ticker") or position.get("undSym") or "").upper().lstrip("/")

        if asset_class in {"OPT", "FOP"}:
            if not ticker:
                # FOP positions from IBKR API lack ticker/undSym; parse from contractDesc.
                _desc = str(position.get("contractDesc") or "").upper().strip()
                _tm = re.match(r'^/?([A-Z]{1,6})\b', _desc)
                if _tm:
                    ticker = _tm.group(1).lstrip("/")
            if ticker in {"ES", "SPX", "SPXW"}:
                return 50.0
            if ticker in {"MES"}:
                return 5.0
            if ticker in {"NQ", "NQ100"}:
                return 20.0
            if ticker in {"MNQ"}:
                return 2.0
            return 100.0

        if asset_class == "FUT":
            if ticker == "ES":
                return 50.0
            if ticker == "MES":
                return 5.0

        return 1.0

    @staticmethod
    def _is_futures_option(position: UnifiedPosition) -> bool:
        underlying = str(position.underlying or "").upper().lstrip("/")
        futures_roots = {
            "ES",
            "MES",
            "NQ",
            "MNQ",
            "YM",
            "MYM",
            "RTY",
            "M2K",
            "CL",
            "NG",
            "GC",
            "SI",
            "ZB",
            "ZN",
            "ZF",
        }
        return underlying in futures_roots

    def _option_delta_gamma_multiplier(self, position: UnifiedPosition, contract_multiplier: float) -> float:
        if self._is_futures_option(position):
            return 1.0
        return contract_multiplier if contract_multiplier > 0 else 100.0

    def _option_theta_vega_multiplier(self, position: UnifiedPosition, contract_multiplier: float) -> float:
        if contract_multiplier > 0:
            return contract_multiplier
        if self._is_futures_option(position):
            return 1.0
        return 100.0

    @staticmethod
    def _is_stock_option(position: UnifiedPosition) -> bool:
        return (
            position.instrument_type == InstrumentType.OPTION
            and not IBKRAdapter._is_futures_option(position)
        )

    def _stock_option_cache_key(self, position: UnifiedPosition) -> str:
        broker_id = str(getattr(position, "broker_id", "") or "").strip()
        if broker_id:
            return f"conid:{broker_id}"
        expiration = position.expiration.isoformat() if position.expiration else ""
        strike = f"{float(position.strike):.6f}" if position.strike is not None else ""
        return "|".join(
            [
                str(position.underlying or "").upper().lstrip("/"),
                expiration,
                strike,
                str(position.option_type or "").upper(),
            ]
        )

    def _load_stock_option_greeks_cache(self) -> dict[str, dict[str, Any]]:
        path = self._stock_option_greeks_cache_path
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.debug("Unable to read stock option Greeks cache %s: %s", path, exc)
            return {}
        if not isinstance(raw, dict):
            return {}

        normalized: dict[str, dict[str, Any]] = {}
        for key, payload in raw.items():
            if not isinstance(key, str) or not isinstance(payload, dict):
                continue
            normalized[key] = {
                "delta": self._safe_optional_float(payload.get("delta")),
                "gamma": self._safe_optional_float(payload.get("gamma")),
                "theta": self._safe_optional_float(payload.get("theta")),
                "vega": self._safe_optional_float(payload.get("vega")),
                "iv": self._safe_optional_float(payload.get("iv")),
                "updated_at": str(payload.get("updated_at") or ""),
            }
        return normalized

    def _save_stock_option_greeks_cache(self) -> None:
        path = self._stock_option_greeks_cache_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._stock_option_greeks_cache, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception as exc:
            LOGGER.debug("Unable to write stock option Greeks cache %s: %s", path, exc)

    def _update_stock_option_greeks_cache(self, position: UnifiedPosition) -> None:
        if not self._is_stock_option(position):
            return

        has_value = any(
            self._safe_optional_float(value) is not None
            for value in (position.delta, position.gamma, position.theta, position.vega)
        )
        if not has_value:
            return

        key = self._stock_option_cache_key(position)
        self._stock_option_greeks_cache[key] = {
            "delta": float(position.delta),
            "gamma": float(position.gamma),
            "theta": float(position.theta),
            "vega": float(position.vega),
            "iv": self._safe_optional_float(position.iv),
            "updated_at": datetime.utcnow().isoformat(),
        }
        self._save_stock_option_greeks_cache()

    def _apply_stock_option_cache(self, position: UnifiedPosition) -> bool:
        if not self._is_stock_option(position):
            return False

        payload = self._stock_option_greeks_cache.get(self._stock_option_cache_key(position))
        if not payload:
            return False

        delta = self._safe_optional_float(payload.get("delta"))
        gamma = self._safe_optional_float(payload.get("gamma"))
        theta = self._safe_optional_float(payload.get("theta"))
        vega = self._safe_optional_float(payload.get("vega"))
        if delta is None and gamma is None and theta is None and vega is None:
            return False

        position.delta = float(delta or 0.0)
        position.gamma = float(gamma or 0.0)
        position.theta = float(theta or 0.0)
        position.vega = float(vega or 0.0)
        position.iv = self._safe_optional_float(payload.get("iv"))
        position.greeks_source = "stock_option_cache"
        return True

    # ─── What-If margin simulation (FR-005 / T005-T006) ─────────────────────

    async def simulate_margin_impact(
        self,
        account_id: str,
        legs: list[dict],
    ) -> dict[str, float]:
        """Simulate the margin impact of a multi-leg option combo via What-If.

        Returns a dict with keys:
            init_margin_change   – Initial Margin delta (positive = increases margin)
            maint_margin_change  – Maintenance Margin delta

        Dispatches to SOCKET or PORTAL mode based on IB_API_MODE env var.

        Args:
            account_id: IBKR account ID (e.g. "U1234567").
            legs: list of leg dicts, each with:
                  {conId: int, action: "BUY"|"SELL", quantity: int,
                   exchange: str (optional, default "SMART")}

        Raises:
            ValueError: if legs is empty.
            RuntimeError: if the What-If call fails after retries.
        """
        if not legs:
            raise ValueError("simulate_margin_impact requires at least one leg")

        _api_mode = os.getenv("IB_API_MODE", "PORTAL").split("#")[0].strip().upper()

        if _api_mode == "SOCKET":
            return await self._simulate_margin_impact_socket(account_id, legs)
        return await self._simulate_margin_impact_portal(account_id, legs)

    async def _simulate_margin_impact_socket(
        self,
        account_id: str,
        legs: list[dict],
    ) -> dict[str, float]:
        """SOCKET implementation: uses ib_async whatIfOrder with a Bag contract."""
        try:
            from ib_async import IB, Contract, ComboLeg, MarketOrder, Order
        except ImportError:
            LOGGER.warning("ib_async not installed – simulate_margin_impact unavailable")
            return {"init_margin_change": 0.0, "maint_margin_change": 0.0}

        host      = os.getenv("IB_SOCKET_HOST", "127.0.0.1")
        port      = int(os.getenv("IB_SOCKET_PORT", "7496"))
        client_id = int(os.getenv("IB_WHATIF_CLIENT_ID", "15"))  # distinct from acct-summary (13)

        ib = IB()
        try:
            await ib.connectAsync(host=host, port=port, clientId=client_id, timeout=10.0)

            import asyncio as _asyncio

            if len(legs) == 1:
                # Single-leg: submit the FOP/OPT contract directly — BAG hangs on WhatIf
                leg = legs[0]
                contract = Contract(conId=int(leg["conId"]))
                contract.exchange = leg.get("exchange", "SMART")
                _action   = leg["action"].upper()
                _quantity = int(leg.get("quantity", 1))
                _limit_price = legs[0].get("limit_price") if legs else None
                if _limit_price is not None:
                    from ib_async import LimitOrder as _LimitOrder
                    order = _LimitOrder(_action, _quantity, lmtPrice=float(_limit_price))
                else:
                    order = MarketOrder(_action, _quantity)
                order.whatIf = True
            else:
                # Multi-leg: build a BAG combo contract
                contract = Contract()
                contract.symbol   = legs[0].get("symbol", "ES")
                contract.secType  = "BAG"
                contract.currency = "USD"
                _first_sym = str(legs[0].get("symbol", "")).upper()
                contract.exchange = "CME" if _first_sym in {"ES", "MES"} else "SMART"

                combo_legs: list[ComboLeg] = []
                for leg in legs:
                    cl          = ComboLeg()
                    cl.conId    = int(leg["conId"])
                    cl.ratio    = int(leg.get("quantity", 1))
                    cl.action   = leg["action"].upper()
                    cl.exchange = leg.get("exchange", "SMART")
                    combo_legs.append(cl)

                contract.comboLegs = combo_legs
                # Overall order action: SELL if all legs SELL, else BUY
                _actions = [l["action"].upper() for l in legs]
                _bag_action = "SELL" if all(a == "SELL" for a in _actions) else "BUY"
                _bag_price = legs[0].get("limit_price") if legs else None
                if _bag_price is not None:
                    from ib_async import LimitOrder as _LimitOrder
                    order = _LimitOrder(_bag_action, 1, lmtPrice=float(_bag_price))
                else:
                    order = MarketOrder(_bag_action, 1)
                order.whatIf = True

            # Wrap in asyncio timeout so we never hang indefinitely
            order_state = await _asyncio.wait_for(
                ib.whatIfOrderAsync(contract, order),
                timeout=30.0,
            )

            init_change  = self._safe_float(getattr(order_state, "initMarginChange",  None))
            maint_change = self._safe_float(getattr(order_state, "maintMarginChange", None))
            LOGGER.info(
                "simulate_margin_impact SOCKET: init=%s maint=%s",
                init_change, maint_change,
            )
            return {
                "init_margin_change":  init_change,
                "maint_margin_change": maint_change,
            }

        except Exception as exc:
            LOGGER.warning(
                "simulate_margin_impact SOCKET failed: %s: %s",
                type(exc).__name__, exc,
            )
            return {"init_margin_change": 0.0, "maint_margin_change": 0.0}
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    async def _simulate_margin_impact_portal(
        self,
        account_id: str,
        legs: list[dict],
    ) -> dict[str, float]:
        """PORTAL implementation: POST to /v1/api/iserver/account/{acctId}/orders/whatif."""
        import aiohttp
        import ssl

        base_url = os.getenv("IBKR_PORTAL_BASE_URL", "https://localhost:5001")
        url      = f"{base_url}/v1/api/iserver/account/{account_id}/orders/whatif"

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode    = ssl.CERT_NONE

        combo_legs_payload = [
            {
                "conid":    int(leg["conId"]),
                "side":     leg["action"].upper(),
                "quantity": int(leg.get("quantity", 1)),
            }
            for leg in legs
        ]

        payload = {
            "orders": [
                {
                    "conid":        0,
                    "secType":      "BAG",
                    "orderType":    "MKT",
                    "side":         "BUY",
                    "quantity":     1,
                    "comboLegs":    combo_legs_payload,
                    "tif":          "DAY",
                }
            ]
        }

        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        json=payload,
                        ssl=ssl_ctx,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            LOGGER.warning(
                                "simulate_margin_impact PORTAL HTTP %d (attempt %d): %s",
                                resp.status, attempt + 1, body[:200],
                            )
                            if attempt < 2:
                                await asyncio.sleep(2 ** attempt)
                                continue
                            return {"init_margin_change": 0.0, "maint_margin_change": 0.0}
                        data = await resp.json(content_type=None)
                        orders = data if isinstance(data, list) else data.get("orders", [data])
                        first = orders[0] if orders else {}
                        init_change  = self._safe_float(first.get("initMarginChange"))
                        maint_change = self._safe_float(first.get("maintMarginChange"))
                        LOGGER.info(
                            "simulate_margin_impact PORTAL: init=%s maint=%s",
                            init_change, maint_change,
                        )
                        return {
                            "init_margin_change":  init_change,
                            "maint_margin_change": maint_change,
                        }
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "simulate_margin_impact PORTAL attempt %d failed: %s",
                    attempt + 1, exc,
                )
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

        return {"init_margin_change": 0.0, "maint_margin_change": 0.0}

    # ─── Options chain (bid/ask) via TWS ────────────────────────────────────

    async def fetch_option_expirations_tws(
        self,
        underlying: str,
        dte_min: int = 0,
        dte_max: int = 180,
    ) -> list[dict[str, Any]]:
        import math

        _allowlist = {"SPX", "SPY", "ES", "MES", "QQQ", "NDX", "RUT"}
        if underlying not in _allowlist:
            return []

        try:
            from ib_async import IB, Contract
        except ImportError:
            LOGGER.warning("ib_async not installed – option chain unavailable")
            return []

        host = os.getenv("IB_SOCKET_HOST", "127.0.0.1")
        port = int(os.getenv("IB_SOCKET_PORT", "7496"))
        client_id = int(os.getenv("IB_CHAIN_CLIENT_ID", "19"))
        connect_timeout = float(os.getenv("IB_CHAIN_CONNECT_TIMEOUT", "10.0"))

        _spec: dict[str, dict[str, Any]] = {
            "SPX": {"secType": "IND", "symbol": "SPX", "exchange": "CBOE", "currency": "USD", "undSecType": "IND", "futFopExchange": "", "multiplier": 100},
            "SPY": {"secType": "STK", "symbol": "SPY", "exchange": "SMART", "currency": "USD", "primaryExch": "ARCA", "undSecType": "STK", "futFopExchange": "", "multiplier": 100},
            "QQQ": {"secType": "STK", "symbol": "QQQ", "exchange": "SMART", "currency": "USD", "primaryExch": "NASDAQ", "undSecType": "STK", "futFopExchange": "", "multiplier": 100},
            "NDX": {"secType": "IND", "symbol": "NDX", "exchange": "NASDAQ", "currency": "USD", "undSecType": "IND", "futFopExchange": "", "multiplier": 100},
            "RUT": {"secType": "IND", "symbol": "RUT", "exchange": "RUSSELL", "currency": "USD", "undSecType": "IND", "futFopExchange": "", "multiplier": 100},
            "ES": {"secType": "FUT", "symbol": "ES", "exchange": "CME", "currency": "USD", "undSecType": "FUT", "futFopExchange": "CME", "multiplier": 50},
            "MES": {"secType": "FUT", "symbol": "MES", "exchange": "CME", "currency": "USD", "undSecType": "FUT", "futFopExchange": "CME", "multiplier": 5},
        }

        spec = dict(_spec[underlying])
        if underlying in {"ES", "MES"}:
            today = datetime.utcnow().date()
            q_months = [3, 6, 9, 12]
            next_q = next((m for m in q_months if (today.year, m) >= (today.year, today.month)), None)
            year = today.year if next_q is not None else today.year + 1
            month = next_q if next_q is not None else 3
            spec["lastTradeDateOrContractMonth"] = f"{year}{month:02d}"

        ib = IB()
        try:
            await ib.connectAsync(host=host, port=port, clientId=client_id, timeout=connect_timeout)
            base_fields = {
                k: v for k, v in spec.items()
                if k in {"secType", "symbol", "exchange", "currency", "lastTradeDateOrContractMonth", "primaryExch"} and v
            }
            und_contract = Contract(**base_fields)
            qualified = await ib.qualifyContractsAsync(und_contract)
            if not qualified:
                return []
            und_conid = qualified[0].conId

            chains = await ib.reqSecDefOptParamsAsync(
                underlyingSymbol=underlying,
                futFopExchange=spec.get("futFopExchange", ""),
                underlyingSecType=spec["undSecType"],
                underlyingConId=und_conid,
            )
            if not chains:
                return []

            _preferred_exch_map = {
                "ES": "CME", "MES": "CME",
                "SPX": "CBOE", "NDX": "CBOE", "RUT": "CBOE",
            }
            preferred_exchange = _preferred_exch_map.get(underlying, "SMART")
            chain = next((c for c in chains if c.exchange == preferred_exchange), chains[0])

            today = datetime.utcnow().date()
            expiries: list[dict[str, Any]] = []
            for exp in sorted(chain.expirations):
                try:
                    exp_date = datetime.strptime(exp, "%Y%m%d").date()
                except ValueError:
                    continue
                dte = (exp_date - today).days
                if dte_min <= dte <= dte_max:
                    expiries.append({
                        "expiry": exp,
                        "dte": dte,
                        "exchange": chain.exchange,
                        "tradingClass": getattr(chain, "tradingClass", underlying),
                        "multiplier": int(spec["multiplier"]),
                    })
            return expiries
        except Exception as exc:
            LOGGER.warning("fetch_option_expirations_tws failed for %s: %s", underlying, exc)
            return []
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    async def fetch_option_chain_matrix_tws(
        self,
        underlying: str,
        expiry: str,
        atm_price: float = 0.0,
        strikes_each_side: int = 6,
    ) -> list[dict[str, Any]]:
        import math

        _allowlist = {"SPX", "SPY", "ES", "MES", "QQQ", "NDX", "RUT"}
        if underlying not in _allowlist:
            return []

        try:
            from ib_async import IB, Contract
        except ImportError:
            LOGGER.warning("ib_async not installed – option chain unavailable")
            return []

        host = os.getenv("IB_SOCKET_HOST", "127.0.0.1")
        port = int(os.getenv("IB_SOCKET_PORT", "7496"))
        # Use a distinct client ID for chain-matrix fetches (separate from expiration lookup)
        client_id = int(os.getenv("IB_CHAIN_MATRIX_CLIENT_ID", os.getenv("IB_CHAIN_CLIENT_ID", "20")))
        poll_secs = float(os.getenv("IB_CHAIN_POLL_SECS", "4.0"))
        connect_timeout = float(os.getenv("IB_CHAIN_CONNECT_TIMEOUT", "10.0"))

        _spec: dict[str, dict[str, Any]] = {
            "SPX": {"secType": "IND", "symbol": "SPX", "exchange": "CBOE", "currency": "USD", "undSecType": "IND", "futFopExchange": "", "multiplier": 100},
            "SPY": {"secType": "STK", "symbol": "SPY", "exchange": "SMART", "currency": "USD", "primaryExch": "ARCA", "undSecType": "STK", "futFopExchange": "", "multiplier": 100},
            "QQQ": {"secType": "STK", "symbol": "QQQ", "exchange": "SMART", "currency": "USD", "primaryExch": "NASDAQ", "undSecType": "STK", "futFopExchange": "", "multiplier": 100},
            "NDX": {"secType": "IND", "symbol": "NDX", "exchange": "NASDAQ", "currency": "USD", "undSecType": "IND", "futFopExchange": "", "multiplier": 100},
            "RUT": {"secType": "IND", "symbol": "RUT", "exchange": "RUSSELL", "currency": "USD", "undSecType": "IND", "futFopExchange": "", "multiplier": 100},
            "ES": {"secType": "FUT", "symbol": "ES", "exchange": "CME", "currency": "USD", "undSecType": "FUT", "futFopExchange": "CME", "multiplier": 50},
            "MES": {"secType": "FUT", "symbol": "MES", "exchange": "CME", "currency": "USD", "undSecType": "FUT", "futFopExchange": "CME", "multiplier": 5},
        }

        spec = dict(_spec[underlying])
        if underlying in {"ES", "MES"}:
            today = datetime.utcnow().date()
            q_months = [3, 6, 9, 12]
            next_q = next((m for m in q_months if (today.year, m) >= (today.year, today.month)), None)
            year = today.year if next_q is not None else today.year + 1
            month = next_q if next_q is not None else 3
            spec["lastTradeDateOrContractMonth"] = f"{year}{month:02d}"

        ib = IB()
        rows: list[dict[str, Any]] = []
        try:
            await ib.connectAsync(host=host, port=port, clientId=client_id, timeout=connect_timeout)
            try:
                ib.reqMarketDataType(3)
            except Exception:
                pass

            base_fields = {
                k: v for k, v in spec.items()
                if k in {"secType", "symbol", "exchange", "currency", "lastTradeDateOrContractMonth", "primaryExch"} and v
            }
            und_contract = Contract(**base_fields)
            qualified = await ib.qualifyContractsAsync(und_contract)
            if not qualified:
                return []
            und_conid = qualified[0].conId

            chains = await ib.reqSecDefOptParamsAsync(
                underlyingSymbol=underlying,
                futFopExchange=spec.get("futFopExchange", ""),
                underlyingSecType=spec["undSecType"],
                underlyingConId=und_conid,
            )
            if not chains:
                return []

            # Pick the most appropriate exchange for each underlying
            _preferred_exch_map = {
                "ES": "CME", "MES": "CME",
                "SPX": "CBOE", "NDX": "CBOE", "RUT": "CBOE",
            }
            preferred_exchange = _preferred_exch_map.get(underlying, "SMART")
            eligible = [c for c in chains if expiry in set(c.expirations)]
            if not eligible:
                return []
            chain = next((c for c in eligible if c.exchange == preferred_exchange), eligible[0])

            strikes_all = sorted(float(s) for s in chain.strikes)
            if not strikes_all:
                return []
            if atm_price <= 0:
                atm_price = strikes_all[len(strikes_all) // 2]

            atm_idx = min(range(len(strikes_all)), key=lambda i: abs(strikes_all[i] - atm_price))
            start = max(0, atm_idx - strikes_each_side)
            end = min(len(strikes_all), atm_idx + strikes_each_side + 1)
            strikes = strikes_all[start:end]
            if not strikes:
                return []

            sec_type_opt = "FOP" if underlying in {"ES", "MES"} else "OPT"
            opt_exchange = "CME" if underlying in {"ES", "MES"} else "SMART"
            trading_class = getattr(chain, "tradingClass", None) or underlying
            multiplier = int(spec["multiplier"])

            contracts = []
            for strike in strikes:
                for right in ("P", "C"):
                    contracts.append(
                        Contract(
                            secType=sec_type_opt,
                            symbol=underlying,
                            lastTradeDateOrContractMonth=expiry,
                            strike=float(strike),
                            right=right,
                            exchange=opt_exchange,
                            currency="USD",
                            multiplier=str(multiplier),
                            tradingClass=trading_class,
                        )
                    )

            qualified_opts = await ib.qualifyContractsAsync(*contracts)
            if not qualified_opts:
                return []

            ticker_map: list[tuple[Any, Any]] = []
            for contract in qualified_opts:
                ticker = ib.reqMktData(contract, "", snapshot=True)
                ticker_map.append((contract, ticker))

            await asyncio.sleep(poll_secs)
            exp_date = datetime.strptime(expiry, "%Y%m%d").date()
            today = datetime.utcnow().date()
            dte = (exp_date - today).days

            for contract, ticker in ticker_map:
                raw_bid = getattr(ticker, "bid", None)
                raw_ask = getattr(ticker, "ask", None)
                raw_last = getattr(ticker, "last", None)
                bid = float(raw_bid) if raw_bid is not None and not math.isnan(float(raw_bid)) and float(raw_bid) > 0 else 0.0
                ask = float(raw_ask) if raw_ask is not None and not math.isnan(float(raw_ask)) and float(raw_ask) > 0 else 0.0
                last = float(raw_last) if raw_last is not None and not math.isnan(float(raw_last)) and float(raw_last) > 0 else 0.0
                mid = round((bid + ask) / 2, 4) if bid > 0 and ask > 0 else (ask or bid or last)

                greeks = (
                    getattr(ticker, "modelGreeks", None)
                    or getattr(ticker, "bidGreeks", None)
                    or getattr(ticker, "askGreeks", None)
                    or getattr(ticker, "lastGreeks", None)
                )

                rows.append({
                    "conId": int(getattr(contract, "conId", 0) or 0),
                    "symbol": underlying,
                    "strike": float(contract.strike),
                    "right": str(contract.right),
                    "expiry": expiry,
                    "dte": int(dte),
                    "bid": float(bid),
                    "ask": float(ask),
                    "last": float(last),
                    "mid": float(mid),
                    "delta": float(getattr(greeks, "delta", 0.0) or 0.0),
                    "gamma": float(getattr(greeks, "gamma", 0.0) or 0.0),
                    "theta": float(getattr(greeks, "theta", 0.0) or 0.0),
                    "vega": float(getattr(greeks, "vega", 0.0) or 0.0),
                    "iv": float(getattr(greeks, "impliedVol", 0.0) or 0.0),
                    "multiplier": multiplier,
                    "tradingClass": trading_class,
                })

            rows.sort(key=lambda r: (r["strike"], r["right"]))
            return rows
        except Exception as exc:
            LOGGER.warning("fetch_option_chain_matrix_tws failed for %s %s: %s", underlying, expiry, exc)
            return []
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    async def fetch_options_chain_tws(
        self,
        underlying: str,
        dte_min: int = 30,
        dte_max: int = 60,
        atm_price: float = 0.0,
        right: str = "P",
        n_strikes: int = 4,
    ) -> list[dict]:
        expirations = await self.fetch_option_expirations_tws(
            underlying=underlying,
            dte_min=dte_min,
            dte_max=dte_max,
        )
        if not expirations:
            return []

        target_dte = (dte_min + dte_max) // 2
        chosen = min(expirations, key=lambda x: abs(int(x.get("dte", 0)) - target_dte))
        expiry = str(chosen["expiry"])

        matrix = await self.fetch_option_chain_matrix_tws(
            underlying=underlying,
            expiry=expiry,
            atm_price=atm_price,
            strikes_each_side=n_strikes,
        )
        if not matrix:
            return []

        out = []
        for row in matrix:
            if str(row.get("right", "")).upper() != str(right).upper():
                continue
            out.append({
                "conId": row.get("conId", 0),
                "symbol": row.get("symbol", underlying),
                "strike": float(row.get("strike", 0.0)),
                "right": row.get("right", right),
                "dte": int(row.get("dte", 0)),
                "expiry": row.get("expiry", expiry),
                "bid": float(row.get("bid", 0.0)),
                "ask": float(row.get("ask", 0.0)),
                "mid": float(row.get("mid", 0.0)),
                "multiplier": int(row.get("multiplier", 100)),
                "tradingClass": row.get("tradingClass", underlying),
            })
        return out

    @staticmethod
    def _parse_expiration(expiry: str) -> date:
        normalized = expiry.replace("-", "").strip()
        if len(normalized) == 8:
            return datetime.strptime(normalized, "%Y%m%d").date()
        if len(normalized) == 6 and normalized.isdigit():
            # YYYYMM – produced for FOP contracts whose contractDesc encodes the
            # expiry as e.g. "FEB2026".  Use the last calendar day of that month
            # as the expiration date (exact weekly date is not critical for beta
            # weighting calculations).
            year, month = int(normalized[:4]), int(normalized[4:6])
            last_day = calendar.monthrange(year, month)[1]
            return date(year, month, last_day)
        raise ValueError(f"Unsupported expiration format: {expiry!r}")

    @staticmethod
    def _safe_float(value: Any) -> float:
        if value in (None, "N/A", ""):
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _safe_optional_float(value: Any) -> float | None:
        if value in (None, "N/A", ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
