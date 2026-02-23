from __future__ import annotations

import asyncio
import calendar
import logging
import os
import re
from datetime import date, datetime
from typing import Any

from adapters.base_adapter import BrokerAdapter
from ibkr_portfolio_client import IBKRClient
from models.order import PortfolioGreeks
from models.unified_position import InstrumentType, UnifiedPosition
from risk_engine.beta_weighter import BetaWeighter
from core.event_bus import get_event_bus
import json
import ssl

LOGGER = logging.getLogger(__name__)

class IBKRWebSocketClient:
    """
    Basic WebSocket client for IBKR CPAPI.
    Connects to wss://localhost:5001/v1/api/ws and publishes updates to EventBus.
    """
    def __init__(self, base_url: str = "wss://localhost:5001/v1/api/ws"):
        self.base_url = base_url
        self._running = False
        self._ws = None

    async def start(self):
        import websockets
        self._running = True
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        
        try:
            async with websockets.connect(self.base_url, ssl=ssl_context) as ws:
                self._ws = ws
                LOGGER.info("Connected to IBKR WebSocket")
                
                # Send initial session message
                await ws.send(json.dumps({"session": "portfolio_ws"}))
                
                while self._running:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    
                    # Publish to event bus
                    event_bus = get_event_bus()
                    if event_bus._running:
                        await event_bus.publish("market_data", data)
                        
        except Exception as e:
            LOGGER.error(f"IBKR WebSocket error: {e}")
            self._running = False

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()

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
        # BetaWeighter provides the SPX beta waterfall used by compute_portfolio_greeks().
        # Session is injected later if a Tastytrade session is available.
        self._beta_weighter = BetaWeighter(tastytrade_session=None, ibkr_client=self.client)

    async def fetch_positions(self, account_id: str) -> list[UnifiedPosition]:
        """Fetch IBKR positions and convert to unified schema."""
        if os.getenv("MOCK_IBKR") == "1":
            return [
                UnifiedPosition(
                    symbol="AAPL240621C200",
                    instrument_type=InstrumentType.OPTION,
                    broker="ibkr",
                    quantity=2,
                    avg_price=1,
                    market_value=1,
                    unrealized_pnl=0,
                    underlying="AAPL",
                    strike=200,
                    expiration=date(2026, 6, 21),
                    option_type="call",
                    iv=0.40,
                    gamma=1.5,
                    theta=25,
                    vega=80,
                )
            ]

        try:
            raw_positions = await asyncio.to_thread(self.client.get_positions, account_id)
        except Exception as exc:
            raise ConnectionError(f"Unable to fetch IBKR positions for account {account_id}.") from exc

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
            try:
                _ibkr_greeks = await asyncio.to_thread(
                    self.client.get_market_greeks_batch, _conids
                )
            except Exception as _exc:
                LOGGER.warning("IBKR snapshot greeks batch failed: %s", _exc)
                _ibkr_greeks = {}
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
                pos.greeks_source = "ibkr_snapshot"
                ibkr_snapshot_hits += 1

        LOGGER.debug(
            "IBKR snapshot greeks: %d/%d enriched, %d failed",
            ibkr_snapshot_hits,
            len(_options_needing_greeks),
            len(ibkr_snapshot_errors),
        )

        # Fetch SPX price once for the entire batch (used by BetaWeighter, T016)
        try:
            _spx_price_for_batch = await asyncio.to_thread(self.client.get_spx_price)
        except Exception as _exc:
            LOGGER.debug("Could not fetch SPX price for BetaWeighter batch: %s", _exc)
            _spx_price_for_batch = 0.0
        _spx_price_candidate = self._safe_optional_float(_spx_price_for_batch)
        _spx_price_for_batch_num = (
            float(_spx_price_candidate)
            if isinstance(_spx_price_candidate, (int, float))
            else 0.0
        )

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

            if position.greeks_source in {"ibkr_native", "ibkr_snapshot"}:
                # Already have IBKR-sourced greeks — still need SPX delta via BetaWeighter
                if _spx_price_for_batch_num > 0:
                    bw_result = await self._beta_weighter.compute_spx_equivalent_delta(
                        position, _spx_price_for_batch_num
                    )
                    position.spx_delta = bw_result.spx_equivalent_delta
                    position.beta_unavailable = bw_result.beta_unavailable
                updated_positions.append(position)
                continue

            if not position.underlying or not position.expiration or not position.strike or not position.option_type:
                updated_positions.append(position)
                continue

            # IBKR-only debug mode: never hit Tastytrade fallback.
            if self.disable_tasty_cache:
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
                        "lookup_candidates": "",
                        "lookup_used": "",
                        "reason": "ibkr_no_data_no_fallback",
                    }
                )
                position.greeks_source = "none"
                updated_positions.append(position)
                continue

            candidates = self._candidate_tasty_underlyings(position)
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
                        force_refresh_on_miss=self.force_refresh_on_miss,
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
        candidates = [primary]

        base_underlying = str(position.underlying or "").upper().lstrip("/")
        if self._is_futures_option(position) and base_underlying:
            base_candidate = f"/{base_underlying}"
            if base_candidate not in candidates:
                candidates.append(base_candidate)

        return candidates

    def _resolve_tasty_underlying(self, position: UnifiedPosition) -> str:
        """Map a normalized option position into a Tastytrade underlying symbol."""

        underlying = str(position.underlying or "").upper().lstrip("/")
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
