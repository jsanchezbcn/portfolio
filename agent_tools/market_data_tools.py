from __future__ import annotations

import math
from datetime import datetime
from typing import Iterable

import yfinance as yf

from adapters.polymarket_adapter import PolymarketAdapter


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
