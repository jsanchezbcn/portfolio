#!/usr/bin/env python3
"""
portfolio_cli.py — comprehensive CLI mirror of every dashboard feature.

Sub-commands
------------
  greeks    Full Greeks pipeline with per-position table + diagnostic JSON.
  positions List all positions (raw or normalized).
  summary   Portfolio totals (delta, theta, vega, gamma, SPX delta).
  spx-price Test every SPX price source in priority order.
  snapshot  Hit IBKR market-data snapshot for arbitrary conids.
  account   IBKR account summary (NLV, buying power, margin).
  raw       Dump raw IBKR positions payload (optionally filtered).
  verify    Compare computed Greeks against reference values from TWS.
  risk      Show active risk-limit violations.
  regime    Show current market regime + VIX / macro data.

Quick examples
--------------
  python scripts/portfolio_cli.py greeks --account U2052408
  python scripts/portfolio_cli.py greeks --account U2052408 --ibkr-only --json
  python scripts/portfolio_cli.py positions --account U2052408 --asset-class FOP
  python scripts/portfolio_cli.py summary --account U2052408
  python scripts/portfolio_cli.py spx-price --verbose
  python scripts/portfolio_cli.py snapshot --conids 649180695,853842073
  python scripts/portfolio_cli.py account --account U2052408
  python scripts/portfolio_cli.py raw --account U2052408 --asset-class FOP
  python scripts/portfolio_cli.py verify --account U2052408 \\
      --ref-delta -70 --ref-theta 9359 --ref-vega -748 --tolerance 15
  python scripts/portfolio_cli.py risk --account U2052408
  python scripts/portfolio_cli.py regime
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# ── add repo root to path so local imports work when run from any cwd ──────────
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from adapters.ibkr_adapter import IBKRAdapter
from agent_tools.portfolio_tools import PortfolioTools
from ibkr_portfolio_client import IBKRClient
from models.unified_position import InstrumentType, UnifiedPosition
from risk_engine.regime_detector import RegimeDetector

# ── Silence noisy third-party loggers ────────────────────────────────────────
import logging
logging.basicConfig(level=logging.ERROR)
for _noisy in ("urllib3", "tastytrade", "asyncio", "httpcore", "httpx"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

COL_SEP = "  "
LINE_FILL = "─"

def _hr(width: int = 90) -> str:
    return LINE_FILL * width

def _fmt(value: float | None, width: int = 9, prec: int = 3) -> str:
    if value is None:
        return "N/A".rjust(width)
    return f"{value:{width}.{prec}f}"

def _pct(value: float | None, width: int = 7, prec: int = 1) -> str:
    if value is None:
        return "N/A".rjust(width)
    return f"{value * 100:{width}.{prec}f}%"

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _print_kv(key: str, value, width: int = 28) -> None:
    print(f"  {key:<{width}}: {value}")


# ═══════════════════════════════════════════════════════════════════════════════
# Argument parsing
# ═══════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="portfolio_cli",
        description="CLI mirror of the Portfolio Risk dashboard.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # ── greeks ──────────────────────────────────────────────────────────────
    g = sub.add_parser("greeks", help="Full Greeks pipeline + diagnostic JSON")
    g.add_argument("--account", required=True)
    g.add_argument("--ibkr-only", action="store_true",
                   help="Skip Tastytrade fallback (IBKR snapshot only)")
    g.add_argument("--disable-cache", action="store_true",
                   help="Force live Tastytrade fetch (skip cached data)")
    g.add_argument("--json", dest="as_json", action="store_true",
                   help="Print full diagnostic JSON to stdout")
    g.add_argument("--output", default=None,
                   help="Path for diagnostic JSON file (default: .greeks_diag_<acct>.json)")
    g.add_argument("--max-options", type=int, default=0,
                   help="Limit option positions processed (0 = all)")

    # ── positions ────────────────────────────────────────────────────────────
    pos = sub.add_parser("positions", help="List normalized (or raw) positions")
    pos.add_argument("--account", required=True)
    pos.add_argument("--raw", action="store_true", help="Dump raw IBKR API JSON")
    pos.add_argument("--asset-class", default=None,
                     help="Filter by asset class: FOP, OPT, FUT, STK, BOND …")
    pos.add_argument("--json", dest="as_json", action="store_true")

    # ── summary ──────────────────────────────────────────────────────────────
    s = sub.add_parser("summary", help="Portfolio Greeks totals")
    s.add_argument("--account", required=True)
    s.add_argument("--ibkr-only", action="store_true")
    s.add_argument("--json", dest="as_json", action="store_true")

    # ── spx-price ────────────────────────────────────────────────────────────
    sp = sub.add_parser("spx-price", help="Test SPX price sources in priority order")
    sp.add_argument("--verbose", action="store_true")

    # ── snapshot ─────────────────────────────────────────────────────────────
    sn = sub.add_parser("snapshot", help="IBKR market-data snapshot for conids")
    sn.add_argument("--conids", required=True,
                    help="Comma-separated conids, e.g. 649180695,853842073")
    sn.add_argument("--fields", default=None,
                    help="Comma-separated field codes (default: 31,7308,7309,7310,7311,7633)")
    sn.add_argument("--json", dest="as_json", action="store_true")

    # ── account ──────────────────────────────────────────────────────────────
    ac = sub.add_parser("account", help="IBKR account summary (NLV, margin, etc.)")
    ac.add_argument("--account", required=True)
    ac.add_argument("--json", dest="as_json", action="store_true")

    # ── raw ──────────────────────────────────────────────────────────────────
    r = sub.add_parser("raw", help="Dump raw IBKR positions JSON")
    r.add_argument("--account", required=True)
    r.add_argument("--asset-class", default=None)
    r.add_argument("--output", default=None, help="Save JSON to this file")

    # ── verify ───────────────────────────────────────────────────────────────
    v = sub.add_parser("verify", help="Compare Greeks to TWS reference values")
    v.add_argument("--account", required=True)
    v.add_argument("--ref-delta", type=float, required=True,
                   help="Expected SPX delta from TWS screenshot")
    v.add_argument("--ref-theta", type=float, default=None)
    v.add_argument("--ref-vega",  type=float, default=None)
    v.add_argument("--tolerance", type=float, default=15.0,
                   help="Acceptable deviation %% (default 15%%)")
    v.add_argument("--ibkr-only", action="store_true")

    # ── risk ─────────────────────────────────────────────────────────────────
    rk = sub.add_parser("risk", help="Show risk-limit violations")
    rk.add_argument("--account", required=True)
    rk.add_argument("--ibkr-only", action="store_true")
    rk.add_argument("--json", dest="as_json", action="store_true")

    # ── regime ───────────────────────────────────────────────────────────────
    sub.add_parser("regime", help="Current market regime + VIX / macro data")

    # ── portal ───────────────────────────────────────────────────────────────
    po = sub.add_parser("portal", help="Manage IBKR Client Portal (status/restart/auth)")
    po.add_argument("--status", action="store_true", help="Show portal status only")
    po.add_argument("--restart", action="store_true", help="Restart portal Java service")
    po.add_argument("--open-auth", action="store_true", help="Open browser for re-auth after restart")
    po.add_argument("--wait", type=int, default=45, help="Seconds to wait for restart readiness")

    return p


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_adapter(ibkr_only: bool = False, disable_cache: bool = False) -> IBKRAdapter:
    adapter = IBKRAdapter()
    if ibkr_only:
        # Disable all Tastytrade fetching — IBKR snapshot only
        adapter.disable_tasty_cache = True
        adapter.force_refresh_on_miss = False
        os.environ["GREEKS_DISABLE_CACHE"] = "1"
        os.environ["GREEKS_FORCE_REFRESH_ON_MISS"] = "0"
    elif disable_cache:
        adapter.disable_tasty_cache = True
        os.environ["GREEKS_DISABLE_CACHE"] = "1"
    return adapter


async def _run_pipeline(
    account: str,
    ibkr_only: bool = False,
    disable_cache: bool = False,
    max_options: int = 0,
) -> tuple[list[UnifiedPosition], IBKRAdapter]:
    adapter = _make_adapter(ibkr_only, disable_cache)
    positions = await adapter.fetch_positions(account)

    if max_options > 0:
        opts = [p for p in positions if p.instrument_type == InstrumentType.OPTION]
        rest = [p for p in positions if p.instrument_type != InstrumentType.OPTION]
        positions = rest + opts[:max_options]

    positions = await adapter.fetch_greeks(positions)
    return positions, adapter


# ═══════════════════════════════════════════════════════════════════════════════
# Command: greeks
# ═══════════════════════════════════════════════════════════════════════════════

def _build_greeks_diag(
    account: str,
    positions: list[UnifiedPosition],
    adapter: IBKRAdapter,
) -> dict:
    """Build the full diagnostic JSON payload for the greeks command."""
    status = getattr(adapter, "last_greeks_status", {})
    pt = PortfolioTools()
    summary = pt.get_portfolio_summary(positions)

    opts = [p for p in positions if p.instrument_type == InstrumentType.OPTION]
    opts_active = [p for p in opts if abs(float(p.quantity or 0.0)) > 1e-9]
    opts_zero_qty = [p for p in opts if abs(float(p.quantity or 0.0)) <= 1e-9]
    futs = [p for p in positions if p.instrument_type == InstrumentType.FUTURE]
    eqty = [p for p in positions if p.instrument_type == InstrumentType.EQUITY]
    source_counts = Counter(getattr(p, "greeks_source", "none") for p in opts_active)

    per_position = []
    for p in positions:
        per_position.append({
            "symbol":       p.symbol,
            "broker_id":    getattr(p, "broker_id", ""),
            "asset_type":   p.instrument_type.value,
            "underlying":   p.underlying,
            "strike":       float(p.strike) if p.strike is not None else None,
            "expiration":   p.expiration.isoformat() if p.expiration else None,
            "option_type":  p.option_type,
            "dte":          p.days_to_expiration,
            "quantity":     float(p.quantity),
            "multiplier":   float(p.contract_multiplier),
            "mkt_value":    float(p.market_value),
            "upnl":         float(p.unrealized_pnl),
            "delta":        float(p.delta),
            "gamma":        float(p.gamma),
            "theta":        float(p.theta),
            "vega":         float(p.vega),
            "spx_delta":    float(p.spx_delta),
            "underlying_price": float(p.underlying_price) if p.underlying_price is not None else None,
            "iv":           float(p.iv) if p.iv is not None else None,
            "greeks_source": p.greeks_source,
            "beta_unavailable": bool(getattr(p, "beta_unavailable", False)),
        })

    missing = status.get("missing_greeks_details", []) or []

    return {
        "generated_at":    _now_utc(),
        "account":         account,
        "spx_price":       status.get("spx_price"),
        "spx_price_source": status.get("spx_price_source", "unknown"),
        "positions_total": len(positions),
        "options_total":   len(opts_active),
        "options_zero_qty_total": len(opts_zero_qty),
        "futures_total":   len(futs),
        "equity_total":    len(eqty),
        "portfolio_totals": {
            "spx_delta":    float(summary.get("total_spx_delta", 0.0)),
            "delta":        float(summary.get("total_delta", 0.0)),
            "gamma":        float(summary.get("total_gamma", 0.0)),
            "theta":        float(summary.get("total_theta", 0.0)),
            "vega":         float(summary.get("total_vega", 0.0)),
            "theta_vega_ratio": float(summary.get("theta_vega_ratio", 0.0)),
        },
        "greeks_source_breakdown": dict(source_counts),
        "ibkr_snapshot": {
            "candidates":  status.get("ibkr_snapshot_total", 0),
            "hits":        status.get("ibkr_snapshot_hits", 0),
            "no_data":     status.get("ibkr_snapshot_errors", []),
        },
        "tastytrade": {
            "prefetch_targets": status.get("prefetch_targets", {}),
            "prefetch_results": status.get("prefetch_results", {}),
            "cache_miss_count": status.get("cache_miss_count", 0),
            "disable_cache":    status.get("disable_tasty_cache", False),
            "force_refresh":    status.get("force_refresh_on_miss", False),
            "last_session_error": status.get("last_session_error"),
        },
        "missing_greeks": missing,
        "per_position": per_position,
    }


def _print_greeks_table(positions: list[UnifiedPosition]) -> None:
    opts = [p for p in positions if p.instrument_type == InstrumentType.OPTION]
    futs = [p for p in positions if p.instrument_type == InstrumentType.FUTURE]
    eqty = [p for p in positions if p.instrument_type == InstrumentType.EQUITY]

    # ── Options table ────────────────────────────────────────────────────────
    if opts:
        print(f"\n{'OPTION / FOP POSITIONS':^90}")
        print(_hr())
        hdr = (
            f"{'Symbol':<38}"
            f"{'Qty':>5}"
            f"{'K':>8}"
            f"{'DTE':>4}"
            f"{'Delta':>9}"
            f"{'Theta':>9}"
            f"{'Vega':>9}"
            f"{'SPXδ':>9}"
            f"{'Source':<14}"
        )
        print(hdr)
        print(_hr())
        for p in opts:
            print(
                f"{p.symbol[:38]:<38}"
                f"{p.quantity:>5.0f}"
                f"{(p.strike or 0):>8.0f}"
                f"{(p.days_to_expiration or 9999):>4d}"
                f"{_fmt(p.delta, 9, 3)}"
                f"{_fmt(p.theta, 9, 2)}"
                f"{_fmt(p.vega, 9, 2)}"
                f"{_fmt(p.spx_delta, 9, 2)}"
                f"  {p.greeks_source:<14}"
            )

    # ── Futures table ────────────────────────────────────────────────────────
    if futs:
        print(f"\n{'FUTURES POSITIONS':^90}")
        print(_hr())
        print(f"{'Symbol':<38}{'Qty':>6}{'MktVal':>12}{'Delta':>9}{'SPXδ':>9}")
        print(_hr())
        for p in futs:
            print(
                f"{p.symbol[:38]:<38}"
                f"{p.quantity:>6.0f}"
                f"{_fmt(p.market_value, 12, 0)}"
                f"{_fmt(p.delta, 9, 2)}"
                f"{_fmt(p.spx_delta, 9, 2)}"
            )

    # ── Equity / other ───────────────────────────────────────────────────────
    if eqty:
        print(f"\n{'EQUITY / OTHER POSITIONS':^90}")
        print(_hr())
        print(f"{'Symbol':<30}{'Qty':>8}{'MktVal':>12}{'SPXδ':>9}")
        print(_hr())
        for p in eqty:
            print(
                f"{p.symbol[:30]:<30}"
                f"{p.quantity:>8.0f}"
                f"{_fmt(p.market_value, 12, 0)}"
                f"{_fmt(p.spx_delta, 9, 2)}"
            )


def cmd_greeks(args: argparse.Namespace) -> None:
    print(f"[{_now_utc()}]  greeks —  account={args.account}")
    positions, adapter = asyncio.run(
        _run_pipeline(args.account, args.ibkr_only, args.disable_cache, args.max_options)
    )

    diag = _build_greeks_diag(args.account, positions, adapter)
    status = getattr(adapter, "last_greeks_status", {})

    # ── Print tables ──────────────────────────────────────────────────────────
    if not args.as_json:
        _print_greeks_table(positions)

        print(f"\n{'PORTFOLIO TOTALS':^90}")
        print(_hr())
        t = diag["portfolio_totals"]
        _print_kv("SPX delta (β-weighted)", f"{t['spx_delta']:+.2f}")
        _print_kv("Delta",                  f"{t['delta']:+.2f}")
        _print_kv("Theta  ($/day)",         f"{t['theta']:+.2f}")
        _print_kv("Vega",                   f"{t['vega']:+.2f}")
        _print_kv("Gamma",                  f"{t['gamma']:+.4f}")
        _print_kv("Theta / Vega",           f"{t['theta_vega_ratio']:.4f}")

        print(f"\n{'GREEKS DIAGNOSTICS':^90}")
        print(_hr())
        spx_p = diag.get("spx_price") or 0.0
        _print_kv("SPX price",              f"{spx_p:.2f}  (source: {diag['spx_price_source']})")
        _print_kv("Positions total",        diag["positions_total"])
        _print_kv("Options total",          diag["options_total"])
        _print_kv("Options zero-qty",       diag.get("options_zero_qty_total", 0))
        _print_kv("Greeks source breakdown",diag["greeks_source_breakdown"])
        sn = diag["ibkr_snapshot"]
        _print_kv("IBKR snapshot",
                  f"sent={sn['candidates']}  hits={sn['hits']}  no_data={len(sn['no_data'])}")
        tt = diag["tastytrade"]
        _print_kv("Tastytrade cache misses", tt["cache_miss_count"])
        if tt["last_session_error"]:
            _print_kv("Tastytrade auth error", tt["last_session_error"])

        if diag["missing_greeks"]:
            print(f"\n  {len(diag['missing_greeks'])} option(s) still missing Greeks:")
            for m in diag["missing_greeks"][:15]:
                sym = (m.get("symbol") or "")[:40]
                print(f"    {sym:<40}  reason={m.get('reason','?')}"
                      f"  und={m.get('underlying','')}  exp={m.get('expiry','')}  K={m.get('strike','')}")
            if len(diag["missing_greeks"]) > 15:
                print(f"    … and {len(diag['missing_greeks']) - 15} more")

    # ── Save diagnostic JSON ──────────────────────────────────────────────────
    out_path = Path(args.output or f".greeks_diag_{args.account}.json")
    out_path.write_text(json.dumps(diag, indent=2, default=str), encoding="utf-8")

    if args.as_json:
        print(json.dumps(diag, indent=2, default=str))
    else:
        print(f"\n  Diagnostic JSON: {out_path}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# Command: positions
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_positions(args: argparse.Namespace) -> None:
    client = IBKRClient()
    raw = client.get_positions(args.account)

    if args.asset_class:
        raw = [p for p in raw if (p.get("assetClass") or "").upper() == args.asset_class.upper()]

    if args.as_json or args.raw:
        out = json.dumps(raw, indent=2, default=str)
        print(out)
        return

    # Normalized display
    adapter = IBKRAdapter(client)
    positions = []
    for p in raw:
        try:
            positions.append(adapter._to_unified_position(p))
        except Exception:
            pass

    total_rows = len(positions)
    print(f"[{_now_utc()}]  positions — account={args.account}  total={total_rows}")
    print(_hr())
    print(f"{'Symbol':<40}{'Type':<8}{'Qty':>6}{'MktVal':>12}{'UPNL':>10}{'Source':<14}")
    print(_hr())
    for p in positions:
        print(
            f"{p.symbol[:40]:<40}"
            f"{p.instrument_type.value[:7]:<8}"
            f"{p.quantity:>6.0f}"
            f"{_fmt(p.market_value, 12, 0)}"
            f"{_fmt(p.unrealized_pnl, 10, 0)}"
            f"  {p.greeks_source:<14}"
        )
    print(_hr())


# ═══════════════════════════════════════════════════════════════════════════════
# Command: summary
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_summary(args: argparse.Namespace) -> None:
    positions, adapter = asyncio.run(_run_pipeline(args.account, getattr(args, "ibkr_only", False)))
    pt = PortfolioTools()
    summary = pt.get_portfolio_summary(positions)

    spx_price = (getattr(adapter, "last_greeks_status", {}) or {}).get("spx_price") or 0.0

    data = {
        "account":      args.account,
        "spx_price":    spx_price,
        "total_delta":  float(summary.get("total_delta", 0.0)),
        "total_gamma":  float(summary.get("total_gamma", 0.0)),
        "total_theta":  float(summary.get("total_theta", 0.0)),
        "total_vega":   float(summary.get("total_vega", 0.0)),
        "total_spx_delta": float(summary.get("total_spx_delta", 0.0)),
        "theta_vega_ratio": float(summary.get("theta_vega_ratio", 0.0)),
    }

    if getattr(args, "as_json", False):
        print(json.dumps(data, indent=2))
        return

    print(f"[{_now_utc()}]  summary — {args.account}")
    print(_hr(50))
    for k, v in data.items():
        if k == "account":
            continue
        _print_kv(k, f"{v:.4f}" if isinstance(v, float) else v)


# ═══════════════════════════════════════════════════════════════════════════════
# Command: spx-price
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_spx_price(args: argparse.Namespace) -> None:
    """Test each SPX price source in priority order and show results."""
    client = IBKRClient()
    verbose = getattr(args, "verbose", False)

    print(f"[{_now_utc()}]  Testing SPX price sources ...")
    print(_hr(60))
    results: list[dict] = []

    # Method 0: IBKR ES snapshot
    t0 = time.perf_counter()
    try:
        price = client.get_es_price_from_ibkr()
        elapsed = time.perf_counter() - t0
        ok = price is not None and 5000 < price < 9000
        results.append({"source": "IBKR ES snapshot", "price": price, "ok": ok, "ms": elapsed * 1000})
        status = f"✓  {price:.2f}" if ok else f"✗  {price}"
        print(f"  [0] IBKR ES snapshot        {status}  ({elapsed*1000:.0f} ms)")
    except Exception as e:
        elapsed = time.perf_counter() - t0
        results.append({"source": "IBKR ES snapshot", "price": None, "ok": False, "ms": elapsed * 1000, "error": str(e)})
        print(f"  [0] IBKR ES snapshot        ✗  {e}  ({elapsed*1000:.0f} ms)")

    # Method 1: Yahoo Finance ES=F
    t0 = time.perf_counter()
    try:
        import yfinance as yf
        hist = yf.Ticker("ES=F").history(period="1d", interval="1m")
        elapsed = time.perf_counter() - t0
        if not hist.empty:
            price = float(hist["Close"].iloc[-1])
            ok = 5000 < price < 9000
            results.append({"source": "Yahoo ES=F", "price": price, "ok": ok, "ms": elapsed * 1000})
            print(f"  [1] Yahoo Finance ES=F      {'✓' if ok else '✗'}  {price:.2f}  ({elapsed*1000:.0f} ms)")
        else:
            results.append({"source": "Yahoo ES=F", "price": None, "ok": False, "ms": elapsed * 1000, "error": "empty"})
            print(f"  [1] Yahoo Finance ES=F      ✗  empty  ({elapsed*1000:.0f} ms)")
    except Exception as e:
        elapsed = time.perf_counter() - t0
        results.append({"source": "Yahoo ES=F", "price": None, "ok": False, "ms": elapsed * 1000, "error": str(e)})
        print(f"  [1] Yahoo Finance ES=F      ✗  {e}  ({elapsed*1000:.0f} ms)")

    # Method 2: Yahoo Finance SPY×10
    t0 = time.perf_counter()
    try:
        hist = yf.Ticker("SPY").history(period="1d", interval="1m")
        elapsed = time.perf_counter() - t0
        if not hist.empty:
            spy = float(hist["Close"].iloc[-1])
            price = spy * 10
            ok = 400 < spy < 900
            results.append({"source": "Yahoo SPY×10", "price": price, "ok": ok, "ms": elapsed * 1000})
            print(f"  [2] Yahoo Finance SPY×10    {'✓' if ok else '✗'}  {price:.2f}  ({elapsed*1000:.0f} ms)")
        else:
            results.append({"source": "Yahoo SPY×10", "price": None, "ok": False, "ms": elapsed * 1000, "error": "empty"})
            print(f"  [2] Yahoo Finance SPY×10    ✗  empty  ({elapsed*1000:.0f} ms)")
    except Exception as e:
        elapsed = time.perf_counter() - t0
        results.append({"source": "Yahoo SPY×10", "price": None, "ok": False, "ms": elapsed * 1000, "error": str(e)})
        print(f"  [2] Yahoo Finance SPY×10    ✗  {e}  ({elapsed*1000:.0f} ms)")

    # Summary: which source would win
    winner = next((r for r in results if r["ok"]), None)
    print(_hr(60))
    if winner:
        print(f"  → Active source: {winner['source']}  price={winner['price']:.2f}")
    else:
        print("  ✗ All real-time sources failed — hardcoded estimate 6475.0 would be used")

    if verbose:
        print(json.dumps(results, indent=2))


# ═══════════════════════════════════════════════════════════════════════════════
# Command: snapshot
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_snapshot(args: argparse.Namespace) -> None:
    client = IBKRClient()
    conids = [c.strip() for c in args.conids.split(",") if c.strip()]
    fields = args.fields if getattr(args, "fields", None) else None

    print(f"[{_now_utc()}]  snapshot — conids={conids}  fields={fields or 'default'}")
    print("  Subscribing (call 1) …")
    data = client.get_market_snapshot(conids, fields=fields)
    print(f"  Data received for {len(data)} conid(s).")
    print(_hr())

    if getattr(args, "as_json", False):
        print(json.dumps(data, indent=2, default=str))
        return

    FIELD_LABELS = {
        "31": "last", "84": "bid", "86": "ask", "82": "change",
        "7308": "delta", "7309": "gamma", "7310": "theta",
        "7311": "vega", "7633": "IV",
    }
    for conid, item in data.items():
        print(f"\n  conid={conid}")
        for k, v in item.items():
            label = FIELD_LABELS.get(str(k), str(k))
            print(f"    {label:<12} ({k:<6}): {v}")


# ═══════════════════════════════════════════════════════════════════════════════
# Command: account
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_account(args: argparse.Namespace) -> None:
    client = IBKRClient()
    payload = client.get_account_summary(args.account)

    if getattr(args, "as_json", False):
        print(json.dumps(payload, indent=2, default=str))
        return

    def _v(key: str) -> str:
        raw = payload.get(key)
        if raw is None:
            return "N/A"
        if isinstance(raw, dict):
            return str(raw.get("amount", raw))
        return str(raw)

    print(f"[{_now_utc()}]  account — {args.account}")
    print(_hr(50))
    for k, label in [
        ("netliquidation",    "Net Liquidation"),
        ("buyingpower",       "Buying Power"),
        ("maintmarginreq",    "Maint Margin Req"),
        ("excessliquidity",   "Excess Liquidity"),
        ("totalcashvalue",    "Total Cash"),
        ("unrealizedpnl",     "Unrealized PnL"),
        ("realizedpnl",       "Realized PnL"),
    ]:
        _print_kv(label, _v(k))


# ═══════════════════════════════════════════════════════════════════════════════
# Command: raw
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_raw(args: argparse.Namespace) -> None:
    client = IBKRClient()
    raw = client.get_positions(args.account)
    ac_filter = (args.asset_class or "").upper()
    if ac_filter:
        raw = [p for p in raw if (p.get("assetClass") or "").upper() == ac_filter]

    output_json = json.dumps(raw, indent=2, default=str)
    out_path_str = getattr(args, "output", None)
    if out_path_str:
        Path(out_path_str).write_text(output_json, encoding="utf-8")
        print(f"Wrote {len(raw)} positions to {out_path_str}")
    else:
        print(output_json)


# ═══════════════════════════════════════════════════════════════════════════════
# Command: verify
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_verify(args: argparse.Namespace) -> None:
    print(f"[{_now_utc()}]  verify — account={args.account}  ref_delta={args.ref_delta}")
    positions, adapter = asyncio.run(
        _run_pipeline(args.account, getattr(args, "ibkr_only", False))
    )
    pt = PortfolioTools()
    summary = pt.get_portfolio_summary(positions)
    spx_price = (getattr(adapter, "last_greeks_status", {}) or {}).get("spx_price") or 0.0

    computed_delta = float(summary.get("total_spx_delta", 0.0))
    computed_theta = float(summary.get("total_theta", 0.0))
    computed_vega  = float(summary.get("total_vega", 0.0))
    tol = args.tolerance / 100.0

    def _check(name: str, computed: float, ref: float | None) -> bool:
        if ref is None:
            print(f"  {name:<16}: computed={computed:+.3f}  ref=N/A  (skipped)")
            return True
        if abs(ref) < 1e-9:
            ok = abs(computed) < 1.0
        else:
            ok = abs(computed - ref) / abs(ref) <= tol
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {name:<16}: computed={computed:+.3f}  ref={ref:+.3f}  "
              f"diff={computed - ref:+.3f}  ({(computed - ref) / abs(ref) * 100 if abs(ref) > 1e-9 else 0:+.1f}%)  {status}")
        return ok

    print(f"\n  SPX price: {spx_price:.2f}   tolerance: {args.tolerance:.1f}%")
    print(_hr(80))
    r1 = _check("SPX delta",   computed_delta, args.ref_delta)
    r2 = _check("Theta",       computed_theta, args.ref_theta)
    r3 = _check("Vega",        computed_vega,  args.ref_vega)
    print(_hr(80))
    overall = "✓ PASS" if (r1 and r2 and r3) else "✗ FAIL"
    print(f"  Overall: {overall}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# Command: risk
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_risk(args: argparse.Namespace) -> None:
    positions, _ = asyncio.run(_run_pipeline(args.account, getattr(args, "ibkr_only", False)))
    pt = PortfolioTools()
    summary = pt.get_portfolio_summary(positions)

    # Minimal regime for risk limits (reuse the regime_detector)
    regime_detector = RegimeDetector()
    try:
        from dashboard.app import get_cached_vix_data, get_cached_macro_data
        vix_data  = get_cached_vix_data()
        macro_data = get_cached_macro_data()
        regime = regime_detector.detect_regime(
            vix=vix_data.get("vix"),
            term_structure=vix_data.get("term_structure"),
            recession_probability=macro_data.get("recession_probability"),
        )
    except Exception:
        regime = regime_detector.detect_regime(vix=None, term_structure=None, recession_probability=None)

    violations = pt.check_risk_limits(summary, regime)

    print(f"[{_now_utc()}]  risk violations — account={args.account}  regime={regime.name}")
    print(_hr(70))
    if not violations:
        print("  ✓  No risk violations detected.")
    else:
        for v in violations:
            print(f"  ✗  {v}")

    if getattr(args, "as_json", False):
        print(json.dumps({"regime": regime.name, "violations": violations}, indent=2))


# ═══════════════════════════════════════════════════════════════════════════════
# Command: regime
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_regime(_args: argparse.Namespace) -> None:
    try:
        from dashboard.app import get_cached_vix_data, get_cached_macro_data
        vix_data   = get_cached_vix_data()
        macro_data = get_cached_macro_data()
    except Exception as e:
        print(f"Warning: could not load dashboard cache helpers: {e}")
        vix_data   = {}
        macro_data = {}

    regime_detector = RegimeDetector()
    regime = regime_detector.detect_regime(
        vix=vix_data.get("vix"),
        term_structure=vix_data.get("term_structure"),
        recession_probability=(macro_data.get("recession_probability") if macro_data else None),
    )

    print(f"[{_now_utc()}]  regime")
    print(_hr(50))
    _print_kv("Regime",                 regime.name)
    _print_kv("VIX",                    vix_data.get("vix", "N/A"))
    _print_kv("VIX term structure",     vix_data.get("term_structure", "N/A"))
    if macro_data:
        _print_kv("Recession prob",     macro_data.get("recession_probability", "N/A"))


# ═══════════════════════════════════════════════════════════════════════════════
# Command: portal
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_portal(args: argparse.Namespace) -> None:
    script = _REPO / "scripts" / "restart_ibkr_portal.sh"
    if not script.exists():
        print(f"restart script not found: {script}")
        sys.exit(1)

    # default action: restart
    do_status = bool(args.status)
    do_restart = bool(args.restart or not args.status)

    cmd = [str(script)]
    if do_status and not do_restart:
        cmd.append("--status")
    else:
        cmd.extend(["--wait", str(args.wait)])
        if args.open_auth:
            cmd.append("--open-auth")

    print(f"[{_now_utc()}]  portal — {'status' if do_status and not do_restart else 'restart'}")
    proc = subprocess.run(cmd, cwd=str(_REPO), text=True, capture_output=True)
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip())
    if proc.returncode != 0:
        sys.exit(proc.returncode)


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

_COMMANDS = {
    "greeks":    cmd_greeks,
    "positions": cmd_positions,
    "summary":   cmd_summary,
    "spx-price": cmd_spx_price,
    "snapshot":  cmd_snapshot,
    "account":   cmd_account,
    "raw":       cmd_raw,
    "verify":    cmd_verify,
    "risk":      cmd_risk,
    "regime":    cmd_regime,
    "portal":    cmd_portal,
}

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    fn = _COMMANDS.get(args.cmd)
    if fn is None:
        parser.print_help()
        sys.exit(1)
    fn(args)


if __name__ == "__main__":
    main()
