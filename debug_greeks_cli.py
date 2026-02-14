#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter
from pathlib import Path

import pandas as pd

from adapters.ibkr_adapter import IBKRAdapter
from agent_tools.portfolio_tools import PortfolioTools
from models.unified_position import InstrumentType


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CLI diagnostics for IBKR/Tasty option Greeks")
    parser.add_argument("--account", required=True, help="IBKR account id (e.g. U2052408)")
    parser.add_argument(
        "--disable-cache",
        action="store_true",
        help="Disable Tastytrade cache reads and force live fetch attempts per contract",
    )
    parser.add_argument(
        "--force-refresh-on-miss",
        action="store_true",
        default=False,
        help="When cache is enabled, force a live fetch if cache miss occurs",
    )
    parser.add_argument(
        "--output-prefix",
        default=None,
        help="Output prefix for JSON/CSV (default: .missing_greeks_<account>)",
    )
    parser.add_argument(
        "--max-options",
        type=int,
        default=0,
        help="Limit number of option positions processed (0 = all)",
    )
    return parser.parse_args()


async def run_debug(account: str) -> dict:
    adapter = IBKRAdapter()
    portfolio_tools = PortfolioTools()

    positions = await adapter.fetch_positions(account)

    max_options_raw = os.getenv("GREEKS_DEBUG_MAX_OPTIONS", "").strip()
    max_options = int(max_options_raw) if max_options_raw.isdigit() else None
    if max_options is not None and max_options > 0:
        option_positions_all = [p for p in positions if p.instrument_type == InstrumentType.OPTION]
        non_option_positions = [p for p in positions if p.instrument_type != InstrumentType.OPTION]
        positions = non_option_positions + option_positions_all[:max_options]

    positions = await adapter.fetch_greeks(positions)

    status = getattr(adapter, "last_greeks_status", {})
    missing_details = status.get("missing_greeks_details", []) or []

    summary = portfolio_tools.get_portfolio_summary(positions)

    option_positions = [p for p in positions if p.instrument_type == InstrumentType.OPTION]
    source_counts = Counter(getattr(p, "greeks_source", "none") for p in option_positions)
    reason_counts = Counter(item.get("reason", "") for item in missing_details)

    return {
        "positions_total": len(positions),
        "options_total": len(option_positions),
        "summary": {
            "spx_delta": float(summary.get("total_spx_delta", 0.0)),
            "theta_dollars": float(summary.get("total_theta", 0.0)),
            "vega_dollars": float(summary.get("total_vega", 0.0)),
            "gamma_dollars": float(summary.get("total_gamma", 0.0)),
        },
        "greeks_source_counts": dict(source_counts),
        "missing_reason_counts": dict(reason_counts),
        "status": status,
        "missing_details": missing_details,
    }


def main() -> None:
    args = parse_args()

    if args.disable_cache:
        os.environ["GREEKS_DISABLE_CACHE"] = "1"
    if args.force_refresh_on_miss:
        os.environ["GREEKS_FORCE_REFRESH_ON_MISS"] = "1"
    if args.max_options and args.max_options > 0:
        os.environ["GREEKS_DEBUG_MAX_OPTIONS"] = str(args.max_options)

    report = asyncio.run(run_debug(args.account))

    output_prefix = args.output_prefix or f".missing_greeks_{args.account}"
    output_json = Path(f"{output_prefix}.json")
    output_csv = Path(f"{output_prefix}.csv")

    missing_details = report.get("missing_details", [])
    output_json.write_text(json.dumps(missing_details, indent=2), encoding="utf-8")

    missing_df = pd.DataFrame(missing_details)
    missing_df.to_csv(output_csv, index=False)

    print("=== Greeks Debug Report ===")
    print(f"account: {args.account}")
    print(f"disable_cache: {bool(args.disable_cache)}")
    print(f"force_refresh_on_miss: {bool(args.force_refresh_on_miss)}")
    print(f"positions_total: {report['positions_total']}")
    print(f"options_total: {report['options_total']}")
    print(f"spx_delta: {report['summary']['spx_delta']:.2f}")
    print(f"theta_dollars: {report['summary']['theta_dollars']:.2f}")
    print(f"vega_dollars: {report['summary']['vega_dollars']:.2f}")
    print(f"gamma_dollars: {report['summary']['gamma_dollars']:.2f}")
    print(f"greeks_source_counts: {report['greeks_source_counts']}")
    print(f"missing_reason_counts: {report['missing_reason_counts']}")
    print(f"json: {output_json}")
    print(f"csv: {output_csv}")


if __name__ == "__main__":
    main()
