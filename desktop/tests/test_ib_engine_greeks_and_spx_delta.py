from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from desktop.engine.ib_engine import IBEngine


@pytest.mark.asyncio
async def test_refresh_positions_uses_fallback_greeks_sources():
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._account_id = "U123"
    engine._spx_proxy_price_async = AsyncMock(return_value=7000.0)

    opt_contract = SimpleNamespace(
        conId=101,
        secType="OPT",
        symbol="SPY",
        localSymbol="SPY  260320C00680000",
        exchange="SMART",
        currency="USD",
        strike=680.0,
        right="C",
        lastTradeDateOrContractMonth="20260320",
        multiplier="100",
    )
    opt_pos = SimpleNamespace(contract=opt_contract, position=1.0, avgCost=10.0)
    engine._ib.positions.return_value = [opt_pos]

    portfolio_item = SimpleNamespace(
        contract=opt_contract,
        marketPrice=12.0,
        marketValue=1200.0,
        unrealizedPNL=200.0,
        realizedPNL=0.0,
        averageCost=10.0,
    )
    engine._ib.portfolio.return_value = [portfolio_item]

    bid_greeks = SimpleNamespace(delta=0.30, gamma=0.02, theta=-1.0, vega=5.0, impliedVol=0.20)
    ticker = SimpleNamespace(modelGreeks=None, bidGreeks=bid_greeks, askGreeks=None, lastGreeks=None)
    engine._ib.reqMktData.return_value = ticker

    rows = await engine.refresh_positions()

    assert len(rows) == 1
    row = rows[0]
    assert row.delta == pytest.approx(30.0)
    assert row.gamma == pytest.approx(2.0)
    assert row.theta == pytest.approx(-100.0)
    assert row.vega == pytest.approx(500.0)
    assert row.iv == pytest.approx(0.20)
    assert row.greeks_source == "live"


@pytest.mark.asyncio
async def test_refresh_positions_estimates_missing_option_greeks_locally():
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._account_id = "U123"
    engine._spx_proxy_price_async = AsyncMock(return_value=7000.0)
    engine._symbol_betas = {"SPY": 1.0}
    engine._last_price_cache["SPY"] = 600.0

    opt_contract = SimpleNamespace(
        conId=202,
        secType="OPT",
        symbol="SPY",
        localSymbol="SPY  260320C00600000",
        exchange="SMART",
        currency="USD",
        strike=600.0,
        right="C",
        lastTradeDateOrContractMonth="20990320",
        multiplier="100",
    )
    opt_pos = SimpleNamespace(contract=opt_contract, position=1.0, avgCost=10.0)
    engine._ib.positions.return_value = [opt_pos]
    engine._ib.portfolio.return_value = [
        SimpleNamespace(
            contract=opt_contract,
            marketPrice=12.0,
            marketValue=1200.0,
            unrealizedPNL=200.0,
            realizedPNL=0.0,
            averageCost=10.0,
        )
    ]
    engine._greeks_cache[202] = {"iv": 0.20}

    ticker = SimpleNamespace(modelGreeks=None, bidGreeks=None, askGreeks=None, lastGreeks=None)
    engine._ib.reqMktData.return_value = ticker

    rows = await engine.refresh_positions()

    assert len(rows) == 1
    row = rows[0]
    assert row.greeks_source == "estimated_bsm"
    assert row.delta is not None
    assert row.gamma is not None
    assert row.theta is not None
    assert row.vega is not None


@pytest.mark.asyncio
async def test_refresh_positions_computes_stock_spx_weighted_delta():
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._account_id = "U123"
    engine._spx_proxy_price_async = AsyncMock(return_value=7000.0)

    # Beta map from config equivalent
    engine._symbol_betas = {"AAPL": 1.4}
    engine._beta_default = 1.0

    stock_contract = SimpleNamespace(
        conId=1,
        secType="STK",
        symbol="AAPL",
        localSymbol="AAPL",
        exchange="SMART",
        currency="USD",
        strike=0.0,
        right="",
        lastTradeDateOrContractMonth="",
        multiplier="1",
    )
    stock_pos = SimpleNamespace(contract=stock_contract, position=100.0, avgCost=150.0)
    engine._ib.positions.return_value = [stock_pos]

    portfolio_item = SimpleNamespace(
        contract=stock_contract,
        marketPrice=200.0,
        marketValue=20000.0,
        unrealizedPNL=0.0,
        realizedPNL=0.0,
        averageCost=150.0,
    )
    engine._ib.portfolio.return_value = [portfolio_item]

    rows = await engine.refresh_positions()

    assert len(rows) == 1
    row = rows[0]
    expected_spx_delta = 100.0 * 1.4 * (200.0 / 7000.0)
    assert row.delta == pytest.approx(expected_spx_delta)
    assert row.spx_delta == pytest.approx(expected_spx_delta)


# ── Futures SPX delta ─────────────────────────────────────────────────────


def _make_fut_engine(symbol: str, multiplier: str, qty: float):
    """Helper: engine wired with a single FUT position."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._account_id = "U123"
    engine._spx_proxy_price_async = AsyncMock(return_value=7000.0)

    contract = SimpleNamespace(
        conId=200,
        secType="FUT",
        symbol=symbol,
        localSymbol=f"{symbol}H6",
        exchange="CME",
        currency="USD",
        strike=0.0,
        right="",
        lastTradeDateOrContractMonth="20260320",
        multiplier=multiplier,
    )
    pos = SimpleNamespace(contract=contract, position=qty, avgCost=6900.0)
    engine._ib.positions.return_value = [pos]

    portfolio_item = SimpleNamespace(
        contract=contract,
        marketPrice=6900.0,
        marketValue=6900.0 * float(multiplier) * qty,
        unrealizedPNL=0.0,
        realizedPNL=0.0,
        averageCost=6900.0,
    )
    engine._ib.portfolio.return_value = [portfolio_item]

    # Futures have no option Greeks ticker
    ticker = SimpleNamespace(modelGreeks=None, bidGreeks=None, askGreeks=None, lastGreeks=None)
    engine._ib.reqMktData.return_value = ticker
    return engine


@pytest.mark.asyncio
async def test_es_futures_spx_delta():
    """/ES with 2 long contracts should give spx_delta = +100 (2 × 50)."""
    engine = _make_fut_engine("ES", "50", qty=2.0)
    rows = await engine.refresh_positions()
    assert len(rows) == 1
    row = rows[0]
    assert row.sec_type == "FUT"
    assert row.delta == pytest.approx(2.0 * 50.0)   # 100
    assert row.spx_delta == pytest.approx(2.0 * 50.0)  # 100


@pytest.mark.asyncio
async def test_mes_futures_spx_delta():
    """/MES with 3 long contracts should give spx_delta = +15 (3 × 5)."""
    engine = _make_fut_engine("MES", "5", qty=3.0)
    rows = await engine.refresh_positions()
    assert len(rows) == 1
    row = rows[0]
    assert row.sec_type == "FUT"
    assert row.delta == pytest.approx(3.0 * 5.0)    # 15
    assert row.spx_delta == pytest.approx(3.0 * 5.0)   # 15


@pytest.mark.asyncio
async def test_mes_short_futures_spx_delta():
    """-10 /MES contracts should give spx_delta = -50."""
    engine = _make_fut_engine("MES", "5", qty=-10.0)
    rows = await engine.refresh_positions()
    row = rows[0]
    assert row.spx_delta == pytest.approx(-50.0)


@pytest.mark.asyncio
async def test_nq_futures_spx_delta():
    """/NQ multiplier is 20; 1 contract = spx_delta +20."""
    engine = _make_fut_engine("NQ", "20", qty=1.0)
    rows = await engine.refresh_positions()
    row = rows[0]
    assert row.spx_delta == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_unknown_futures_spx_delta_is_none():
    """Unknown futures root symbol (e.g. CL) should get spx_delta=None."""
    engine = _make_fut_engine("CL", "1000", qty=1.0)
    rows = await engine.refresh_positions()
    row = rows[0]
    assert row.spx_delta is None
