#!/usr/bin/env python3
from __future__ import annotations

import asyncio

from adapters.ibkr_adapter import IBKRAdapter
from agent_tools.market_data_tools import MarketDataTools
from agent_tools.portfolio_tools import PortfolioTools
from risk_engine.regime_detector import RegimeDetector

ACCOUNT = "U2052408"


async def main() -> None:
    adapter = IBKRAdapter()
    market = MarketDataTools()
    tools = PortfolioTools()
    regime_detector = RegimeDetector("config/risk_matrix.yaml")

    positions = await adapter.fetch_positions(ACCOUNT)
    non_options = [p for p in positions if p.instrument_type.name != "OPTION"]
    options_subset = [p for p in positions if p.instrument_type.name == "OPTION"][:12]
    positions = non_options + options_subset

    positions = await adapter.fetch_greeks(positions)

    vix_data = market.get_vix_data()
    macro_data = await market.get_macro_indicators()
    regime = regime_detector.detect_regime(
        vix=vix_data["vix"],
        term_structure=vix_data["term_structure"],
        recession_probability=macro_data.get("recession_probability") if isinstance(macro_data, dict) else None,
    )

    summary = tools.get_portfolio_summary(positions)
    gamma_buckets = tools.get_gamma_risk_by_dte(positions)

    iv_symbols = sorted({(p.underlying or "").upper() for p in positions if p.iv is not None and p.underlying})
    hv_by_symbol = market.get_historical_volatility(iv_symbols)
    iv_analysis = tools.get_iv_analysis(positions, hv_by_symbol)

    signal_counts: dict[str, int] = {}
    for row in iv_analysis:
        signal = str(row.get("signal") or "unknown")
        signal_counts[signal] = signal_counts.get(signal, 0) + 1

    print("=== Feature Demo (US6 + US7) ===")
    print("account:", ACCOUNT)
    print("positions_used:", len(positions), "(options subset:", len(options_subset), ")")
    print("regime:", regime.name)
    print("recession_probability:", macro_data.get("recession_probability"))
    print("macro_source:", macro_data.get("source"))
    print("SPX_delta:", round(summary["total_spx_delta"], 2))
    print("theta_dollars:", round(summary["total_theta"], 2))
    print("vega_dollars:", round(summary["total_vega"], 2))
    print("gamma_dollars:", round(summary["total_gamma"], 2))
    print("theta_vega_ratio:", round(summary["theta_vega_ratio"], 3))
    print("gamma_by_dte:", {k: round(float(v), 4) for k, v in gamma_buckets.items()})
    print("iv_hv_rows:", len(iv_analysis))
    print("iv_hv_signals:", signal_counts)
    for row in iv_analysis[:5]:
        print(
            "sample:",
            row["underlying"],
            "iv=",
            round(float(row["iv"]), 4),
            "hv=",
            round(float(row["hv"]), 4),
            "signal=",
            row["signal"],
        )


if __name__ == "__main__":
    asyncio.run(main())
