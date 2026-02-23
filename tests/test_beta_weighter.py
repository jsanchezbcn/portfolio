"""tests/test_beta_weighter.py — TDD tests for risk_engine/beta_weighter.py (T012).

Write these tests FIRST; they must FAIL before BetaWeighter is implemented (T013–T015).

Coverage:
 - get_beta(): Tastytrade primary, yfinance fallback, beta_config.json fallback, all-fail default
 - compute_spx_equivalent_delta(): formula + multiplier handling (/ES=50, /MES=5, options=100, stock=1)
 - compute_portfolio_spx_delta(): aggregation → PortfolioGreeks
"""
from __future__ import annotations

import json
import math
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from models.order import PortfolioGreeks
from models.unified_position import BetaWeightedPosition, UnifiedPosition
from risk_engine.beta_weighter import BetaWeighter


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

SPX_PRICE = 5_200.0


def _make_position(
    *,
    symbol: str = "AAPL",
    underlying: str = "AAPL",
    delta: float = 0.50,
    gamma: float = 0.01,
    theta: float = -5.0,
    vega: float = 20.0,
    quantity: int = 1,
    multiplier: float = 1.0,
    underlying_price: float | None = 150.0,
    is_option: bool = False,
    is_futures: bool = False,
) -> UnifiedPosition:
    from models.unified_position import InstrumentType
    from datetime import date as date_

    if is_option and is_futures:
        itype = InstrumentType.FUTURE_OPTION
    elif is_futures:
        itype = InstrumentType.FUTURE
    elif is_option:
        itype = InstrumentType.OPTION
    else:
        itype = InstrumentType.EQUITY

    kwargs: dict = dict(
        symbol=symbol,
        underlying=underlying,
        quantity=float(quantity),
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        underlying_price=underlying_price,
        contract_multiplier=multiplier,
        instrument_type=itype,
        broker="TEST",
        avg_price=0.0,
        market_value=0.0,
        unrealized_pnl=0.0,
    )
    if is_option:
        # Option positions require strike, expiration, option_type
        kwargs.setdefault("strike", 4500.0)
        kwargs.setdefault("expiration", date_(2025, 12, 19))
        kwargs.setdefault("option_type", "call")

    return UnifiedPosition(**kwargs)


def _make_weighter(beta_config: dict | None = None, config_path: str | None = None) -> BetaWeighter:
    """Construct a BetaWeighter with no live connections."""
    return BetaWeighter(
        tastytrade_session=None,
        beta_config_path=config_path,
        _beta_config_override=beta_config or {},
    )


# ------------------------------------------------------------------ #
# get_beta — source priority                                          #
# ------------------------------------------------------------------ #

class TestGetBeta:
    """Beta source waterfall: Tastytrade → yfinance → beta_config.json → 1.0."""

    @pytest.mark.asyncio
    async def test_returns_tastytrade_beta_when_available(self):
        """Primary source: Tastytrade get_market_metrics()."""
        weighter = _make_weighter()
        mock_session = MagicMock()
        # Tastytrade SDK: get_market_metrics returns a Metrics-like object with .beta
        mock_metrics = MagicMock()
        mock_metrics.beta = 1.23
        with patch(
            "risk_engine.beta_weighter.get_market_metrics",
            return_value=[mock_metrics],
        ):
            beta, source, unavailable = await weighter.get_beta("AAPL", session=mock_session)
        assert beta == pytest.approx(1.23)
        assert source == "tastytrade"
        assert unavailable is False

    @pytest.mark.asyncio
    async def test_falls_back_to_yfinance_when_tastytrade_unavailable(self):
        """Second source: yfinance Ticker.info['beta']."""
        weighter = _make_weighter()
        with (
            patch("risk_engine.beta_weighter.get_market_metrics", side_effect=Exception("offline")),
            patch("risk_engine.beta_weighter.yf") as mock_yf,
        ):
            mock_ticker = MagicMock()
            mock_ticker.info = {"beta": 0.85}
            mock_yf.Ticker.return_value = mock_ticker
            beta, source, unavailable = await weighter.get_beta("AAPL")
        assert beta == pytest.approx(0.85)
        assert source == "yfinance"
        assert unavailable is False

    @pytest.mark.asyncio
    async def test_falls_back_to_config_when_yfinance_unavailable(self):
        """Third source: beta_config.json lookup."""
        weighter = _make_weighter(beta_config={"SPY": 1.0, "MES": 1.0})
        with (
            patch("risk_engine.beta_weighter.get_market_metrics", side_effect=Exception("offline")),
            patch("risk_engine.beta_weighter.yf") as mock_yf,
        ):
            mock_ticker = MagicMock()
            mock_ticker.info = {}  # no beta key
            mock_yf.Ticker.return_value = mock_ticker
            beta, source, unavailable = await weighter.get_beta("SPY")
        assert beta == pytest.approx(1.0)
        assert source == "config"
        assert unavailable is False

    @pytest.mark.asyncio
    async def test_returns_default_1_0_when_all_sources_fail(self):
        """Final fallback: beta=1.0, beta_unavailable=True."""
        weighter = _make_weighter(beta_config={})
        with (
            patch("risk_engine.beta_weighter.get_market_metrics", side_effect=Exception("offline")),
            patch("risk_engine.beta_weighter.yf") as mock_yf,
        ):
            mock_ticker = MagicMock()
            mock_ticker.info = {}
            mock_yf.Ticker.return_value = mock_ticker
            beta, source, unavailable = await weighter.get_beta("EXOTIC_SYMBOL_XYZ")
        assert beta == pytest.approx(1.0)
        assert source == "default"
        assert unavailable is True

    @pytest.mark.asyncio
    async def test_slash_mes_key_resolves_correctly(self):
        """'/MES' in beta_config should be matched for the symbol '/MES'."""
        weighter = _make_weighter(beta_config={"/MES": 1.0, "MES": 1.0})
        with (
            patch("risk_engine.beta_weighter.get_market_metrics", side_effect=Exception("offline")),
            patch("risk_engine.beta_weighter.yf") as mock_yf,
        ):
            mock_yf.Ticker.return_value.info = {}
            beta, source, _ = await weighter.get_beta("/MES")
        assert beta == pytest.approx(1.0)
        assert source == "config"


# ------------------------------------------------------------------ #
# compute_spx_equivalent_delta — formula accuracy                    #
# ------------------------------------------------------------------ #

class TestComputeSpxEquivalentDelta:
    """Formula: (delta × qty × multiplier × beta × underlying_price) / spx_price."""

    @pytest.mark.asyncio
    async def test_stock_position_formula(self):
        """Stock: multiplier=1, underlying_price=150, total_delta=50 (=0.5×100×1), beta=1.0."""
        weighter = _make_weighter(beta_config={"AAPL": 1.2})
        # pos.delta stores TOTAL position delta = raw(0.50) × qty(100) × mult(1.0) = 50.0
        pos = _make_position(delta=50.0, quantity=100, multiplier=1.0, underlying_price=150.0)
        with patch.object(weighter, "get_beta", return_value=(1.2, "config", False)):
            result = await weighter.compute_spx_equivalent_delta(pos, SPX_PRICE)
        # (50.0 × 1.2 × 150) / 5200 = 9000/5200 ≈ 1.7308
        expected = (50.0 * 1.2 * 150.0) / SPX_PRICE
        assert isinstance(result, BetaWeightedPosition)
        assert result.spx_equivalent_delta == pytest.approx(expected, rel=1e-4)
        assert result.beta == pytest.approx(1.2)
        assert result.beta_source == "config"
        assert result.beta_unavailable is False

    @pytest.mark.asyncio
    async def test_option_position_multiplier_100(self):
        """/Equity options: multiplier=100. total_delta = 0.30 × -1 × 100 = -30."""
        weighter = _make_weighter()
        # pos.delta stores TOTAL position delta = raw(0.30) × qty(-1) × mult(100) = -30.0
        pos = _make_position(
            symbol="SPX   260321C04500",
            underlying="SPX",
            delta=-30.0,
            quantity=-1,  # short call
            multiplier=100.0,
            underlying_price=SPX_PRICE,
            is_option=True,
        )
        with patch.object(weighter, "get_beta", return_value=(1.0, "tastytrade", False)):
            result = await weighter.compute_spx_equivalent_delta(pos, SPX_PRICE)
        # (-30.0 × 1.0 × 5200) / 5200 = -30.0
        expected = (-30.0 * 1.0 * SPX_PRICE) / SPX_PRICE
        assert result.spx_equivalent_delta == pytest.approx(expected, rel=1e-4)

    @pytest.mark.asyncio
    async def test_mes_futures_multiplier_5(self):
        """/MES futures: multiplier=5, beta=1.0. total_delta = 1.0×2×5 = 10."""
        weighter = _make_weighter(beta_config={"/MES": 1.0})
        # pos.delta stores TOTAL position delta = raw(1.0) × qty(2) × mult(5) = 10.0
        pos = _make_position(
            symbol="/MES",
            underlying="/MES",
            delta=10.0,
            quantity=2,
            multiplier=5.0,
            underlying_price=5_200.0,
            is_futures=True,
        )
        with patch.object(weighter, "get_beta", return_value=(1.0, "config", False)):
            result = await weighter.compute_spx_equivalent_delta(pos, SPX_PRICE)
        # (10.0 × 1.0 × 5200) / 5200 = 10.0
        assert result.spx_equivalent_delta == pytest.approx(10.0, rel=1e-4)

    @pytest.mark.asyncio
    async def test_es_futures_multiplier_50(self):
        """/ES futures: multiplier=50. total_delta = 1.0×1×50 = 50."""
        weighter = _make_weighter(beta_config={"/ES": 1.0})
        # pos.delta stores TOTAL position delta = raw(1.0) × qty(1) × mult(50) = 50.0
        pos = _make_position(
            symbol="/ES",
            underlying="/ES",
            delta=50.0,
            quantity=1,
            multiplier=50.0,
            underlying_price=5_200.0,
            is_futures=True,
        )
        with patch.object(weighter, "get_beta", return_value=(1.0, "config", False)):
            result = await weighter.compute_spx_equivalent_delta(pos, SPX_PRICE)
        # (50.0 × 1.0 × 5200) / 5200 = 50.0
        assert result.spx_equivalent_delta == pytest.approx(50.0, rel=1e-4)

    @pytest.mark.asyncio
    async def test_spx_price_zero_returns_zero_delta(self):
        """Guard: spx_price=0 should not raise ZeroDivisionError; return 0 delta."""
        weighter = _make_weighter()
        pos = _make_position(delta=0.5, quantity=1, multiplier=1.0, underlying_price=150.0)
        with patch.object(weighter, "get_beta", return_value=(1.0, "default", True)):
            result = await weighter.compute_spx_equivalent_delta(pos, spx_price=0.0)
        assert result.spx_equivalent_delta == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_missing_underlying_price_falls_back_to_zero_delta(self):
        """Position with underlying_price=None should yield spx_equivalent_delta=0."""
        weighter = _make_weighter()
        pos = _make_position(delta=0.5, quantity=1, multiplier=1.0, underlying_price=None)
        with patch.object(weighter, "get_beta", return_value=(1.0, "default", True)):
            result = await weighter.compute_spx_equivalent_delta(pos, SPX_PRICE)
        assert result.spx_equivalent_delta == pytest.approx(0.0)
        assert result.beta_unavailable is True


# ------------------------------------------------------------------ #
# compute_portfolio_spx_delta — aggregation → PortfolioGreeks        #
# ------------------------------------------------------------------ #

class TestComputePortfolioSpxDelta:
    @pytest.mark.asyncio
    async def test_portfolio_delta_sums_correctly(self):
        """Portfolio of 3 positions: SPX delta should be their sum."""
        weighter = _make_weighter()

        # pos.delta = total position delta (raw × qty × mult)
        positions = [
            _make_position(symbol="AAPL", underlying="AAPL", delta=50.0, quantity=100, multiplier=1.0, underlying_price=150.0, theta=-5.0, vega=20.0, gamma=0.01),   # 0.5×100×1
            _make_position(symbol="SPX_CALL", underlying="SPX", delta=-30.0, quantity=-1, multiplier=100.0, underlying_price=SPX_PRICE, is_option=True, theta=-80.0, vega=200.0, gamma=-0.004),  # 0.30×-1×100
            _make_position(symbol="/MES", underlying="/MES", delta=10.0, quantity=2, multiplier=5.0, underlying_price=SPX_PRICE, is_futures=True, theta=0.0, vega=0.0, gamma=0.0),  # 1.0×2×5
        ]

        betas = [(1.2, "config", False), (1.0, "tastytrade", False), (1.0, "config", False)]
        with patch.object(weighter, "get_beta", side_effect=betas):
            greeks = await weighter.compute_portfolio_spx_delta(positions, SPX_PRICE)

        assert isinstance(greeks, PortfolioGreeks)

        # Manually compute expected deltas (total_delta × β × P) / SPX:
        # AAPL:  (50.0 × 1.2 × 150) / 5200 ≈ 1.7308
        # SPX_CALL: (-30.0 × 1.0 × 5200) / 5200 = -30.0
        # /MES: (10.0 × 1.0 × 5200) / 5200 = 10.0
        expected_delta = (
            (50.0 * 1.2 * 150.0) / SPX_PRICE
            + (-30.0 * 1.0 * SPX_PRICE) / SPX_PRICE
            + (10.0 * 1.0 * SPX_PRICE) / SPX_PRICE
        )
        assert greeks.spx_delta == pytest.approx(expected_delta, rel=1e-4)

    @pytest.mark.asyncio
    async def test_portfolio_greeks_sums_theta_vega_gamma(self):
        """Sum of theta, vega, gamma matches raw position values (not beta-weighted)."""
        weighter = _make_weighter()
        positions = [
            _make_position(theta=-50.0, vega=150.0, gamma=0.02, delta=0.5, quantity=1, multiplier=100.0, underlying_price=SPX_PRICE, is_option=True),
            _make_position(theta=-30.0, vega=80.0, gamma=0.01, delta=-0.20, quantity=1, multiplier=100.0, underlying_price=SPX_PRICE, is_option=True),
        ]
        with patch.object(weighter, "get_beta", return_value=(1.0, "tastytrade", False)):
            greeks = await weighter.compute_portfolio_spx_delta(positions, SPX_PRICE)

        assert greeks.theta == pytest.approx(-80.0)
        assert greeks.vega == pytest.approx(230.0)
        assert greeks.gamma == pytest.approx(0.03)

    @pytest.mark.asyncio
    async def test_empty_positions_returns_zero_greeks(self):
        """Empty position list → all-zero PortfolioGreeks."""
        weighter = _make_weighter()
        greeks = await weighter.compute_portfolio_spx_delta([], SPX_PRICE)
        assert greeks.spx_delta == pytest.approx(0.0)
        assert greeks.theta == pytest.approx(0.0)
        assert greeks.vega == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_timestamp_is_set(self):
        """PortfolioGreeks.timestamp should be set to a recent datetime."""
        weighter = _make_weighter()
        greeks = await weighter.compute_portfolio_spx_delta([], SPX_PRICE)
        assert isinstance(greeks.timestamp, datetime)
