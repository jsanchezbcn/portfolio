"""desktop/tests/test_ai_risk_tab_tools.py — Unit tests for AI/Risk tab function-calling tools.

Tests the tool handlers that allow LLM to fetch data on-demand instead of
pre-loading everything into prompts.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from desktop.ui.ai_risk_tab import AIRiskTab
from desktop.engine.ib_engine import PositionRow, AccountSummary, MarketSnapshot, OpenOrder, ChainRow


@pytest.fixture
def mock_engine():
    """Mock IBEngine with tool handler dependencies."""
    engine = MagicMock()
    engine.account_id = "U123456"
    engine._account_id = "U123456"
    engine._db_ok = False
    engine.connected = MagicMock()
    engine.disconnected = MagicMock()
    return engine


@pytest.fixture
def ai_risk_tab(mock_engine, qtbot):
    """Create AIRiskTab widget with mocked engine."""
    tab = AIRiskTab(mock_engine)
    qtbot.addWidget(tab)
    return tab


@pytest.mark.asyncio
async def test_tool_get_positions(ai_risk_tab, mock_engine):
    """Test _tool_get_positions returns position list as dicts."""
    mock_engine.refresh_positions = AsyncMock(return_value=[
        PositionRow(
            conid=123,
            symbol="AAPL",
            sec_type="STK",
            underlying="AAPL",
            strike=None,
            right=None,
            expiry=None,
            quantity=100,
            avg_cost=145.0,
            market_price=150.0,
            market_value=15000.0,
            unrealized_pnl=500.0,
            realized_pnl=0.0,
            delta=100.0,
            gamma=None,
            theta=None,
            vega=None,
            iv=None,
            spx_delta=140.0,
        ),
        PositionRow(
            conid=456,
            symbol="MES",
            sec_type="FOP",
            underlying="MES",
            strike=5700,
            right="C",
            expiry="20260320",
            quantity=-2,
            avg_cost=25.0,
            market_price=23.0,
            market_value=-4600.0,
            unrealized_pnl=400.0,
            realized_pnl=0.0,
            delta=-0.65,
            gamma=0.002,
            theta=-12.5,
            vega=8.3,
            iv=0.20,
            spx_delta=-6.5,
        ),
    ])

    result = await ai_risk_tab._tool_get_positions()

    assert len(result) == 2
    assert result[0]["symbol"] == "AAPL"
    assert result[0]["sec_type"] == "STK"
    assert result[0]["quantity"] == 100
    assert result[1]["symbol"] == "MES"
    assert result[1]["sec_type"] == "FOP"
    assert result[1]["delta"] == -0.65
    assert result[1]["gamma"] == 0.002


@pytest.mark.asyncio
async def test_tool_get_positions_prefers_live_snapshot_when_connected(ai_risk_tab, mock_engine):
    mock_engine.is_connected = True
    mock_engine.positions_snapshot = MagicMock(return_value=[
        PositionRow(
            conid=999,
            symbol="MES",
            sec_type="FOP",
            underlying="MES",
            strike=5700,
            right="C",
            expiry="20260320",
            quantity=-1,
            avg_cost=10.0,
            market_price=11.0,
            market_value=-1100.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            delta=-5.0,
            gamma=1.0,
            theta=-20.0,
            vega=300.0,
            iv=0.2,
            spx_delta=-5.0,
        )
    ])
    mock_engine._db_ok = True
    mock_engine._db = MagicMock()
    mock_engine._db.get_cached_positions = AsyncMock(return_value=[{"symbol": "STALE"}])

    result = await ai_risk_tab._tool_get_positions()

    assert len(result) == 1
    assert result[0]["symbol"] == "MES"
    mock_engine._db.get_cached_positions.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_get_positions_uses_cached_db_snapshot(ai_risk_tab, mock_engine):
    mock_engine._db_ok = True
    mock_engine._db = MagicMock()
    mock_engine._db.get_cached_positions = AsyncMock(return_value=[
        {
            "conid": 123,
            "symbol": "AAPL",
            "sec_type": "STK",
            "quantity": 100,
            "delta": 100.0,
        }
    ])
    mock_engine.refresh_positions = AsyncMock()

    result = await ai_risk_tab._tool_get_positions()

    assert len(result) == 1
    assert result[0]["symbol"] == "AAPL"
    mock_engine._db.get_cached_positions.assert_awaited_once()
    mock_engine.refresh_positions.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_get_account(ai_risk_tab, mock_engine):
    """Test _tool_get_account returns account summary as dict."""
    mock_engine.refresh_account = AsyncMock(return_value=AccountSummary(
        account_id="U123456",
        net_liquidation=250000.0,
        total_cash=50000.0,
        init_margin=25000.0,
        maint_margin=20000.0,
        buying_power=225000.0,
        unrealized_pnl=5000.0,
        realized_pnl=1000.0,
    ))

    result = await ai_risk_tab._tool_get_account()

    assert result["account_id"] == "U123456"
    assert result["net_liquidation"] == 250000.0
    assert result["init_margin"] == 25000.0


@pytest.mark.asyncio
async def test_tool_get_account_prefers_live_snapshot_when_connected(ai_risk_tab, mock_engine):
    mock_engine.is_connected = True
    mock_engine.account_snapshot = MagicMock(return_value=AccountSummary(
        account_id="U123456",
        net_liquidation=123000.0,
        total_cash=22000.0,
        init_margin=9000.0,
        maint_margin=7000.0,
        buying_power=101000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
    ))
    mock_engine._db_ok = True
    mock_engine._db = MagicMock()
    mock_engine._db.get_cached_account_snapshot = AsyncMock(return_value={"net_liquidation": 999999.0})

    result = await ai_risk_tab._tool_get_account()

    assert result["net_liquidation"] == 123000.0
    mock_engine._db.get_cached_account_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_get_account_uses_cached_db_snapshot(ai_risk_tab, mock_engine):
    """Test account tool prefers the latest DB snapshot within the short TTL."""
    mock_engine._db_ok = True
    mock_engine._db = MagicMock()
    mock_engine._db.get_cached_account_snapshot = AsyncMock(return_value={
        "account_id": "U123456",
        "net_liquidation": 260000.0,
        "total_cash": 40000.0,
        "buying_power": 210000.0,
        "init_margin": 30000.0,
        "maint_margin": 24000.0,
        "unrealized_pnl": 6000.0,
        "realized_pnl": 1500.0,
    })
    mock_engine.refresh_account = AsyncMock()

    result = await ai_risk_tab._tool_get_account()

    assert result["net_liquidation"] == 260000.0
    mock_engine._db.get_cached_account_snapshot.assert_awaited_once()
    mock_engine.refresh_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_get_open_orders(ai_risk_tab, mock_engine):
    """Test _tool_get_open_orders returns order list as dicts."""
    mock_engine.get_open_orders = AsyncMock(return_value=[
        OpenOrder(
            order_id=123,
            perm_id=456,
            symbol="MES",
            action="SELL",
            quantity=1,
            order_type="LIMIT",
            limit_price=25.0,
            status="Submitted",
            filled=0.0,
            remaining=1.0,
            avg_fill_price=0.0,
        ),
    ])

    result = await ai_risk_tab._tool_get_open_orders()

    assert len(result) == 1
    assert result[0]["order_id"] == 123
    assert result[0]["order_type"] == "LIMIT"
    assert result[0]["limit_price"] == 25.0


@pytest.mark.asyncio
async def test_tool_get_market_snapshot(ai_risk_tab, mock_engine):
    """Test _tool_get_market_snapshot returns price data as dict."""
    mock_engine.get_market_snapshot = AsyncMock(return_value=MarketSnapshot(
        symbol="ES",
        last=5850.5,
        bid=5850.25,
        ask=5850.75,
            high=5855.0,
            low=5840.0,
        close=5840.0,
        volume=125000,
        timestamp="2026-03-06T10:30:00Z",
    ))

    result = await ai_risk_tab._tool_get_market_snapshot("ES", "FUT", "CME")

    assert result["symbol"] == "ES"
    assert result["last"] == 5850.5
    assert result["bid"] == 5850.25
    assert result["ask"] == 5850.75


@pytest.mark.asyncio
async def test_tool_get_bid_ask_from_cache(ai_risk_tab, mock_engine):
    """Test _tool_get_bid_ask returns option quote data and caches it."""
    mock_engine.get_bid_ask_for_legs = AsyncMock(return_value=[{"bid": 25.0, "ask": 26.0, "mid": 25.5}])

    result = await ai_risk_tab._tool_get_bid_ask("MES", 5700, "20260320", "C", "FOP", "CME")
    result_2 = await ai_risk_tab._tool_get_bid_ask("MES", 5700, "20260320", "C", "FOP", "CME")

    assert result["symbol"] == "MES"
    assert result["strike"] == 5700
    assert result["expiry"] == "20260320"
    assert result["right"] == "C"
    assert result["bid"] == 25.0
    assert result["ask"] == 26.0
    assert result["mid"] == 25.5
    assert result["spread"] == 1.0
    assert result_2["mid"] == 25.5
    mock_engine.get_bid_ask_for_legs.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_get_bid_ask_no_cache(ai_risk_tab, mock_engine):
    """Test _tool_get_bid_ask validates missing option fields."""

    result = await ai_risk_tab._tool_get_bid_ask("MES", None, None, None, "FOP", "CME")

    assert result["symbol"] == "MES"
    assert result["mid"] is None
    assert "require strike" in result["error"].lower()


@pytest.mark.asyncio
async def test_tool_get_bid_ask_for_stock_uses_snapshot(ai_risk_tab, mock_engine):
    """Test stock quotes are derived from market snapshots."""
    mock_engine.get_market_snapshot = AsyncMock(return_value=MarketSnapshot(
        symbol="AAPL",
        last=210.0,
        bid=209.8,
        ask=210.2,
        high=211.0,
        low=208.0,
        close=208.5,
        volume=100,
        timestamp="2026-03-06T10:30:00Z",
    ))

    result = await ai_risk_tab._tool_get_bid_ask("AAPL", None, None, None, "STK", "SMART")

    assert result["symbol"] == "AAPL"
    assert result["bid"] == 209.8
    assert result["ask"] == 210.2
    assert result["spread"] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_tool_get_trade_bid_ask_aggregates_multi_leg(ai_risk_tab, mock_engine):
    """Test multi-leg quote aggregation returns debit/credit estimates."""
    mock_engine.get_bid_ask_for_legs = AsyncMock(return_value=[
        {"bid": 4.0, "ask": 4.4, "mid": 4.2},
        {"bid": 1.2, "ask": 1.4, "mid": 1.3},
    ])
    legs = [
        {"symbol": "MES", "action": "BUY", "qty": 1, "sec_type": "FOP", "strike": 5700, "right": "P", "expiry": "20260320", "exchange": "CME"},
        {"symbol": "MES", "action": "SELL", "qty": 1, "sec_type": "FOP", "strike": 5650, "right": "P", "expiry": "20260320", "exchange": "CME"},
    ]

    result = await ai_risk_tab._tool_get_trade_bid_ask(legs)

    assert len(result["legs"]) == 2
    assert result["natural_net_debit"] == pytest.approx(3.2)
    assert result["mid_net_debit"] == pytest.approx(2.9)
    assert result["estimated_slippage_vs_mid"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_tool_get_positions_uses_five_minute_cache(ai_risk_tab, mock_engine):
    """Test repeated position calls reuse the snapshot cache within the TTL window."""
    mock_engine.refresh_positions = AsyncMock(return_value=[
        PositionRow(
            conid=123,
            symbol="AAPL",
            sec_type="STK",
            underlying="AAPL",
            strike=None,
            right=None,
            expiry=None,
            quantity=100,
            avg_cost=145.0,
            market_price=150.0,
            market_value=15000.0,
            unrealized_pnl=500.0,
            realized_pnl=0.0,
            delta=100.0,
            gamma=None,
            theta=None,
            vega=None,
            iv=None,
            spx_delta=4.2,
        ),
    ])

    await ai_risk_tab._tool_get_positions()
    await ai_risk_tab._tool_get_positions()

    mock_engine.refresh_positions.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_get_portfolio_greeks_uses_one_minute_cache(ai_risk_tab, mock_engine):
    """Test repeated aggregate-greeks calls reuse the fresh 1-minute cache."""
    mock_engine.refresh_positions = AsyncMock(return_value=[
        PositionRow(
            conid=456,
            symbol="MES",
            sec_type="FOP",
            underlying="MES",
            strike=5700,
            right="C",
            expiry="20260320",
            quantity=-2,
            avg_cost=25.0,
            market_price=23.0,
            market_value=-4600.0,
            unrealized_pnl=400.0,
            realized_pnl=0.0,
            delta=-65.0,
            gamma=0.2,
            theta=-25.0,
            vega=16.6,
            iv=0.20,
            spx_delta=-6.5,
        ),
    ])

    first = await ai_risk_tab._tool_get_portfolio_greeks()
    second = await ai_risk_tab._tool_get_portfolio_greeks()

    assert first["total_gamma"] == 0.2
    assert second["greeks_coverage"] == 1.0
    mock_engine.refresh_positions.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_get_portfolio_greeks_prefers_cached_db_aggregates(ai_risk_tab, mock_engine):
    """Test portfolio greeks tool uses DB aggregate snapshots before live refresh."""
    mock_engine._db_ok = True
    mock_engine._db = MagicMock()
    mock_engine._db.get_cached_portfolio_greeks = AsyncMock(return_value={
        "total_delta": -12.0,
        "total_gamma": 0.3,
        "total_theta": -40.0,
        "total_vega": 18.0,
        "total_spx_delta": -8.5,
    })
    mock_engine._db.get_cached_positions = AsyncMock(return_value=[
        {
            "symbol": "MES",
            "sec_type": "FOP",
            "quantity": -1,
            "expiry": "20260320",
            "delta": -12.0,
            "gamma": 0.3,
            "theta": -40.0,
            "vega": 18.0,
            "spx_delta": -8.5,
        }
    ])
    mock_engine.refresh_positions = AsyncMock()

    result = await ai_risk_tab._tool_get_portfolio_greeks()

    assert result["total_spx_delta"] == -8.5
    assert result["options_with_greeks"] == 1
    mock_engine.refresh_positions.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_get_portfolio_metrics(ai_risk_tab, mock_engine):
    """Test aggregate portfolio metrics tool combines positions and account data."""
    mock_engine.refresh_positions = AsyncMock(return_value=[
        PositionRow(
            conid=1,
            symbol="AAPL",
            sec_type="STK",
            underlying="AAPL",
            strike=None,
            right=None,
            expiry=None,
            quantity=100,
            avg_cost=145.0,
            market_price=150.0,
            market_value=15000.0,
            unrealized_pnl=500.0,
            realized_pnl=0.0,
            delta=100.0,
            gamma=None,
            theta=None,
            vega=None,
            iv=None,
            spx_delta=4.2,
        ),
        PositionRow(
            conid=2,
            symbol="MES",
            sec_type="FOP",
            underlying="MES",
            strike=5700,
            right="C",
            expiry="20260320",
            quantity=-1,
            avg_cost=25.0,
            market_price=23.0,
            market_value=-2300.0,
            unrealized_pnl=400.0,
            realized_pnl=0.0,
            delta=-20.0,
            gamma=0.1,
            theta=-12.0,
            vega=8.0,
            iv=0.20,
            spx_delta=-6.5,
        ),
    ])
    mock_engine.refresh_account = AsyncMock(return_value=AccountSummary(
        account_id="U123456",
        net_liquidation=100000.0,
        total_cash=20000.0,
        buying_power=80000.0,
        init_margin=10000.0,
        maint_margin=8000.0,
        unrealized_pnl=900.0,
        realized_pnl=0.0,
    ))

    result = await ai_risk_tab._tool_get_portfolio_metrics()

    assert result["total_positions"] == 2
    assert result["options_count"] == 1
    assert result["stocks_count"] == 1
    assert result["nlv"] == 100000.0
    assert result["top_spx_delta_positions"][0]["symbol"] in {"AAPL", "MES"}


@pytest.mark.asyncio
async def test_tool_get_portfolio_metrics_prefers_cached_db_snapshot(ai_risk_tab, mock_engine):
    """Test portfolio metrics tool uses DB metrics cache and cached positions before live refresh."""
    mock_engine._db_ok = True
    mock_engine._db = MagicMock()
    mock_engine._db.get_cached_portfolio_metrics = AsyncMock(return_value={
        "total_positions": 2,
        "total_value": 12700.0,
        "total_spx_delta": -2.3,
        "total_delta": 80.0,
        "total_gamma": 0.1,
        "total_theta": -12.0,
        "total_vega": 8.0,
        "theta_vega_ratio": -1.5,
        "gross_exposure": 17300.0,
        "net_exposure": 12700.0,
        "options_count": 1,
        "stocks_count": 1,
        "nlv": 100000.0,
        "buying_power": 80000.0,
        "init_margin": 10000.0,
        "maint_margin": 8000.0,
    })
    mock_engine._db.get_cached_positions = AsyncMock(return_value=[
        {
            "symbol": "AAPL",
            "sec_type": "STK",
            "quantity": 100,
            "market_value": 15000.0,
            "spx_delta": 4.2,
        },
        {
            "symbol": "MES",
            "sec_type": "FOP",
            "quantity": -1,
            "expiry": "20260320",
            "market_value": -2300.0,
            "delta": -20.0,
            "gamma": 0.1,
            "theta": -12.0,
            "vega": 8.0,
            "spx_delta": -6.5,
        },
    ])
    mock_engine._db.get_cached_account_snapshot = AsyncMock(return_value={
        "account_id": "U123456",
        "net_liquidation": 100000.0,
        "total_cash": 20000.0,
        "buying_power": 80000.0,
        "init_margin": 10000.0,
        "maint_margin": 8000.0,
        "unrealized_pnl": 900.0,
        "realized_pnl": 0.0,
    })
    mock_engine.refresh_positions = AsyncMock()
    mock_engine.refresh_account = AsyncMock()

    result = await ai_risk_tab._tool_get_portfolio_metrics()

    assert result["total_positions"] == 2
    assert result["nlv"] == 100000.0
    assert result["top_spx_delta_positions"][0]["symbol"] == "MES"
    mock_engine.refresh_positions.assert_not_awaited()
    mock_engine.refresh_account.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_get_recent_market_intel(ai_risk_tab, mock_engine):
    """Test recent market intel tool delegates to the shared database layer."""
    mock_engine._db = MagicMock()
    mock_engine._db.get_recent_market_intel = AsyncMock(return_value=[{"source": "llm_risk_audit", "symbol": "PORTFOLIO"}])

    result = await ai_risk_tab._tool_get_recent_market_intel(limit=5)

    assert result[0]["source"] == "llm_risk_audit"
    mock_engine._db.get_recent_market_intel.assert_awaited_once_with(limit=5)


@pytest.mark.asyncio
async def test_tool_analyze_trade_candidate_bundles_context(ai_risk_tab, mock_engine):
    """Test candidate-trade analysis bundles portfolio, spread, and optional simulation context."""
    mock_engine.refresh_positions = AsyncMock(return_value=[
        PositionRow(
            conid=2,
            symbol="MES",
            sec_type="FOP",
            underlying="MES",
            strike=5700,
            right="C",
            expiry="20260320",
            quantity=-1,
            avg_cost=25.0,
            market_price=23.0,
            market_value=-2300.0,
            unrealized_pnl=400.0,
            realized_pnl=0.0,
            delta=-20.0,
            gamma=0.1,
            theta=-12.0,
            vega=8.0,
            iv=0.20,
            spx_delta=-6.5,
        ),
    ])
    mock_engine.refresh_account = AsyncMock(return_value=AccountSummary(
        account_id="U123456",
        net_liquidation=100000.0,
        total_cash=20000.0,
        buying_power=80000.0,
        init_margin=10000.0,
        maint_margin=8000.0,
        unrealized_pnl=900.0,
        realized_pnl=0.0,
    ))
    mock_engine.get_market_snapshot = AsyncMock(return_value=SimpleNamespace(last=18.0, close=18.5))
    mock_engine.get_bid_ask_for_legs = AsyncMock(return_value=[{"bid": 4.0, "ask": 4.4, "mid": 4.2}])
    mock_engine.whatif_order = AsyncMock(return_value={"margin_impact": 2500.0})
    mock_engine._db = MagicMock()
    mock_engine._db.get_recent_market_intel = AsyncMock(return_value=[{"source": "llm_brief"}])

    result = await ai_risk_tab._tool_analyze_trade_candidate(
        [{"symbol": "MES", "action": "BUY", "qty": 1, "sec_type": "FOP", "strike": 5700, "right": "P", "expiry": "20260320", "exchange": "CME"}],
        include_whatif=True,
    )

    assert "portfolio_metrics" in result
    assert "portfolio_greeks" in result
    assert "trade_bid_ask" in result
    assert result["whatif"]["margin_impact"] == 2500.0
    assert result["recent_market_intel"][0]["source"] == "llm_brief"


@pytest.mark.asyncio
async def test_tool_get_chain(ai_risk_tab, mock_engine):
    """Test _tool_get_chain returns filtered chain data."""
    mock_engine.chain_snapshot = MagicMock(return_value=[
        SimpleNamespace(underlying="MES", expiry="20260320", strike=5700, right="C", bid=25.0, ask=26.0, delta=0.65),
        SimpleNamespace(underlying="MES", expiry="20260320", strike=5700, right="P", bid=12.0, ask=13.0, delta=-0.35),
        SimpleNamespace(underlying="MES", expiry="20260417", strike=5750, right="C", bid=18.0, ask=19.0, delta=0.55),
    ])

    result = await ai_risk_tab._tool_get_chain("MES", "20260320")

    assert len(result) == 2  # Only 20260320 expiry
    assert all(r["underlying"] == "MES" for r in result)
    assert all(r["expiry"] == "20260320" for r in result)
    assert result[0]["right"] == "C"
    assert result[1]["right"] == "P"


@pytest.mark.asyncio
async def test_tool_get_chain_fetches_when_snapshot_is_empty(ai_risk_tab, mock_engine):
    """Test _tool_get_chain falls back to engine.get_chain when the cache is empty."""
    mock_engine.chain_snapshot = MagicMock(return_value=[])
    mock_engine.get_chain = AsyncMock(return_value=[
        SimpleNamespace(underlying="AAPL", expiry="20260320", strike=210.0, right="C", bid=4.2, ask=4.5, delta=0.51),
        SimpleNamespace(underlying="AAPL", expiry="20260320", strike=210.0, right="P", bid=3.8, ask=4.0, delta=-0.49),
    ])

    result = await ai_risk_tab._tool_get_chain("AAPL", "20260320")

    assert len(result) == 2
    assert all(r["underlying"] == "AAPL" for r in result)
    mock_engine.get_chain.assert_awaited_once()


@pytest.mark.asyncio
async def test_tool_log_summary_for_stock_bid_ask_omits_placeholders(ai_risk_tab):
    """Stock quote log lines should not show fake action/qty placeholders."""
    summary = ai_risk_tab._summarize_tool_payload(
        "get_bid_ask",
        {"symbol": "AAPL", "sec_type": "STK", "exchange": "SMART"},
    )

    assert summary == "AAPL STK @SMART"


@pytest.mark.asyncio
async def test_tool_whatif_order_success(ai_risk_tab, mock_engine):
    """Test _tool_whatif_order calls engine and returns result."""
    mock_engine.whatif_order = AsyncMock(return_value={
        "commission": 1.40,
        "margin_impact": 2500.0,
        "delta_change": -0.65,
        "gamma_change": 0.002,
    })

    legs = [{"symbol": "MES", "action": "SELL", "qty": 1, "strike": 5700, "right": "C", "expiry": "20260320"}]
    result = await ai_risk_tab._tool_whatif_order(legs)

    assert "commission" in result
    assert result["commission"] == 1.40
    assert result["delta_change"] == -0.65
    mock_engine.whatif_order.assert_called_once()


@pytest.mark.asyncio
async def test_tool_whatif_order_error(ai_risk_tab, mock_engine):
    """Test _tool_whatif_order handles errors gracefully."""
    mock_engine.whatif_order = AsyncMock(side_effect=Exception("Contract not found"))

    legs = [{"symbol": "INVALID", "action": "SELL", "qty": 1}]
    result = await ai_risk_tab._tool_whatif_order(legs)

    assert "error" in result
    assert "Contract not found" in result["error"]


@pytest.mark.asyncio
async def test_tool_get_recent_fills_db_available(ai_risk_tab, mock_engine):
    """Test _tool_get_recent_fills returns fills when DB available."""
    mock_engine._db_ok = True
    mock_engine._db = MagicMock()
    mock_engine._db.get_fills = AsyncMock(return_value=[
        {"symbol": "MES", "action": "BUY", "qty": 2, "price": 25.5, "timestamp": "2026-03-06 10:30:00"},
        {"symbol": "AAPL", "action": "SELL", "qty": 100, "price": 150.0, "timestamp": "2026-03-06 09:15:00"},
    ])

    result = await ai_risk_tab._tool_get_recent_fills(limit=20)

    assert len(result) == 2
    assert result[0]["symbol"] == "MES"
    assert result[1]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_tool_get_recent_fills_db_unavailable(ai_risk_tab, mock_engine):
    """Test _tool_get_recent_fills returns empty list when DB unavailable."""
    mock_engine._db_ok = False

    result = await ai_risk_tab._tool_get_recent_fills(limit=20)

    assert result == []


@pytest.mark.asyncio
async def test_tool_get_risk_breaches(ai_risk_tab, mock_engine):
    """Test _tool_get_risk_breaches computes violations."""
    mock_engine.refresh_positions = AsyncMock(return_value=[
        PositionRow(
            conid=1,
            symbol="MES",
            sec_type="FOP",
            underlying="MES",
            strike=5700.0,
            right="C",
            expiry="20260320",
            quantity=-5,
            avg_cost=25.0,
            market_price=20.0,
            market_value=-10000.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            delta=-0.65,
            gamma=0.01,
            theta=-62.5,
            vega=20.0,
            iv=0.2,
            spx_delta=-15.0,
        ),
    ])
    mock_engine.refresh_account = AsyncMock(return_value=AccountSummary(
        account_id="U123456",
        net_liquidation=50000.0,
        total_cash=10000.0,
        buying_power=60000.0,
        init_margin=5000.0,
        maint_margin=4000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
    ))
    mock_engine.get_market_snapshot = AsyncMock(return_value=SimpleNamespace(last=18.0, close=18.5))
    mock_engine.account_id = "U123456"

    result = await ai_risk_tab._tool_get_risk_breaches()

    # Result should be a list of breach dicts
    assert isinstance(result, list)
    # May be empty or have breaches depending on regime limits
    for breach in result:
        assert "metric" in breach
        assert "current" in breach
        assert "limit" in breach


def test_create_tools_for_session(ai_risk_tab):
    """Test _create_tools_for_session returns list of tool definitions."""
    tools = ai_risk_tab._create_tools_for_session()

    assert len(tools) == 17  # Updated: 14 original + 3 new strategy tools
    # Copilot SDK returns Tool objects with metadata (not callables)
    assert all(hasattr(tool, "name") for tool in tools)
    assert all(hasattr(tool, "description") for tool in tools)


@pytest.mark.asyncio
async def test_audit_uses_fresh_data(ai_risk_tab, mock_engine):
    """Test _async_audit fetches fresh data instead of using pre-loaded context."""
    mock_engine._db = MagicMock()
    mock_engine.refresh_positions = AsyncMock(return_value=[
        PositionRow(conid=456, symbol="MES", sec_type="FOP", underlying="MES", strike=5700, right="C", expiry="20260320",
                quantity=-2, avg_cost=25.0, market_price=23.0, market_value=-4600.0,
                unrealized_pnl=400.0, realized_pnl=0.0,
                delta=-0.65, gamma=0.002, theta=-25.0, vega=16.6, iv=0.20, spx_delta=-6.5),
    ])
    mock_engine.refresh_account = AsyncMock(return_value=AccountSummary(
        account_id="U123456",
        net_liquidation=100000.0,
            total_cash=20000.0,
            buying_power=80000.0,
        init_margin=10000.0,
        maint_margin=8000.0,
        unrealized_pnl=400.0,
        realized_pnl=0.0,
    ))
    mock_engine.get_market_snapshot = AsyncMock(return_value=SimpleNamespace(last=15.0, close=15.5))

    with patch("desktop.ui.ai_risk_tab.LLMRiskAuditor") as mock_auditor_cls:
        mock_auditor = MagicMock()
        mock_auditor._model = "gpt-5-mini"
        mock_auditor.audit_now = AsyncMock(return_value={
            "headline": "Portfolio looks good",
            "body": "No major issues",
            "urgency": "low",
        })
        mock_auditor_cls.return_value = mock_auditor

        # Call audit without pre-loaded context
        ai_risk_tab._context = {}
        await ai_risk_tab._async_audit()

        # Verify data was fetched fresh (risk-breach detection may refresh account again)
        assert mock_engine.refresh_positions.call_count >= 1
        assert mock_engine.refresh_account.call_count >= 1
        mock_auditor_cls.assert_called_once_with(db=mock_engine._db)
        mock_auditor.audit_now.assert_called_once()


@pytest.mark.asyncio
async def test_suggest_uses_fresh_data(ai_risk_tab, mock_engine):
    """Test _async_suggest fetches fresh data instead of using pre-loaded context."""
    mock_engine.account_id = "U123456"
    mock_engine._db = MagicMock()
    mock_engine.refresh_positions = AsyncMock(return_value=[
        PositionRow(conid=456, symbol="MES", sec_type="FOP", underlying="MES", strike=5700, right="C", expiry="20260320",
                quantity=-2, avg_cost=25.0, market_price=23.0, market_value=-4600.0,
                unrealized_pnl=400.0, realized_pnl=0.0,
                delta=-0.65, gamma=0.002, theta=-25.0, vega=16.6, iv=0.20, spx_delta=-6.5),
    ])
    mock_engine.refresh_account = AsyncMock(return_value=AccountSummary(
        account_id="U123456",
        net_liquidation=100000.0,
        total_cash=20000.0,
        buying_power=80000.0,
        init_margin=10000.0,
        maint_margin=8000.0,
        unrealized_pnl=400.0,
        realized_pnl=0.0,
    ))
    mock_engine.get_market_snapshot = AsyncMock(return_value=SimpleNamespace(last=15.0, close=15.5))

    with patch("desktop.ui.ai_risk_tab.LLMRiskAuditor") as mock_auditor_cls:
        mock_auditor = MagicMock()
        mock_auditor._model = "gpt-5-mini"
        mock_auditor.suggest_trades = AsyncMock(return_value=[])
        mock_auditor_cls.return_value = mock_auditor

        # Mock RiskRegimeLoader to avoid import issues
        with patch("desktop.ui.ai_risk_tab.RiskRegimeLoader") as mock_loader_cls:
            mock_loader = MagicMock()
            mock_loader.get_effective_limits = MagicMock(return_value=("normal", {}))
            mock_loader_cls.return_value = mock_loader

            # Call suggest without pre-loaded context
            ai_risk_tab._context = {}
            await ai_risk_tab._async_suggest()

            # Verify data was fetched fresh
            assert mock_engine.refresh_positions.call_count >= 1
            assert mock_engine.refresh_account.call_count >= 1
            mock_auditor_cls.assert_called_once_with(db=mock_engine._db)
            mock_auditor.suggest_trades.assert_called_once()


@pytest.mark.asyncio
async def test_refresh_context_is_lightweight(ai_risk_tab, mock_engine):
    """Test _async_refresh_context no longer pre-loads 180KB of JSON."""
    mock_engine.refresh_positions = AsyncMock(return_value=[
        PositionRow(conid=123, symbol="AAPL", sec_type="STK", underlying="AAPL", strike=None, right=None, expiry=None,
                quantity=100, avg_cost=145.0, market_price=150.0, market_value=15000.0,
                unrealized_pnl=500.0, realized_pnl=0.0,
                delta=100.0, gamma=None, theta=None, vega=None, iv=None, spx_delta=140.0),
    ])
    mock_engine.refresh_account = AsyncMock(return_value=AccountSummary(
        account_id="U123456",
            total_cash=20000.0,
            buying_power=80000.0,
            init_margin=10000.0,
            maint_margin=8000.0,
            unrealized_pnl=500.0,
            realized_pnl=0.0,
        net_liquidation=100000.0,
    ))
    mock_engine.get_market_snapshot = AsyncMock(return_value=SimpleNamespace(last=15.0, close=15.5))

    with patch("desktop.ui.ai_risk_tab.BreachDetector") as mock_detector_cls:
        mock_detector = MagicMock()
        mock_detector.check = MagicMock(return_value=[])
        mock_detector_cls.return_value = mock_detector

        await ai_risk_tab._async_refresh_context()

        # Context should exist but be minimal
        assert ai_risk_tab._context is not None
        assert "summary" in ai_risk_tab._context
        assert "nlv" in ai_risk_tab._context
        # Should NOT have massive arrays like old architecture
        assert "positions" not in ai_risk_tab._context
        assert "open_orders" not in ai_risk_tab._context
        assert "recent_fills" not in ai_risk_tab._context


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
