#!/usr/bin/env python3
from __future__ import annotations

from datetime import date

from agent_tools.portfolio_tools import PortfolioTools
from models.unified_position import InstrumentType, UnifiedPosition


def main() -> None:
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
            iv=0.40,
            gamma=1.5,
            theta=25,
            vega=80,
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
            iv=0.27,
            gamma=0.8,
            theta=12,
            vega=45,
        ),
        UnifiedPosition(
            symbol="NVDA240315C900",
            instrument_type=InstrumentType.OPTION,
            broker="ibkr",
            quantity=1,
            avg_price=1,
            market_value=1,
            unrealized_pnl=0,
            underlying="NVDA",
            strike=900,
            expiration=date(2026, 3, 15),
            option_type="call",
            iv=0.18,
            gamma=6.2,
            theta=-8,
            vega=20,
        ),
    ]

    historical_vol = {"AAPL": 0.22, "MSFT": 0.16, "NVDA": 0.24}

    tools = PortfolioTools()
    summary = tools.get_portfolio_summary(positions)
    gamma_by_dte = tools.get_gamma_risk_by_dte(positions)
    iv_analysis = tools.get_iv_analysis(positions, historical_vol)

    print("=== Deterministic Feature Demo (US7) ===")
    print("theta_vega_ratio:", round(summary["theta_vega_ratio"], 3), "zone=", summary["theta_vega_zone"])
    print("gamma_by_dte:", gamma_by_dte)
    print("iv_hv_rows:", len(iv_analysis))
    print("iv_hv_signals:", {row["underlying"]: row["signal"] for row in iv_analysis})
    print(
        "sell_edge_count:",
        sum(1 for row in iv_analysis if row["signal"] in {"strong_sell_edge", "moderate_sell_edge"}),
    )


if __name__ == "__main__":
    main()
