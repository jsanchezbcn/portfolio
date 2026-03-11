from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
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
    engine._enable_local_greeks = True
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


@pytest.mark.asyncio
async def test_refresh_positions_returns_all_positions_not_just_last_contract():
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._account_id = "U123"
    engine._spx_proxy_price_async = AsyncMock(return_value=7000.0)

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
    option_contract = SimpleNamespace(
        conId=2,
        secType="OPT",
        symbol="SPY",
        localSymbol="SPY  260320C00600000",
        exchange="SMART",
        currency="USD",
        strike=600.0,
        right="C",
        lastTradeDateOrContractMonth="20260320",
        multiplier="100",
    )

    engine._ib.positions.return_value = [
        SimpleNamespace(contract=stock_contract, position=100.0, avgCost=150.0),
        SimpleNamespace(contract=option_contract, position=1.0, avgCost=10.0),
    ]
    engine._ib.portfolio.return_value = [
        SimpleNamespace(
            contract=stock_contract,
            marketPrice=200.0,
            marketValue=20000.0,
            unrealizedPNL=0.0,
            realizedPNL=0.0,
            averageCost=150.0,
        ),
        SimpleNamespace(
            contract=option_contract,
            marketPrice=12.0,
            marketValue=1200.0,
            unrealizedPNL=200.0,
            realizedPNL=0.0,
            averageCost=10.0,
        ),
    ]

    ticker = SimpleNamespace(
        modelGreeks=SimpleNamespace(delta=0.30, gamma=0.02, theta=-1.0, vega=5.0, impliedVol=0.20, undPrice=600.0),
        bidGreeks=None,
        askGreeks=None,
        lastGreeks=None,
    )
    engine._ib.reqMktData.return_value = ticker

    rows = await engine.refresh_positions()

    assert len(rows) == 2
    assert {row.conid for row in rows} == {1, 2}


@pytest.mark.asyncio
async def test_refresh_positions_persists_greek_fields_to_db_rows():
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._account_id = "U123"
    engine._db_ok = True
    db_mock = SimpleNamespace(
        upsert_positions=AsyncMock(),
        replace_strategy_groups=AsyncMock(),
        cache_positions_snapshot=AsyncMock(),
        cache_portfolio_greeks=AsyncMock(),
        cache_portfolio_metrics=AsyncMock(),
        store_cached_greeks=AsyncMock(),
    )
    engine._db = cast(Any, db_mock)
    engine._persist_portfolio_risk_snapshot = AsyncMock()
    engine._spx_proxy_price_async = AsyncMock(return_value=7000.0)

    opt_contract = SimpleNamespace(
        conId=303,
        secType="OPT",
        symbol="SPY",
        localSymbol="SPY  260320C00600000",
        exchange="SMART",
        currency="USD",
        strike=600.0,
        right="C",
        lastTradeDateOrContractMonth="20260320",
        multiplier="100",
    )
    engine._ib.positions.return_value = [SimpleNamespace(contract=opt_contract, position=2.0, avgCost=10.0)]
    engine._ib.portfolio.return_value = [
        SimpleNamespace(
            contract=opt_contract,
            marketPrice=12.0,
            marketValue=2400.0,
            unrealizedPNL=200.0,
            realizedPNL=0.0,
            averageCost=10.0,
        )
    ]
    engine._ib.reqMktData.return_value = SimpleNamespace(
        modelGreeks=SimpleNamespace(delta=0.30, gamma=0.02, theta=-1.0, vega=5.0, impliedVol=0.20, undPrice=600.0),
        bidGreeks=None,
        askGreeks=None,
        lastGreeks=None,
    )

    await engine.refresh_positions()

    upsert_positions = cast(AsyncMock, db_mock.upsert_positions)
    upsert_positions.assert_awaited_once()
    assert upsert_positions.await_args is not None
    persisted_rows = upsert_positions.await_args.args[1]
    assert len(persisted_rows) == 1
    persisted = persisted_rows[0]
    assert persisted["delta"] == pytest.approx(60.0)
    assert persisted["gamma"] == pytest.approx(4.0)
    assert persisted["theta"] == pytest.approx(-200.0)
    assert persisted["vega"] == pytest.approx(1000.0)
    assert persisted["iv"] == pytest.approx(0.20)
    assert persisted["spx_delta"] is not None


@pytest.mark.asyncio
async def test_refresh_positions_uses_cached_greeks_without_retrying_live_fetch():
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._account_id = "U123"
    engine._spx_proxy_price_async = AsyncMock(return_value=7000.0)
    engine._greeks_cache[404] = {
        "delta": 0.25,
        "gamma": 0.01,
        "theta": -0.5,
        "vega": 4.0,
        "iv": 0.18,
        "undPrice": 590.0,
    }

    opt_contract = SimpleNamespace(
        conId=404,
        secType="OPT",
        symbol="SPY",
        localSymbol="SPY  260320C00590000",
        exchange="SMART",
        currency="USD",
        strike=590.0,
        right="C",
        lastTradeDateOrContractMonth="20260320",
        multiplier="100",
    )
    engine._ib.positions.return_value = [SimpleNamespace(contract=opt_contract, position=1.0, avgCost=10.0)]
    engine._ib.portfolio.return_value = [
        SimpleNamespace(
            contract=opt_contract,
            marketPrice=11.0,
            marketValue=1100.0,
            unrealizedPNL=100.0,
            realizedPNL=0.0,
            averageCost=10.0,
        )
    ]
    engine._ib.reqMktData.return_value = SimpleNamespace(modelGreeks=None, bidGreeks=None, askGreeks=None, lastGreeks=None)

    rows = await engine.refresh_positions()

    assert engine._ib.reqMktData.call_count == 1
    assert len(rows) == 1
    row = rows[0]
    assert row.greeks_source == "cached"
    assert row.delta == pytest.approx(25.0)
    assert row.gamma == pytest.approx(1.0)
    assert row.theta == pytest.approx(-50.0)
    assert row.vega == pytest.approx(400.0)
    assert row.iv == pytest.approx(0.18)


@pytest.mark.asyncio
async def test_refresh_positions_does_not_use_signature_cache_when_conid_missing_in_ibkr_only_mode():
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._account_id = "U123"
    engine._spx_proxy_price_async = AsyncMock(return_value=7000.0)

    opt_contract = SimpleNamespace(
        conId=0,
        secType="OPT",
        symbol="SPY",
        localSymbol="SPY  260320C00590000",
        exchange="SMART",
        currency="USD",
        strike=590.0,
        right="C",
        lastTradeDateOrContractMonth="20260320",
        multiplier="100",
    )
    signature = ("SPY", "20260320", 590.0, "C", "OPT")
    engine._greeks_cache_by_contract[signature] = {
        "delta": 0.25,
        "gamma": 0.01,
        "theta": -0.5,
        "vega": 4.0,
        "iv": 0.18,
        "undPrice": 590.0,
    }

    engine._ib.positions.return_value = [SimpleNamespace(contract=opt_contract, position=1.0, avgCost=10.0)]
    engine._ib.portfolio.return_value = [
        SimpleNamespace(
            contract=opt_contract,
            marketPrice=11.0,
            marketValue=1100.0,
            unrealizedPNL=100.0,
            realizedPNL=0.0,
            averageCost=10.0,
        )
    ]
    engine._ib.reqMktData.return_value = SimpleNamespace(modelGreeks=None, bidGreeks=None, askGreeks=None, lastGreeks=None)

    rows = await engine.refresh_positions()

    assert len(rows) == 1
    row = rows[0]
    assert row.greeks_source is None
    assert row.delta is None
    assert row.gamma is None
    assert row.theta is None
    assert row.vega is None
    assert row.iv is None


@pytest.mark.asyncio
async def test_refresh_positions_skips_live_greek_sweep_with_fresh_cache_window():
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._account_id = "U123"
    engine._spx_proxy_price_async = AsyncMock(return_value=7000.0)

    # Populate signature cache and mark live-greek refresh as very recent.
    signature = ("SPY", "20260320", 590.0, "C", "OPT")
    engine._greeks_cache_by_contract[signature] = {
        "delta": 0.25,
        "gamma": 0.01,
        "theta": -0.5,
        "vega": 4.0,
        "iv": 0.18,
        "undPrice": 590.0,
    }
    engine._last_live_greeks_refresh_monotonic = 10_000.0

    opt_contract = SimpleNamespace(
        conId=505,
        secType="OPT",
        symbol="SPY",
        localSymbol="SPY  260320C00590000",
        exchange="SMART",
        currency="USD",
        strike=590.0,
        right="C",
        lastTradeDateOrContractMonth="20260320",
        multiplier="100",
    )
    engine._ib.positions.return_value = [SimpleNamespace(contract=opt_contract, position=1.0, avgCost=10.0)]
    engine._ib.portfolio.return_value = [
        SimpleNamespace(
            contract=opt_contract,
            marketPrice=11.0,
            marketValue=1100.0,
            unrealizedPNL=100.0,
            realizedPNL=0.0,
            averageCost=10.0,
        )
    ]
    # If live sweep runs, this would be called; we expect zero calls.
    engine._ib.reqMktData.return_value = SimpleNamespace(modelGreeks=None, bidGreeks=None, askGreeks=None, lastGreeks=None)

    from desktop.engine import ib_engine as ib_engine_module
    monotonic_mock = MagicMock(return_value=10_010.0)  # 10s since last refresh (< 60s default)
    original_monotonic = ib_engine_module._time_mod.monotonic
    ib_engine_module._time_mod.monotonic = monotonic_mock
    try:
        rows = await engine.refresh_positions()
    finally:
        ib_engine_module._time_mod.monotonic = original_monotonic

    assert len(rows) == 1
    assert engine._ib.reqMktData.call_count == 0
    assert rows[0].greeks_source is None


@pytest.mark.asyncio
async def test_disconnect_cancels_active_greek_streams():
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._manual_disconnect_requested = False
    contract = SimpleNamespace(conId=9001, localSymbol="SPY  260320C00600000", secType="OPT")
    ticker = SimpleNamespace()
    engine._greek_tickers = {9001: (contract, ticker)}
    engine._chain_tickers = {}
    engine._ib.isConnected.return_value = False

    await engine.disconnect()

    engine._ib.cancelMktData.assert_called_once_with(contract)
    assert engine._greek_tickers == {}


@pytest.mark.asyncio
async def test_refresh_positions_reports_total_delta_in_spx_equivalent_units():
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._account_id = "U123"
    engine._spx_proxy_price_async = AsyncMock(return_value=7000.0)

    stock_contract = SimpleNamespace(
        conId=700,
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
    engine._symbol_betas = {"AAPL": 1.0}
    engine._ib.positions.return_value = [SimpleNamespace(contract=stock_contract, position=100.0, avgCost=100.0)]
    engine._ib.portfolio.return_value = [
        SimpleNamespace(
            contract=stock_contract,
            marketPrice=200.0,
            marketValue=20000.0,
            unrealizedPNL=0.0,
            realizedPNL=0.0,
            averageCost=100.0,
        )
    ]

    captured_risk = {}

    def _capture(risk):
        captured_risk["risk"] = risk

    engine.risk_updated.connect(_capture)

    await engine.refresh_positions()

    risk = captured_risk["risk"]
    assert risk.total_delta == pytest.approx(risk.total_spx_delta)


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
