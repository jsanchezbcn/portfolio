from datetime import date

from risk_engine.regime_detector import MarketRegime, RegimeLimits

from agent_tools.portfolio_tools import PortfolioTools
from models.unified_position import InstrumentType, UnifiedPosition


def test_portfolio_summary_theta_vega_ratio_uses_absolute_values() -> None:
    tools = PortfolioTools()
    positions = [
        UnifiedPosition(
            symbol="AAPL",
            instrument_type=InstrumentType.EQUITY,
            broker="ibkr",
            quantity=10,
            avg_price=100,
            market_value=1000,
            unrealized_pnl=10,
            theta=120,
            vega=-360,
        )
    ]

    summary = tools.get_portfolio_summary(positions)

    assert round(summary["theta_vega_ratio"], 2) == 0.33
    assert summary["theta_vega_zone"] == "green"


def test_check_risk_limits_detects_violations() -> None:
    tools = PortfolioTools()
    summary = {
        "total_spx_delta": 700,
        "total_vega": -3000,
        "total_theta": 50,
        "total_gamma": 60,
    }
    regime = MarketRegime(
        name="low_volatility",
        condition="",
        description="",
        limits=RegimeLimits(
            max_beta_delta=600,
            max_negative_vega=-500,
            min_daily_theta=100,
            max_gamma=50,
            allowed_strategies=[],
        ),
    )

    violations = tools.check_risk_limits(summary, regime)

    assert len(violations) == 4


def test_get_gamma_risk_by_dte_groups_expected_buckets() -> None:
    tools = PortfolioTools()
    positions = [
        UnifiedPosition(
            symbol="SPY 240216C500",
            instrument_type=InstrumentType.OPTION,
            broker="ibkr",
            quantity=1,
            avg_price=1,
            market_value=1,
            unrealized_pnl=0,
            underlying="SPY",
            strike=500,
            expiration=date(2024, 2, 16),
            option_type="call",
            gamma=2.0,
        ),
        UnifiedPosition(
            symbol="SPY 240301P490",
            instrument_type=InstrumentType.OPTION,
            broker="ibkr",
            quantity=1,
            avg_price=1,
            market_value=1,
            unrealized_pnl=0,
            underlying="SPY",
            strike=490,
            expiration=date(2024, 3, 1),
            option_type="put",
            gamma=3.0,
        ),
        UnifiedPosition(
            symbol="SPY 240419C510",
            instrument_type=InstrumentType.OPTION,
            broker="ibkr",
            quantity=1,
            avg_price=1,
            market_value=1,
            unrealized_pnl=0,
            underlying="SPY",
            strike=510,
            expiration=date(2024, 4, 19),
            option_type="call",
            gamma=4.0,
        ),
    ]

    grouped = tools.get_gamma_risk_by_dte(positions)

    assert sum(grouped.values()) == 9.0
    assert set(grouped.keys()).issubset({"0-7", "8-30", "31-60", "60+"})
