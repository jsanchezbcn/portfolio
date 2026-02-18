from __future__ import annotations

import logging
import math
import os
from datetime import datetime
from typing import Iterable

import httpx
import yfinance as yf

from adapters.polymarket_adapter import PolymarketAdapter

logger = logging.getLogger(__name__)

# FRED series for the risk-free rate (3-month T-bill yield)
_FRED_SERIES = "DGS3MO"
_FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
_FRED_FALLBACK_RATE = 0.05  # 5% default if FRED unreachable


_POLYMARKET_ADAPTER = PolymarketAdapter()


class MarketDataTools:
    """Market and macro data access utilities for regime and IV/HV analytics."""

    def get_vix_data(self) -> dict:
        """Fetch latest VIX term-structure data."""

        vix = yf.Ticker("^VIX").history(period="5d")
        vix3m = yf.Ticker("^VIX3M").history(period="5d")

        if vix.empty or vix3m.empty:
            raise ValueError("Unable to retrieve VIX data")

        vix_last = float(vix["Close"].iloc[-1])
        vix3m_last = float(vix3m["Close"].iloc[-1])
        term_structure = vix3m_last / vix_last if vix_last else math.nan

        return {
            "vix": vix_last,
            "vix3m": vix3m_last,
            "term_structure": term_structure,
            "is_backwardation": term_structure < 1.0,
            "timestamp": datetime.utcnow().isoformat(),
        }

    def get_spx_data(self) -> dict:
        """Fetch SPX spot and realized volatility snapshot."""

        spx = yf.Ticker("^GSPC").history(period="40d")
        if spx.empty:
            raise ValueError("Unable to retrieve SPX data")

        close = spx["Close"]
        daily_returns = close.pct_change().dropna()
        realized_vol = float(daily_returns.tail(30).std() * (252 ** 0.5)) if not daily_returns.empty else 0.0

        return {
            "spx": float(close.iloc[-1]),
            "realized_vol_30d": realized_vol,
            "timestamp": datetime.utcnow().isoformat(),
        }

    def get_historical_volatility(self, symbols: Iterable[str], lookback_days: int = 30) -> dict[str, float]:
        """Return annualized historical volatility for provided symbols."""

        hv_by_symbol: dict[str, float] = {}
        min_required_days = max(1, lookback_days - 2)

        for symbol in symbols:
            ticker = str(symbol or "").strip().upper()
            if not ticker:
                continue

            history = yf.Ticker(ticker).history(period=f"{lookback_days + 12}d")
            if history.empty or "Close" not in history:
                continue

            close = history["Close"].dropna()
            if close.empty:
                continue

            log_returns = (close / close.shift(1)).apply(lambda value: math.log(value) if value and value > 0 else math.nan)
            log_returns = log_returns.dropna().tail(lookback_days)
            if len(log_returns) < min_required_days:
                continue

            hv_by_symbol[ticker] = float(log_returns.std() * math.sqrt(252))

        return hv_by_symbol

    async def get_macro_indicators(self) -> dict:
        """Fetch macro indicators used by regime detection."""

        return await _POLYMARKET_ADAPTER.get_recession_probability()

    async def get_risk_free_rate(self) -> float:
        """Return the current annualised risk-free rate from FRED (DGS3MO).

        Falls back to ``_FRED_FALLBACK_RATE`` (5%) if the API key is missing
        or the request fails so that callers always receive a usable float.

        Set ``FRED_API_KEY`` in ``.env`` to enable live data:
          https://fred.stlouisfed.org/docs/api/api_key.html  (free)
        """
        api_key = os.getenv("FRED_API_KEY", "")
        if not api_key:
            logger.debug("FRED_API_KEY not set — using fallback rate %.0f%%", _FRED_FALLBACK_RATE * 100)
            return _FRED_FALLBACK_RATE

        try:
            params = {
                "series_id": _FRED_SERIES,
                "api_key": api_key,
                "file_type": "json",
                "limit": 1,
                "sort_order": "desc",
                "observation_start": "2020-01-01",
            }
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(_FRED_BASE_URL, params=params)
                resp.raise_for_status()
                data = resp.json()
            observations: list[dict] = data.get("observations", [])
            if not observations:
                return _FRED_FALLBACK_RATE
            raw_value = observations[0].get("value", ".")
            if raw_value in (".", "", None):
                # FRED uses "." for missing observations on weekends/holidays
                return _FRED_FALLBACK_RATE
            # FRED returns the yield in percentage points (e.g. 5.25 = 5.25%)
            return float(raw_value) / 100.0
        except Exception as exc:
            logger.warning("FRED request failed (%s) — using fallback %.0f%%", exc, _FRED_FALLBACK_RATE * 100)
            return _FRED_FALLBACK_RATE
