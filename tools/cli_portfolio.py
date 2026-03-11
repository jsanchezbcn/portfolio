#!/usr/bin/env python3
"""CLI tool for portfolio inspection and order management.

Usage:
    python -m tools.cli_portfolio summary
    python -m tools.cli_portfolio positions [--type OPTION|STOCK|FUTURE]
    python -m tools.cli_portfolio greeks
    python -m tools.cli_portfolio risk
    python -m tools.cli_portfolio signals
    python -m tools.cli_portfolio vix
    python -m tools.cli_portfolio chain ES 2025-07-18
    python -m tools.cli_portfolio order BUY ES PUT 5900 2025-07-18 1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from ibkr_portfolio_client import load_dotenv
load_dotenv(str(PROJECT_ROOT / ".env"))


def _run(coro):
    return asyncio.run(coro)


def _get_adapter():
    from adapters.ibkr_adapter import IBKRAdapter
    return IBKRAdapter()


def _get_account_id() -> str:
    accts = [a.strip() for a in os.getenv("IB_ACCOUNTS", "").split(",") if a.strip()]
    return accts[0] if accts else "unknown"


def _fmt(val, decimals=2):
    if val is None:
        return "N/A"
    try:
        return f"{float(val):.{decimals}f}"
    except (ValueError, TypeError):
        return str(val)


def cmd_summary(args):
    """Print portfolio summary (aggregated Greeks)."""
    from agent_tools.portfolio_tools import PortfolioTools
    adapter = _get_adapter()
    account_id = args.account or _get_account_id()

    positions = _run(adapter.fetch_positions(account_id))
    positions = _run(adapter.fetch_greeks(positions))

    pt = PortfolioTools()
    summary = pt.get_portfolio_summary(positions)

    print(f"\n{'='*50}")
    print(f"  PORTFOLIO SUMMARY — {account_id}")
    print(f"{'='*50}")
    print(f"  Positions:    {len(positions)}")
    print(f"  SPX Δ:        {_fmt(summary.get('total_spx_delta'))}")
    print(f"  Delta:        {_fmt(summary.get('total_delta'))}")
    print(f"  Theta:        {_fmt(summary.get('total_theta'))}")
    print(f"  Vega:         {_fmt(summary.get('total_vega'))}")
    print(f"  Gamma:        {_fmt(summary.get('total_gamma'), 4)}")
    print(f"  Θ/V Ratio:    {_fmt(summary.get('theta_vega_ratio'), 3)}")
    print(f"  Θ/V Zone:     {summary.get('theta_vega_zone', 'N/A')}")
    print(f"{'='*50}\n")


def cmd_positions(args):
    """Print all positions, optionally filtered by type."""
    adapter = _get_adapter()
    account_id = args.account or _get_account_id()

    positions = _run(adapter.fetch_positions(account_id))
    positions = _run(adapter.fetch_greeks(positions))

    if args.type:
        positions = [p for p in positions if p.instrument_type.name == args.type.upper()]

    now = datetime.now(timezone.utc)

    print(f"\n{'Symbol':<18} {'Type':<8} {'Qty':>6} {'Delta':>8} {'Theta':>8} {'Vega':>8} {'Gamma':>8} {'SPX Δ':>8} {'Source':<15} {'Age':>6}")
    print("-" * 110)
    for p in positions:
        age_min = ""
        if p.timestamp:
            ts = p.timestamp if p.timestamp.tzinfo else p.timestamp.replace(tzinfo=timezone.utc)
            age_min = f"{(now - ts).total_seconds() / 60:.0f}m"
        staleness = ""
        if age_min:
            mins = float(age_min.rstrip("m"))
            if mins > 30:
                staleness = " 🔴"
            elif mins > 5:
                staleness = " 🟡"
            else:
                staleness = " 🟢"

        exp_str = ""
        if p.expiration:
            exp_str = f" {p.option_type or ''}{p.strike or ''} {p.expiration}"

        print(
            f"{(p.symbol + exp_str):<18} {p.instrument_type.name:<8} "
            f"{float(p.quantity):>6.0f} "
            f"{float(p.delta):>8.2f} {float(p.theta):>8.2f} "
            f"{float(p.vega):>8.2f} {float(p.gamma):>8.4f} "
            f"{float(p.spx_delta):>8.2f} "
            f"{getattr(p, 'greeks_source', 'none'):<15} "
            f"{age_min}{staleness}"
        )
    print(f"\nTotal: {len(positions)} positions")


def cmd_greeks(args):
    """Print detailed per-position Greeks with staleness."""
    cmd_positions(args)  # Same output with all types


def cmd_risk(args):
    """Print risk compliance status."""
    from agent_tools.portfolio_tools import PortfolioTools
    from agent_tools.market_data_tools import MarketDataTools
    from risk_engine.regime_detector import RegimeDetector

    adapter = _get_adapter()
    account_id = args.account or _get_account_id()

    positions = _run(adapter.fetch_positions(account_id))
    positions = _run(adapter.fetch_greeks(positions))

    pt = PortfolioTools()
    mdt = MarketDataTools()
    rd = RegimeDetector(PROJECT_ROOT / "config/risk_matrix.yaml")

    summary = pt.get_portfolio_summary(positions)
    vix_data = mdt.get_vix_data()
    regime = rd.detect_regime(vix=vix_data["vix"], term_structure=vix_data["term_structure"])
    violations = pt.check_risk_limits(summary, regime)

    print(f"\n{'='*50}")
    print(f"  RISK CHECK — {regime.name}")
    print(f"{'='*50}")
    print(f"  VIX: {vix_data['vix']:.2f}  Term: {vix_data['term_structure']:.3f}")
    if violations:
        print(f"\n  ⚠️  {len(violations)} VIOLATION(S):")
        for v in violations:
            print(f"    - {v.get('metric', '?')}: {_fmt(v.get('current'))} vs limit {_fmt(v.get('limit'))}")
    else:
        print("\n  ✅ All limits satisfied.")

    # Gamma by DTE
    gamma_by_dte = pt.get_gamma_risk_by_dte(positions)
    print(f"\n  Gamma by DTE: {dict(gamma_by_dte)}")
    print(f"{'='*50}\n")


def cmd_signals(args):
    """Print active arbitrage signals."""
    import concurrent.futures

    async def _fetch():
        try:
            from database.db_manager import DBManager
            db = DBManager()
            await db.connect()
            return await db.get_active_signals(limit=50)
        except Exception:
            return []

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            signals = pool.submit(asyncio.run, _fetch()).result(timeout=15)
    except Exception:
        signals = []

    if not signals:
        print("No active arbitrage signals.")
        return

    print(f"\n{'Type':<25} {'Confidence':>10} {'Net Edge':>10} {'Detected':<20}")
    print("-" * 70)
    for s in signals:
        print(
            f"{str(s.get('signal_type', '')):<25} "
            f"{float(s.get('confidence', 0)):>10.0%} "
            f"${float(s.get('net_value', 0)):>9.2f} "
            f"{str(s.get('detected_at', ''))[:19]:<20}"
        )
    print(f"\nTotal: {len(signals)} signal(s)")


def cmd_vix(args):
    """Print VIX term structure."""
    from agent_tools.market_data_tools import MarketDataTools
    mdt = MarketDataTools()
    vix = mdt.get_vix_data()
    print(f"\n  VIX:    {vix['vix']:.2f}")
    print(f"  VIX3M:  {vix['vix3m']:.2f}")
    print(f"  Term:   {vix['term_structure']:.3f}")
    print(f"  Backwd: {vix['is_backwardation']}\n")


def cmd_chain(args):
    """Print options chain for an underlying+expiry."""
    adapter = _get_adapter()
    underlying = args.underlying
    expiry = args.expiry

    rows = _run(adapter.fetch_option_chain_matrix_tws(
        underlying=underlying,
        expiry=expiry,
        atm_price=0,
        strikes_each_side=int(args.strikes or 8),
    ))

    if not rows:
        print(f"No chain data for {underlying} {expiry}")
        return

    # Separate puts and calls
    puts = {r["strike"]: r for r in rows if r.get("right") == "P"}
    calls = {r["strike"]: r for r in rows if r.get("right") == "C"}
    strikes = sorted(set(puts.keys()) | set(calls.keys()), reverse=True)

    print(f"\n{'Put Bid':>8} {'Put Ask':>8} {'Put Δ':>8}  {'Strike':>8}  {'Call Δ':>8} {'Call Bid':>8} {'Call Ask':>8}")
    print("-" * 70)
    for s in strikes:
        p = puts.get(s, {})
        c = calls.get(s, {})
        print(
            f"{_fmt(p.get('bid')):>8} {_fmt(p.get('ask')):>8} {_fmt(p.get('delta')):>8}  "
            f"{s:>8.1f}  "
            f"{_fmt(c.get('delta')):>8} {_fmt(c.get('bid')):>8} {_fmt(c.get('ask')):>8}"
        )


def cmd_order(args):
    """Stage an order (prints confirmation, doesn't submit)."""
    print(f"\nOrder staged (not submitted):")
    print(f"  Action:  {args.action}")
    print(f"  Symbol:  {args.symbol}")
    print(f"  Right:   {args.right}")
    print(f"  Strike:  {args.strike}")
    print(f"  Expiry:  {args.expiry}")
    print(f"  Qty:     {args.qty}")
    print(f"\nUse the dashboard Order Builder to simulate and submit.\n")


def main():
    parser = argparse.ArgumentParser(description="Portfolio CLI — inspect positions, risk, and market data")
    parser.add_argument("--account", "-a", help="IBKR account ID (default: first from IB_ACCOUNTS)")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    subparsers.add_parser("summary", help="Portfolio summary (Greeks)")
    pos_p = subparsers.add_parser("positions", help="List positions")
    pos_p.add_argument("--type", "-t", choices=["OPTION", "STOCK", "FUTURE"], help="Filter by type")
    subparsers.add_parser("greeks", help="Detailed Greeks per position")
    subparsers.add_parser("risk", help="Risk compliance check")
    subparsers.add_parser("signals", help="Active arb signals")
    subparsers.add_parser("vix", help="VIX term structure")

    chain_p = subparsers.add_parser("chain", help="Options chain")
    chain_p.add_argument("underlying", help="Underlying (ES, MES, SPY)")
    chain_p.add_argument("expiry", help="Expiry (YYYY-MM-DD)")
    chain_p.add_argument("--strikes", "-s", default=8, type=int, help="Strikes each side of ATM")

    order_p = subparsers.add_parser("order", help="Stage order draft")
    order_p.add_argument("action", choices=["BUY", "SELL"])
    order_p.add_argument("symbol")
    order_p.add_argument("right", choices=["PUT", "CALL"])
    order_p.add_argument("strike", type=float)
    order_p.add_argument("expiry")
    order_p.add_argument("qty", type=int, default=1, nargs="?")

    args = parser.parse_args()

    commands = {
        "summary": cmd_summary,
        "positions": cmd_positions,
        "greeks": cmd_greeks,
        "risk": cmd_risk,
        "signals": cmd_signals,
        "vix": cmd_vix,
        "chain": cmd_chain,
        "order": cmd_order,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
