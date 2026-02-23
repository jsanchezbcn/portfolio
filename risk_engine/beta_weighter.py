"""risk_engine/beta_weighter.py — SPX beta-weighting for portfolio delta (T013–T015).

Provides BetaWeighter, which:
 - Fetches stock betas from a 3-layer fallback chain:
     1. Tastytrade `get_market_metrics()` (primary, most current)
     2. yfinance `Ticker(sym).info["beta"]`   (secondary, publicly available)
     3. `beta_config.json` static look-up table  (tertiary, project-maintained)
     4. Default 1.0 + beta_unavailable=True flag  (final fallback)
 - Computes per-position SPX-equivalent delta:
     spx_eq_delta = (delta × qty × multiplier × beta × underlying_price) / spx_price
 - Aggregates a list of positions into a single PortfolioGreeks snapshot.

Usage example::

    weighter = BetaWeighter(
        tastytrade_session=session,
        beta_config_path="beta_config.json",
    )
    greeks = await weighter.compute_portfolio_spx_delta(positions, spx_price=5200.0)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yfinance as yf

try:
    from tastytrade.metrics import get_market_metrics
except ImportError:  # Tastytrade SDK not installed — tests will mock this
    get_market_metrics = None  # type: ignore[assignment]

from models.order import PortfolioGreeks
from models.unified_position import BetaWeightedPosition, UnifiedPosition

logger = logging.getLogger(__name__)

# Default path relative to project root
_DEFAULT_BETA_CONFIG_PATH = Path(__file__).parent.parent / "beta_config.json"


class BetaWeighter:
    """Fetches betas from a waterfall of sources and computes SPX-equivalent delta.

    Parameters
    ----------
    tastytrade_session:
        An active ``tastytrade.Session`` object.  Pass ``None`` to skip the
        Tastytrade source (useful in tests or when the session is unavailable).
    beta_config_path:
        Filesystem path to ``beta_config.json``.  Defaults to the project-root
        ``beta_config.json``.
    _beta_config_override:
        Internal test hook — pass a dict to bypass loading from disk.
    """

    def __init__(
        self,
        tastytrade_session: Any = None,
        beta_config_path: str | Path | None = None,
        ibkr_client: Any = None,
        *,
        _beta_config_override: dict[str, float] | None = None,
    ) -> None:
        self._session = tastytrade_session
        self._ibkr_client = ibkr_client
        self._beta_config: dict[str, float] = {}
        self._beta_cache: dict[str, tuple[float, str, bool]] = {}

        if _beta_config_override is not None:
            # Test-injected config — don't touch disk
            self._beta_config = _beta_config_override
        else:
            path = Path(beta_config_path) if beta_config_path else _DEFAULT_BETA_CONFIG_PATH
            try:
                with path.open() as fh:
                    loaded = json.load(fh)
                    if isinstance(loaded, dict) and isinstance(loaded.get("betas"), dict):
                        self._beta_config = loaded.get("betas", {})
                    elif isinstance(loaded, dict):
                        self._beta_config = loaded
                    else:
                        self._beta_config = {}
                logger.debug("Loaded beta_config from %s (%d entries)", path, len(self._beta_config))
            except FileNotFoundError:
                logger.warning("beta_config.json not found at %s; proceeding without static betas", path)

    # ------------------------------------------------------------------ #
    # Beta retrieval waterfall                                            #
    # ------------------------------------------------------------------ #

    # Futures/index root symbols that share tickers with ordinary equities on
    # financial data providers (e.g. "ES" = Eversource Energy on yfinance).
    # For these we skip all live API lookups and go straight to beta_config /
    # default=1.0 so we never confuse E-mini S&P 500 with Eversource Energy.
    _FUTURES_ROOTS: frozenset[str] = frozenset({
        "ES", "MES", "NQ", "MNQ", "RTY", "M2K", "YM", "MYM",
        "GC", "SI", "HG", "PL", "CL", "NG", "RB", "HO",
        "ZB", "ZN", "ZF", "ZT", "ZC", "ZW", "ZS", "ZL", "ZM",
        "SPX", "SPXW", "XSP", "NDX", "RUT", "VIX",
    })

    async def get_beta(
        self,
        symbol: str,
        *,
        session: Any = None,
    ) -> tuple[float, str, bool]:
        """Return ``(beta_value, source_name, beta_unavailable)`` for *symbol*.

        Sources tried in order:
        1. Tastytrade get_market_metrics() — ``source = "tastytrade"``
        2. IBKR fundamentals snapshot      — ``source = "ibkr"``
        3. yfinance Ticker.info["beta"]   — ``source = "yfinance"``
        4. beta_config.json look-up       — ``source = "config"``
        5. Default 1.0                    — ``source = "default"``, ``unavailable = True``

        NOTE: Futures/index symbols (ES, MES, NQ, SPX, etc.) skip sources 1-3
        entirely because data providers conflate these tickers with equities
        (e.g. yfinance returns Eversource Energy for "ES" with beta=0.79).
        """
        normalized_symbol = (symbol or "").strip().upper()

        # Short-circuit for futures/index symbols — bypass live API lookups AND
        # the cache to avoid serving a stale poisoned entry (e.g. a previous
        # yfinance call may have cached ES → Eversource Energy with beta=0.79).
        # This check MUST come before the cache lookup.
        bare = normalized_symbol.lstrip("/")
        if bare in self._FUTURES_ROOTS:
            for key in (normalized_symbol, bare, f"/{bare}"):
                if key in self._beta_config:
                    beta = float(self._beta_config[key])
                    logger.debug("Beta for %s from config (futures fast-path): %.4f", symbol, beta)
                    result = (beta, "config", False)
                    self._beta_cache[normalized_symbol] = result
                    return result
            # Not in config either — default 1.0 (index/futures beta by definition)
            logger.debug("Beta for futures %s not in config; defaulting to 1.0", symbol)
            result = (1.0, "default_futures", False)
            self._beta_cache[normalized_symbol] = result
            return result

        # Cache check for non-futures/equity symbols (safe to use cached values here)
        if normalized_symbol in self._beta_cache:
            return self._beta_cache[normalized_symbol]

        effective_session = session or self._session

        # ── Source 1: Tastytrade ──────────────────────────────────────────
        if effective_session is not None and get_market_metrics is not None:
            try:
                metrics_list = get_market_metrics(effective_session, [normalized_symbol])
                if metrics_list:
                    raw_beta = getattr(metrics_list[0], "beta", None)
                    if raw_beta is not None:
                        beta = float(raw_beta)
                        logger.debug("Beta for %s from Tastytrade: %.4f", symbol, beta)
                        result = (beta, "tastytrade", False)
                        self._beta_cache[normalized_symbol] = result
                        return result
            except Exception as exc:
                logger.debug("Tastytrade beta fetch failed for %s: %s", symbol, exc)

        # ── Source 2: IBKR Client Portal fundamentals (best-effort) ─────
        try:
            ibkr_beta = self._get_beta_from_ibkr(normalized_symbol)
            if ibkr_beta is not None:
                logger.debug("Beta for %s from IBKR: %.4f", symbol, ibkr_beta)
                result = (ibkr_beta, "ibkr", False)
                self._beta_cache[normalized_symbol] = result
                return result
        except Exception as exc:
            logger.debug("IBKR beta fetch failed for %s: %s", symbol, exc)

        # ── Source 3: yfinance ────────────────────────────────────────────
        try:
            info = yf.Ticker(normalized_symbol).info
            raw_beta = info.get("beta")
            if raw_beta is not None:
                beta = float(raw_beta)
                logger.debug("Beta for %s from yfinance: %.4f", symbol, beta)
                result = (beta, "yfinance", False)
                self._beta_cache[normalized_symbol] = result
                return result
        except Exception as exc:
            logger.debug("yfinance beta fetch failed for %s: %s", symbol, exc)

        # ── Source 4: beta_config.json ────────────────────────────────────
        for key in (normalized_symbol, normalized_symbol.lstrip("/"), f"/{normalized_symbol.lstrip('/')}"):
            if key in self._beta_config:
                beta = float(self._beta_config[key])
                logger.debug("Beta for %s from config ('%s'): %.4f", symbol, key, beta)
                result = (beta, "config", False)
                self._beta_cache[normalized_symbol] = result
                return result

        # ── Source 5: Default ─────────────────────────────────────────────
        logger.warning(
            "No beta found for %s in any source — defaulting to 1.0 (beta_unavailable=True)",
            symbol,
        )
        result = (1.0, "default", True)
        self._beta_cache[normalized_symbol] = result
        return result

    def _get_beta_from_ibkr(self, symbol: str) -> float | None:
        """Try to retrieve beta from IBKR fundamental ratios (field 47).

        Client Portal often omits field 47. This is best-effort and returns None
        when unavailable.
        """
        client = self._ibkr_client
        if client is None:
            return None

        base_url = getattr(client, "base_url", None)
        session = getattr(client, "session", None)
        if not base_url or session is None:
            return None

        sym = (symbol or "").strip().lstrip("/")
        if not sym:
            return None

        search_resp = session.get(
            f"{base_url}/v1/api/iserver/secdef/search",
            params={"symbol": sym, "secType": "STK"},
            verify=False,
            timeout=5,
        )
        if search_resp.status_code != 200:
            return None

        entries = search_resp.json() if isinstance(search_resp.json(), list) else []
        conid = None
        for item in entries:
            cid = item.get("conid")
            if cid:
                conid = cid
                break
        if not conid:
            return None

        snap_resp = session.get(
            f"{base_url}/v1/api/iserver/marketdata/snapshot",
            params={"conids": str(conid), "fields": "47"},
            verify=False,
            timeout=5,
        )
        if snap_resp.status_code != 200:
            return None

        payload = snap_resp.json() if isinstance(snap_resp.json(), list) else []
        if not payload:
            return None
        field_47 = payload[0].get("47")
        if not field_47:
            return None

        match = re.search(r"(?:^|;)\s*BETA\s*=\s*([-+]?\d+(?:\.\d+)?)", str(field_47), re.IGNORECASE)
        if not match:
            return None
        try:
            return float(match.group(1))
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------ #
    # Per-position SPX equivalent delta (T014)                           #
    # ------------------------------------------------------------------ #

    async def compute_spx_equivalent_delta(
        self,
        position: UnifiedPosition,
        spx_price: float,
        *,
        session: Any = None,
    ) -> BetaWeightedPosition:
        """Compute the SPX-equivalent delta for a single position.

        Formula::

            spx_delta = (total_delta × β × P_underlying) / P_SPX

        Where:
        - total_delta    = position.delta — already scaled by qty × multiplier
                          (the adapter stores raw_delta × quantity × contract_multiplier)
        - β              = beta vs. SPX
        - P_underlying   = live price of the underlying asset
        - P_SPX          = live SPX index price

        NOTE: position.delta is the **total position delta** (qty × mult × raw Δ).
        Do NOT re-multiply by quantity or multiplier here; that would double-count.

        Edge cases:
        - spx_price == 0 → return 0 (no ZeroDivisionError)
        - underlying_price is None → return 0 (no price data)
        """
        beta, source, unavailable = await self.get_beta(
            position.underlying or position.symbol,
            session=session,
        )

        underlying_price = position.underlying_price

        if not spx_price or underlying_price is None:
            spx_eq_delta = 0.0
        else:
            total_delta = position.delta if position.delta is not None else 0.0
            spx_eq_delta = (total_delta * beta * underlying_price) / spx_price

        return BetaWeightedPosition(
            position=position,
            beta=beta,
            beta_source=source,
            beta_unavailable=unavailable,
            spx_equivalent_delta=spx_eq_delta,
        )

    # ------------------------------------------------------------------ #
    # Portfolio aggregation (T015)                                       #
    # ------------------------------------------------------------------ #

    async def compute_portfolio_spx_delta(
        self,
        positions: list[UnifiedPosition],
        spx_price: float,
        *,
        session: Any = None,
    ) -> PortfolioGreeks:
        """Aggregate all positions into a single PortfolioGreeks snapshot.

        SPX delta is the sum of beta-weighted deltas across all positions.
        Theta, vega, and gamma are the raw (non-beta-weighted) sums.

        Returns a PortfolioGreeks with timestamp=now(UTC).
        """
        total_spx_delta = 0.0
        total_gamma = 0.0
        total_theta = 0.0
        total_vega = 0.0

        for pos in positions:
            bw = await self.compute_spx_equivalent_delta(pos, spx_price, session=session)
            total_spx_delta += bw.spx_equivalent_delta
            total_gamma += float(pos.gamma or 0.0)
            total_theta += float(pos.theta or 0.0)
            total_vega += float(pos.vega or 0.0)

        return PortfolioGreeks(
            spx_delta=total_spx_delta,
            gamma=total_gamma,
            theta=total_theta,
            vega=total_vega,
            timestamp=datetime.now(timezone.utc),
        )
