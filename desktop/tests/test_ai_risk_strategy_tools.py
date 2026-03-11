"""Tests for AI Risk tab strategy tools: get_strategy_snapshot, validate_strategies, optimize_capital."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

from desktop.ui.ai_risk_tab import AIRiskTab
from desktop.models.strategy_reconstructor import StrategyGroup
from desktop.engine.ib_engine import PositionRow, AccountSummary


@pytest.fixture
def mock_engine():
    """Mock IBEngine with strategy snapshots."""
    engine = MagicMock()
    engine.account_id = "U123"
    engine._account_id = "U123"
    engine._db_ok = False
    engine.connected = MagicMock()
    engine.disconnected = MagicMock()
    engine.strategy_snapshot = MagicMock(return_value=[])
    return engine


@pytest.fixture
def ai_risk_tab(qtbot, mock_engine):
    """Create AIRiskTab instance with mocked dependencies."""
    tab = AIRiskTab(engine=mock_engine)
    qtbot.addWidget(tab)
    return tab


def _make_position_row(
    conid: int = 1,
    symbol: str = "ES",
    quantity: float = 1.0,
    strike: float | None = None,
    right: str = "",
    expiry: str = "",
    sec_type: str = "FUT",
    delta: float | None = None,
    gamma: float | None = None,
    theta: float | None = None,
    vega: float | None = None,
    spx_delta: float | None = None,
) -> PositionRow:
    """Helper to create PositionRow for tests."""
    return PositionRow(
        conid=conid,
        symbol=symbol,
        quantity=quantity,
        sec_type=sec_type,
        underlying=symbol,
        strike=strike,
        right=right,
        expiry=expiry,
        market_price=100.0,
        market_value=quantity * 100.0,
        avg_cost=99.0,
        unrealized_pnl=1.0,
        realized_pnl=0.0,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        iv=None,
        spx_delta=spx_delta,
        greeks_source=None,
        underlying_price=None,
        combo_description=None,
    )


def _make_strategy_group(
    strategy_name: str = "Bull Call Spread",
    underlying: str = "ES",
    legs: list[PositionRow] | None = None,
    net_delta: float | None = None,
    net_gamma: float | None = None,
    net_theta: float | None = None,
    net_vega: float | None = None,
) -> StrategyGroup:
    """Helper to create StrategyGroup for tests."""
    if legs is None:
        legs = [
            _make_position_row(conid=1, strike=5900.0, right="C", expiry="20260320", sec_type="FOP", quantity=1.0),
            _make_position_row(conid=2, strike=5910.0, right="C", expiry="20260320", sec_type="FOP", quantity=-1.0),
        ]
    
    return StrategyGroup(
        association_id="test_id_123",
        strategy_name=strategy_name,
        underlying=underlying,
        legs=legs,
        matched_by="test",
        expiry_label="Mar 20",
        strategy_family="vertical",
        net_delta=net_delta,
        net_gamma=net_gamma,
        net_theta=net_theta,
        net_vega=net_vega,
    )


@pytest.mark.asyncio
async def test_get_strategy_snapshot_empty(ai_risk_tab):
    """Test get_strategy_snapshot with no strategies."""
    ai_risk_tab._engine.strategy_snapshot.return_value = []
    
    result = await ai_risk_tab._tool_get_strategy_snapshot()
    
    assert result == []
    ai_risk_tab._engine.strategy_snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_get_strategy_snapshot_with_strategies(ai_risk_tab):
    """Test get_strategy_snapshot returns strategy data."""
    legs = [
        _make_position_row(conid=1, strike=5900.0, right="C", expiry="20260320", sec_type="FOP", quantity=1.0, delta=5.0, gamma=0.25, theta=-1.0, vega=2.5),
        _make_position_row(conid=2, strike=5910.0, right="C", expiry="20260320", sec_type="FOP", quantity=-1.0, delta=5.0, gamma=0.25, theta=-1.0, vega=2.5),
    ]
    strategy = _make_strategy_group(
        strategy_name="Bull Call Spread",
        underlying="ES",
        legs=legs,
    )
    ai_risk_tab._engine.strategy_snapshot.return_value = [strategy]
    
    result = await ai_risk_tab._tool_get_strategy_snapshot()
    
    assert len(result) == 1
    assert result[0]["strategy_name"] == "Bull Call Spread"
    assert result[0]["underlying"] == "ES"
    assert result[0]["net_delta"] == 10.0  # 5.0 + 5.0
    assert result[0]["leg_count"] == 2
    assert result[0]["strategy_family"] == "vertical"


@pytest.mark.asyncio
async def test_get_strategy_snapshot_multiple_strategies(ai_risk_tab):
    """Test get_strategy_snapshot with multiple strategies."""
    strategies = [
        _make_strategy_group(strategy_name="Bull Call Spread", underlying="ES"),
        _make_strategy_group(strategy_name="Iron Condor", underlying="SPY"),
        _make_strategy_group(strategy_name="Long Call", underlying="AAPL"),
    ]
    ai_risk_tab._engine.strategy_snapshot.return_value = strategies
    
    result = await ai_risk_tab._tool_get_strategy_snapshot()
    
    assert len(result) == 3
    assert {s["strategy_name"] for s in result} == {"Bull Call Spread", "Iron Condor", "Long Call"}


@pytest.mark.asyncio
async def test_validate_strategies_all_valid(ai_risk_tab):
    """Test validate_strategies when all strategies are correctly formed."""
    strategy = _make_strategy_group(
        strategy_name="Bull Call Spread",
        net_delta=5.0,
        net_theta=-1.0,
        net_vega=3.0,
    )
    ai_risk_tab._engine.strategy_snapshot.return_value = [strategy]
    
    result = await ai_risk_tab._tool_validate_strategies()
    
    assert result["total_strategies"] == 1
    assert result["valid_count"] == 1
    assert result["issues_count"] == 0
    assert result["issues"] == []


@pytest.mark.asyncio
async def test_validate_strategies_incomplete_spread(ai_risk_tab):
    """Test validate_strategies detects incomplete spreads."""
    # Bull Call Spread with only 1 leg (should have 2)
    leg = _make_position_row(conid=1, strike=5900.0, right="C", expiry="20260320", sec_type="FOP", quantity=1.0)
    strategy = StrategyGroup(
        association_id="incomplete_123",
        strategy_name="Bull Call Spread",
        underlying="ES",
        legs=[leg],
        matched_by="test",
        expiry_label="Mar 20",
        strategy_family="vertical",
    )
    ai_risk_tab._engine.strategy_snapshot.return_value = [strategy]
    
    result = await ai_risk_tab._tool_validate_strategies()
    
    assert result["total_strategies"] == 1
    assert result["valid_count"] == 0
    assert result["issues_count"] == 1
    assert len(result["issues"]) == 1
    assert "should have 2 legs but has 1" in result["issues"][0]["issues"][0]


@pytest.mark.asyncio
async def test_validate_strategies_unbalanced_quantities(ai_risk_tab):
    """Test validate_strategies detects unbalanced spread quantities."""
    legs = [
        _make_position_row(conid=1, strike=5900.0, right="C", expiry="20260320", sec_type="FOP", quantity=2.0),
        _make_position_row(conid=2, strike=5910.0, right="C", expiry="20260320", sec_type="FOP", quantity=-1.0),
    ]
    strategy = StrategyGroup(
        association_id="unbalanced_123",
        strategy_name="Bull Call Spread",
        underlying="ES",
        legs=legs,
        matched_by="test",
        expiry_label="Mar 20",
        strategy_family="vertical",
    )
    ai_risk_tab._engine.strategy_snapshot.return_value = [strategy]
    
    result = await ai_risk_tab._tool_validate_strategies()
    
    assert result["issues_count"] == 1
    assert "Unbalanced spread quantities" in result["issues"][0]["issues"][0]


@pytest.mark.asyncio
async def test_validate_strategies_high_net_delta(ai_risk_tab):
    """Test validate_strategies detects excessive net delta."""
    legs = [
        _make_position_row(conid=1, strike=5900.0, right="C", expiry="20260320", sec_type="FOP", quantity=1.0, delta=75.0),
        _make_position_row(conid=2, strike=5910.0, right="C", expiry="20260320", sec_type="FOP", quantity=-1.0, delta=75.0),
    ]
    strategy = _make_strategy_group(
        strategy_name="Bull Call Spread",
        legs=legs,
    )
    ai_risk_tab._engine.strategy_snapshot.return_value = [strategy]
    
    result = await ai_risk_tab._tool_validate_strategies()
    
    assert result["issues_count"] == 1
    assert "High net delta" in result["issues"][0]["issues"][0]


@pytest.mark.asyncio
async def test_validate_strategies_conflicting_greeks(ai_risk_tab):
    """Test validate_strategies detects unusual Greek combinations."""
    legs = [
        _make_position_row(conid=1, strike=5900.0, right="C", expiry="20260320", sec_type="FOP", quantity=1.0, theta=2.5, vega=-40.0),
        _make_position_row(conid=2, strike=5910.0, right="C", expiry="20260320", sec_type="FOP", quantity=-1.0, theta=2.5, vega=-40.0),
    ]
    strategy = _make_strategy_group(
        strategy_name="Bull Call Spread",
        legs=legs,
    )
    ai_risk_tab._engine.strategy_snapshot.return_value = [strategy]
    
    result = await ai_risk_tab._tool_validate_strategies()
    
    assert result["issues_count"] == 1
    assert "Unusual" in result["issues"][0]["issues"][0]
    assert "positive theta" in result["issues"][0]["issues"][0]


@pytest.mark.asyncio
async def test_validate_strategies_specific_id(ai_risk_tab):
    """Test validate_strategies with specific strategy_id."""
    strategy1 = StrategyGroup(
        association_id="strat_1",
        strategy_name="Bull Call Spread",
        underlying="ES",
        legs=[_make_position_row()],
        matched_by="test",
        expiry_label="Mar 20",
        strategy_family="vertical",
    )
    strategy2 = StrategyGroup(
        association_id="strat_2",
        strategy_name="Iron Condor",
        underlying="SPY",
        legs=[_make_position_row()],  # Only 1 leg, should have 4
        matched_by="test",
        expiry_label="Mar 20",
        strategy_family="iron_structure",
    )
    ai_risk_tab._engine.strategy_snapshot.return_value = [strategy1, strategy2]
    
    result = await ai_risk_tab._tool_validate_strategies(strategy_id="strat_2")
    
    assert result["total_strategies"] == 1
    assert result["issues"][0]["association_id"] == "strat_2"


@pytest.mark.asyncio
async def test_validate_strategies_id_not_found(ai_risk_tab):
    """Test validate_strategies with non-existent strategy_id."""
    ai_risk_tab._engine.strategy_snapshot.return_value = []
    
    result = await ai_risk_tab._tool_validate_strategies(strategy_id="nonexistent")
    
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_optimize_capital_empty_portfolio(ai_risk_tab):
    """Test optimize_capital with no strategies."""
    ai_risk_tab._engine.strategy_snapshot.return_value = []
    ai_risk_tab._get_positions_data = AsyncMock(return_value=[])
    ai_risk_tab._get_account_data = AsyncMock(return_value=AccountSummary(
        account_id="U123",
        net_liquidation=100000.0,
        total_cash=95000.0,
        buying_power=180000.0,
        init_margin=10000.0,
        maint_margin=8000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
    ))
    
    result = await ai_risk_tab._tool_optimize_capital()
    
    assert result["strategies_analyzed"] == 0
    assert result["suggestions_count"] == 0
    assert result["current_margin_used"] == 10000.0


@pytest.mark.asyncio
async def test_optimize_capital_naked_short_call(ai_risk_tab):
    """Test optimize_capital suggests converting naked short to spread."""
    leg = _make_position_row(conid=1, strike=5900.0, right="C", expiry="20260320", sec_type="FOP", quantity=-1.0)
    strategy = StrategyGroup(
        association_id="naked_short",
        strategy_name="Short Call",
        underlying="ES",
        legs=[leg],
        matched_by="test",
        expiry_label="Mar 20",
        strategy_family="short_option",
    )
    ai_risk_tab._engine.strategy_snapshot.return_value = [strategy]
    ai_risk_tab._get_positions_data = AsyncMock(return_value=[])
    ai_risk_tab._get_account_data = AsyncMock(return_value=AccountSummary(
        account_id="U123",
        net_liquidation=100000.0,
        total_cash=95000.0,
        buying_power=160000.0,
        init_margin=20000.0,
        maint_margin=16000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
    ))
    
    result = await ai_risk_tab._tool_optimize_capital()
    
    assert result["strategies_analyzed"] == 1
    assert result["suggestions_count"] >= 1
    suggestion = result["suggestions"][0]
    assert suggestion["current_strategy"] == "Short Call"
    assert "Bear Call Spread" in suggestion["suggestion"]
    assert "legs_to_add" in suggestion
    assert len(suggestion["legs_to_add"]) == 1
    assert suggestion["legs_to_add"][0]["action"] == "BUY"
    assert suggestion["legs_to_add"][0]["right"] == "C"


@pytest.mark.asyncio
async def test_optimize_capital_naked_short_put(ai_risk_tab):
    """Test optimize_capital suggests converting naked short put to spread."""
    leg = _make_position_row(conid=1, strike=5800.0, right="P", expiry="20260320", sec_type="FOP", quantity=-2.0)
    strategy = StrategyGroup(
        association_id="naked_short_put",
        strategy_name="Short Put",
        underlying="ES",
        legs=[leg],
        matched_by="test",
        expiry_label="Mar 20",
        strategy_family="short_option",
    )
    ai_risk_tab._engine.strategy_snapshot.return_value = [strategy]
    ai_risk_tab._get_positions_data = AsyncMock(return_value=[])
    ai_risk_tab._get_account_data = AsyncMock(return_value=AccountSummary(
        account_id="U123",
        net_liquidation=100000.0,
        total_cash=95000.0,
        buying_power=170000.0,
        init_margin=15000.0,
        maint_margin=12000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
    ))
    
    result = await ai_risk_tab._tool_optimize_capital()
    
    assert result["suggestions_count"] >= 1
    suggestion = result["suggestions"][0]
    assert "Bull Put Spread" in suggestion["suggestion"]
    assert suggestion["legs_to_add"][0]["action"] == "BUY"
    assert suggestion["legs_to_add"][0]["right"] == "P"


@pytest.mark.asyncio
async def test_optimize_capital_incomplete_spread(ai_risk_tab):
    """Test optimize_capital suggests completing incomplete spreads."""
    leg = _make_position_row(conid=1, strike=5900.0, right="C", expiry="20260320", sec_type="FOP", quantity=1.0)
    strategy = StrategyGroup(
        association_id="incomplete",
        strategy_name="Bull Call Spread",
        underlying="ES",
        legs=[leg],
        matched_by="test",
        expiry_label="Mar 20",
        strategy_family="vertical",
    )
    ai_risk_tab._engine.strategy_snapshot.return_value = [strategy]
    ai_risk_tab._get_positions_data = AsyncMock(return_value=[])
    ai_risk_tab._get_account_data = AsyncMock(return_value=AccountSummary(
        account_id="U123",
        net_liquidation=100000.0,
        total_cash=95000.0,
        buying_power=190000.0,
        init_margin=5000.0,
        maint_margin=4000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
    ))
    
    result = await ai_risk_tab._tool_optimize_capital()
    
    assert result["suggestions_count"] >= 1
    suggestion = result["suggestions"][0]
    assert "Incomplete spread" in suggestion["current_strategy"]
    assert "Complete" in suggestion["suggestion"]
    assert len(suggestion["legs_to_add"]) == 1


@pytest.mark.asyncio
async def test_optimize_capital_filter_by_underlying(ai_risk_tab):
    """Test optimize_capital with underlying filter."""
    leg1 = _make_position_row(conid=1, symbol="ES", strike=5900.0, right="C", expiry="20260320", sec_type="FOP", quantity=-1.0)
    leg2 = _make_position_row(conid=2, symbol="SPY", strike=580.0, right="C", expiry="20260320", sec_type="OPT", quantity=-1.0)
    
    strategy1 = StrategyGroup(
        association_id="es_short",
        strategy_name="Short Call",
        underlying="ES",
        legs=[leg1],
        matched_by="test",
        expiry_label="Mar 20",
        strategy_family="short_option",
    )
    strategy2 = StrategyGroup(
        association_id="spy_short",
        strategy_name="Short Call",
        underlying="SPY",
        legs=[leg2],
        matched_by="test",
        expiry_label="Mar 20",
        strategy_family="short_option",
    )
    
    ai_risk_tab._engine.strategy_snapshot.return_value = [strategy1, strategy2]
    ai_risk_tab._get_positions_data = AsyncMock(return_value=[])
    ai_risk_tab._get_account_data = AsyncMock(return_value=AccountSummary(
        account_id="U123",
        net_liquidation=100000.0,
        total_cash=95000.0,
        buying_power=160000.0,
        init_margin=20000.0,
        maint_margin=16000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
    ))
    
    result = await ai_risk_tab._tool_optimize_capital(underlying="ES")
    
    assert result["strategies_analyzed"] == 1
    assert all(s["underlying"] == "ES" for s in result["suggestions"])


@pytest.mark.asyncio
async def test_optimize_capital_high_gamma_strategy(ai_risk_tab):
    """Test optimize_capital suggests iron condor for high gamma spreads."""
    legs = [
        _make_position_row(conid=1, strike=5800.0, right="P", expiry="20260320", sec_type="FOP", quantity=1.0, gamma=30.0),
        _make_position_row(conid=2, strike=5790.0, right="P", expiry="20260320", sec_type="FOP", quantity=-1.0, gamma=30.0),
    ]
    strategy = StrategyGroup(
        association_id="high_gamma",
        strategy_name="Bull Put Spread",
        underlying="ES",
        legs=legs,
        matched_by="test",
        expiry_label="Mar 20",
        strategy_family="vertical",
    )
    ai_risk_tab._engine.strategy_snapshot.return_value = [strategy]
    ai_risk_tab._get_positions_data = AsyncMock(return_value=[])
    ai_risk_tab._get_account_data = AsyncMock(return_value=AccountSummary(
        account_id="U123",
        net_liquidation=100000.0,
        total_cash=95000.0,
        buying_power=180000.0,
        init_margin=10000.0,
        maint_margin=8000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
    ))
    
    result = await ai_risk_tab._tool_optimize_capital()
    
    # Should suggest converting to iron condor
    iron_condor_suggestions = [s for s in result["suggestions"] if "Iron Condor" in s["suggestion"]]
    assert len(iron_condor_suggestions) >= 1
    assert "gamma" in iron_condor_suggestions[0]["rationale"].lower()


@pytest.mark.asyncio
async def test_optimize_capital_margin_metrics(ai_risk_tab):
    """Test optimize_capital returns correct margin metrics."""
    ai_risk_tab._engine.strategy_snapshot.return_value = []
    ai_risk_tab._get_positions_data = AsyncMock(return_value=[])
    ai_risk_tab._get_account_data = AsyncMock(return_value=AccountSummary(
        account_id="U123",
        net_liquidation=100000.0,
        total_cash=95000.0,
        buying_power=150000.0,
        init_margin=25000.0,
        maint_margin=20000.0,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
    ))
    
    result = await ai_risk_tab._tool_optimize_capital()
    
    assert result["current_margin_used"] == 25000.0
    assert result["current_margin_pct"] == 25.0  # 25000 / 100000 * 100
    assert result["net_liquidation"] == 100000.0
