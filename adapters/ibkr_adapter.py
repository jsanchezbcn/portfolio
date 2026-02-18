from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any

from adapters.base_adapter import BrokerAdapter
from ibkr_portfolio_client import IBKRClient
from models.unified_position import InstrumentType, UnifiedPosition


LOGGER = logging.getLogger(__name__)


# Issue 9: Complete futures multiplier table (CME/CBOT standard contracts)
_FUTURES_MULTIPLIERS: dict[str, float] = {
    "ES": 50.0,
    "MES": 5.0,
    "NQ": 20.0,
    "MNQ": 2.0,
    "YM": 5.0,
    "MYM": 0.5,
    "RTY": 50.0,
    "M2K": 5.0,
    "CL": 1_000.0,
    "NG": 10_000.0,
    "GC": 100.0,
    "SI": 5_000.0,
    "ZB": 1_000.0,
    "ZN": 1_000.0,
    "ZF": 1_000.0,
    "ZT": 2_000.0,
}


class IBKRAdapter(BrokerAdapter):
    """IBKR-backed adapter that normalizes positions and enriches Greeks."""

    def __init__(self, client: IBKRClient | None = None) -> None:
        """Initialize adapter and read Greeks cache controls from environment."""

        self.client = client or IBKRClient()
        self.last_greeks_status: dict[str, Any] = {}
        # Issue 10: cache raw IBKR positions so the benchmark avoids a second REST call
        self._last_raw_positions: list[dict[str, Any]] = []
        disable_cache_raw = str(os.getenv("GREEKS_DISABLE_CACHE", "")).strip().lower()
        self.disable_tasty_cache = disable_cache_raw in {"1", "true", "yes", "on"}
        force_refresh_raw = str(os.getenv("GREEKS_FORCE_REFRESH_ON_MISS", "0")).strip().lower()
        self.force_refresh_on_miss = force_refresh_raw in {"1", "true", "yes", "on"}
        # Issue 11 (partial): enable prefetch by default so all options for the same
        # underlying are batched into a single DXLink websocket session rather than
        # reconnecting once per option.  Full persistent session pooling is tracked
        # separately in docs/IMPROVEMENTS.md #11.
        prefetch_raw = str(os.getenv("GREEKS_PREFETCH", "1")).strip().lower()
        self.enable_prefetch = prefetch_raw in {"1", "true", "yes", "on"}

    async def fetch_positions(self, account_id: str) -> list[UnifiedPosition]:
        """Fetch IBKR positions and convert to unified schema."""

        try:
            raw_positions = await asyncio.to_thread(self.client.get_positions, account_id)
        except Exception as exc:
            raise ConnectionError(f"Unable to fetch IBKR positions for account {account_id}.") from exc

        # Issue 10: cache so callers can reuse without a second HTTP round-trip
        self._last_raw_positions = raw_positions
        transformed: list[UnifiedPosition] = []

        for position in raw_positions:
            try:
                transformed.append(self._to_unified_position(position))
            except Exception as exc:
                message = str(exc)
                if "Option positions require fields: expiration" in message:
                    LOGGER.debug("Skipping IBKR option with missing expiration: %s", position)
                else:
                    LOGGER.warning("Skipping unparseable IBKR position payload: %s", exc)
                continue

        return transformed

    async def fetch_greeks(self, positions: list[UnifiedPosition]) -> list[UnifiedPosition]:
        """Enrich option positions with Greeks using cached/live Tastytrade data."""

        updated_positions: list[UnifiedPosition] = []
        prefetch_targets = self._build_prefetch_targets(positions)
        prefetch_results: dict[str, int] = {}
        missing_greeks_details: list[dict[str, Any]] = []

        if self.enable_prefetch and not self.disable_tasty_cache:
            for underlying, request_set in prefetch_targets.items():
                try:
                    fetched = await self.client.options_cache.fetch_and_cache_options_for_underlying(
                        underlying,
                        only_options=request_set,
                        force_refresh=self.force_refresh_on_miss,
                    )
                    prefetch_results[underlying] = len(fetched)
                except Exception as exc:
                    LOGGER.warning("Prefetch failed for %s: %s", underlying, exc)
                    prefetch_results[underlying] = 0

        cache_miss_count = 0

        for position in positions:
            if position.instrument_type != InstrumentType.OPTION:
                updated_positions.append(position)
                continue

            if position.greeks_source == "ibkr_native":
                updated_positions.append(position)
                continue

            if not position.underlying or not position.expiration or not position.strike or not position.option_type:
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

            price = position.market_value / position.quantity if position.quantity else 0.0
            if position.strike is not None and position.strike > 0:
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
            "cache_miss_count": cache_miss_count,
            "missing_greeks_details": missing_greeks_details,
            "last_session_error": getattr(self.client.options_cache, "last_session_error", None),
            "disable_tasty_cache": self.disable_tasty_cache,
            "force_refresh_on_miss": self.force_refresh_on_miss,
            "enable_prefetch": self.enable_prefetch,
        }

        return updated_positions

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
            "event_time": datetime.now(timezone.utc).isoformat(),
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
            )

        ticker = str(position.get("ticker") or position.get("contractDesc") or "UNKNOWN")
        spx_delta = self.client.calculate_spx_weighted_delta(
            symbol=ticker,
            position_qty=quantity,
            price=float(position.get("mktPrice", 0.0)),
            underlying_delta=1.0,
            multiplier=1.0,
        )

        return UnifiedPosition(
            symbol=ticker,
            instrument_type=InstrumentType.EQUITY,
            broker="ibkr",
            quantity=quantity,
            contract_multiplier=contract_multiplier,
            avg_price=avg_price,
            market_value=market_value,
            unrealized_pnl=unrealized_pnl,
            delta=quantity,
            spx_delta=spx_delta,
            greeks_source="ibkr_native",
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
        # Use IBKR's own multiplier field when present and valid
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

        # Issue 9: look up from the comprehensive futures multiplier table
        if asset_class in {"OPT", "FOP", "FUT"} and ticker in _FUTURES_MULTIPLIERS:
            return _FUTURES_MULTIPLIERS[ticker]

        # Equity options default
        if asset_class in {"OPT", "FOP"}:
            return 100.0

        return 1.0

    async def fetch_account_margin(self, account_id: str) -> dict[str, Any]:
        """Issue 2: Fetch IBKR account margin / liquidity summary via REST.

        Returns a dict with keys like ``netliquidation``, ``excessliquidity``, and
        ``cushion`` (ratio of excess liquidity to net liquidation value).  Returns an
        empty dict if the request fails so callers can degrade gracefully.
        """
        try:
            response = await asyncio.to_thread(
                self.client.session.get,
                f"{self.client.base_url}/v1/api/portfolio/{account_id}/summary",
                timeout=5,
            )
            if response.status_code == 200:
                return dict(response.json())
            LOGGER.warning(
                "fetch_account_margin: status %s for account %s",
                response.status_code,
                account_id,
            )
        except Exception as exc:
            LOGGER.warning("fetch_account_margin failed for %s: %s", account_id, exc)
        return {}

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
    def _parse_expiration(expiry: str) -> date | None:
        raw = str(expiry or "").strip()
        if not raw:
            return None

        if raw.isdigit() and len(raw) > 8:
            try:
                return datetime.fromtimestamp(int(raw) / 1000).date()
            except (ValueError, OSError):
                return None

        normalized = raw.replace("-", "")
        if len(normalized) == 8 and normalized.isdigit():
            try:
                return datetime.strptime(normalized, "%Y%m%d").date()
            except ValueError:
                return None

        return None

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
