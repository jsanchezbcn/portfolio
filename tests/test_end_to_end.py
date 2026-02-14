from __future__ import annotations

import time
from datetime import date

from agent_tools.portfolio_tools import PortfolioTools
from models.unified_position import InstrumentType, UnifiedPosition
from risk_engine.regime_detector import RegimeDetector


def test_end_to_end_metrics_pipeline_under_threshold() -> None:
    start = time.perf_counter()

    positions = [
        UnifiedPosition(
            symbol="AAPL240621C200",
            instrument_type=InstrumentType.OPTION,
            broker="ibkr",
            quantity=2,
            avg_price=1,
            market_value=1,
            unrealized_pnl=0,
            underlying="AAPL",
            strike=200,
            expiration=date(2026, 6, 21),
            option_type="call",
            delta=0.8,
            gamma=1.5,
            theta=25,
            vega=80,
            iv=0.40,
        ),
        UnifiedPosition(
            symbol="MSFT240621P400",
            instrument_type=InstrumentType.OPTION,
            broker="ibkr",
            quantity=1,
            avg_price=1,
            market_value=1,
            unrealized_pnl=0,
            underlying="MSFT",
            strike=400,
            expiration=date(2026, 5, 1),
            option_type="put",
            delta=-0.3,
            gamma=0.7,
            theta=12,
            vega=45,
            iv=0.27,
        ),
    ]

    tools = PortfolioTools()
    summary = tools.get_portfolio_summary(positions)
    gamma_dte = tools.get_gamma_risk_by_dte(positions)
    iv_analysis = tools.get_iv_analysis(positions, {"AAPL": 0.22, "MSFT": 0.16})

    regime_detector = RegimeDetector("config/risk_matrix.yaml")
    regime = regime_detector.detect_regime(vix=18.0, term_structure=1.07, recession_probability=0.20)
    violations = tools.check_risk_limits(summary, regime)

    elapsed = time.perf_counter() - start

    assert "total_spx_delta" in summary
    assert isinstance(gamma_dte, dict)
    assert len(iv_analysis) == 2
    assert isinstance(violations, list)
    assert elapsed < 3.0


def test_analytics_performance_with_200_positions() -> None:
    start = time.perf_counter()

    positions: list[UnifiedPosition] = []
    for idx in range(200):
        symbol = f"AAPL2606{idx:02d}C200"
        positions.append(
            UnifiedPosition(
                symbol=symbol,
                instrument_type=InstrumentType.OPTION,
                broker="ibkr",
                quantity=1 + (idx % 3),
                avg_price=1.0,
                market_value=1.0,
                unrealized_pnl=0.0,
                underlying="AAPL" if idx % 2 == 0 else "MSFT",
                strike=200.0 + (idx % 10),
                expiration=date(2026, 6, 21),
                option_type="call" if idx % 2 == 0 else "put",
                delta=0.10 + ((idx % 5) * 0.01),
                gamma=0.01 + ((idx % 7) * 0.001),
                theta=1.0 + (idx % 4),
                vega=2.0 + (idx % 6),
                iv=0.20 + ((idx % 8) * 0.01),
            )
        )

    tools = PortfolioTools()
    summary_start = time.perf_counter()
    summary = tools.get_portfolio_summary(positions)
    gamma_by_dte = tools.get_gamma_risk_by_dte(positions)
    iv_analysis = tools.get_iv_analysis(positions, {"AAPL": 0.18, "MSFT": 0.16})
    analytics_elapsed = time.perf_counter() - summary_start
    total_elapsed = time.perf_counter() - start

    assert summary["position_count"] == 200
    assert "0-7" not in gamma_by_dte or isinstance(gamma_by_dte["0-7"], float)
    assert len(iv_analysis) == 200
    assert analytics_elapsed < 1.0
    assert total_elapsed < 3.0
