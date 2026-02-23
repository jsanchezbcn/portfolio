#!/usr/bin/env python3
"""
scripts/debug_agent_tools.py — CLI debug harness for all agent tools.

Exercises every tool available to the LLM agents and prints a
pass / fail summary.  Each sub-command lets you also run a single tool.

Usage:
    python scripts/debug_agent_tools.py              # run all checks
    python scripts/debug_agent_tools.py market       # market data tools only
    python scripts/debug_agent_tools.py portfolio    # portfolio tools only
    python scripts/debug_agent_tools.py llm          # LLM chat tool
    python scripts/debug_agent_tools.py notify       # notification dispatcher
    python scripts/debug_agent_tools.py alert        # alert dispatcher
    python scripts/debug_agent_tools.py --list       # list all available toolsets
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
import time
import traceback
from pathlib import Path
from typing import Callable

# ── Path / env setup ───────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# ── ANSI colour helpers ────────────────────────────────────────────────────────
_USE_COLOUR = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text


def ok(msg: str)   -> str: return _c(f"✅  {msg}", "32")
def fail(msg: str) -> str: return _c(f"❌  {msg}", "31")
def warn(msg: str) -> str: return _c(f"⚠️  {msg}", "33")
def hdr(msg: str)  -> str: return _c(f"\n── {msg} ──", "34;1")


# ── Result tracking ────────────────────────────────────────────────────────────
_results: list[tuple[str, bool, str]] = []   # (name, passed, detail)


def record(name: str, passed: bool, detail: str = "") -> None:
    _results.append((name, passed, detail))
    sym = ok(name) if passed else fail(name)
    print(sym + (f"  [{detail[:120]}]" if detail else ""))


def _pretty(obj) -> str:
    try:
        return json.dumps(obj, indent=2, default=str)[:600]
    except Exception:
        return str(obj)[:600]


# ── Individual tool runners ────────────────────────────────────────────────────

def check_market_vix() -> None:
    from agent_tools.market_data_tools import MarketDataTools
    t = MarketDataTools()
    data = t.get_vix_data()
    passed = bool(data.get("vix"))
    record("market.get_vix_data", passed, f"vix={data.get('vix')}")


def check_market_spx() -> None:
    from agent_tools.market_data_tools import MarketDataTools
    t = MarketDataTools()
    data = t.get_spx_data()
    passed = bool(data.get("spx") or data.get("last") or data.get("close"))
    record("market.get_spx_data", passed, f"spx={data.get('spx') or data.get('last')}")


def check_market_hv() -> None:
    from agent_tools.market_data_tools import MarketDataTools
    t = MarketDataTools()
    data = t.get_historical_volatility(("SPY", "QQQ", "IWM"))
    passed = isinstance(data, dict) and len(data) > 0
    record("market.get_historical_volatility", passed, f"symbols={list(data.keys())}")


def check_market_macro() -> None:
    from agent_tools.market_data_tools import MarketDataTools
    t = MarketDataTools()
    data = asyncio.run(t.get_macro_indicators())
    passed = isinstance(data, dict) and len(data) > 0
    record("market.get_macro_indicators", passed, f"keys={list(data.keys())[:5]}")


def check_portfolio_summary() -> None:
    from agent_tools.portfolio_tools import PortfolioTools
    from models.unified_position import UnifiedPosition, InstrumentType
    from decimal import Decimal

    positions = [
        UnifiedPosition(
            broker="IBKR",
            symbol="SPY",
            instrument_type=InstrumentType.EQUITY,
            quantity=Decimal("100"),
            cost_basis=Decimal("420.0"),
            avg_price=Decimal("420.0"),
            current_price=Decimal("440.0"),
            market_value=Decimal("44000.0"),
            unrealized_pnl=Decimal("2000.0"),
            delta=Decimal("0.98"),
            gamma=Decimal("0.002"),
            theta=Decimal("-0.05"),
            vega=Decimal("0.01"),
            spx_delta=Decimal("95.3"),
        ),
    ]
    tools = PortfolioTools()
    summary = tools.get_portfolio_summary(positions)
    passed = "total_delta" in summary
    record("portfolio.get_portfolio_summary", passed, f"delta={summary.get('total_delta')}")


def check_portfolio_risk_limits() -> None:
    from agent_tools.portfolio_tools import PortfolioTools
    from risk_engine.regime_detector import RegimeDetector

    detector = RegimeDetector(PROJECT_ROOT / "config" / "risk_matrix.yaml")
    regime   = detector.detect_regime(vix=25.0, term_structure=1.05)

    tools = PortfolioTools()
    summary = {
        "total_delta": 600,
        "total_gamma": 5.0,
        "total_theta": -250.0,
        "total_vega": 800.0,
        "total_spx_delta": 450.0,
    }
    violations = tools.check_risk_limits(summary, regime)
    passed = isinstance(violations, list)
    record(
        "portfolio.check_risk_limits",
        passed,
        f"{len(violations)} violations: {[v.get('metric') for v in violations]}",
    )


def check_portfolio_gamma_by_dte() -> None:
    from agent_tools.portfolio_tools import PortfolioTools
    from models.unified_position import UnifiedPosition, InstrumentType
    from decimal import Decimal
    from datetime import date

    positions = [
        UnifiedPosition(
            broker="IBKR",
            symbol="SPY",
            instrument_type=InstrumentType.EQUITY,
            quantity=Decimal("100"),
            cost_basis=Decimal("420.0"),
            avg_price=Decimal("420.0"),
            current_price=Decimal("440.0"),
            market_value=Decimal("44000.0"),
            unrealized_pnl=Decimal("2000.0"),
            delta=Decimal("0.98"),
            gamma=Decimal("0.002"),
            theta=Decimal("-0.05"),
            vega=Decimal("0.01"),
            spx_delta=Decimal("95.3"),
            expiration=date.today().replace(year=date.today().year, month=6, day=20),
        ),
    ]
    tools = PortfolioTools()
    result = tools.get_gamma_risk_by_dte(positions)
    passed = isinstance(result, dict)
    record("portfolio.get_gamma_risk_by_dte", passed, f"buckets={list(result.keys())}")


def check_portfolio_iv_analysis() -> None:
    from agent_tools.portfolio_tools import PortfolioTools
    from models.unified_position import UnifiedPosition, InstrumentType
    from decimal import Decimal
    from datetime import date

    positions = [
        UnifiedPosition(
            broker="IBKR",
            symbol="SPY",
            instrument_type=InstrumentType.EQUITY,
            quantity=Decimal("100"),
            cost_basis=Decimal("420.0"),
            avg_price=Decimal("420.0"),
            current_price=Decimal("440.0"),
            market_value=Decimal("44000.0"),
            unrealized_pnl=Decimal("2000.0"),
            delta=Decimal("0.98"),
            gamma=Decimal("0.002"),
            theta=Decimal("-0.05"),
            vega=Decimal("0.01"),
            spx_delta=Decimal("95.3"),
        ),
    ]
    tools = PortfolioTools()
    result = tools.get_iv_analysis(positions, historical_volatility={"SPY": 0.18})
    passed = isinstance(result, list)
    record("portfolio.get_iv_analysis", passed, f"entries={len(result)}")


def check_llm_chat() -> None:
    from agents.llm_client import async_llm_chat

    print(warn("LLM test sends a real API request — expect 2-10 s …"))
    start = time.time()
    try:
        reply = asyncio.run(
            async_llm_chat(
                "Reply with exactly: OK",
                model="gpt-4o-mini",
                system="You are a test harness. Be concise.",
                timeout=30.0,
            )
        )
        elapsed = time.time() - start
        passed = bool(reply) and len(reply.strip()) > 0
        record("llm.async_llm_chat", passed, f"{elapsed:.1f}s  reply={reply.strip()[:80]}")
    except Exception as exc:
        record("llm.async_llm_chat", False, str(exc)[:120])


def check_alert_dispatcher() -> None:
    from agent_tools.alert_dispatcher import LogDispatcher

    dispatcher = LogDispatcher()
    violations = [{"rule": "max_delta", "value": 600, "limit": 500, "severity": "WARNING"}]
    try:
        dispatcher.dispatch("debug test alert", violations)
        record("alert.LogDispatcher.dispatch", True)
    except Exception as exc:
        record("alert.LogDispatcher.dispatch", False, str(exc)[:120])


def check_notification_dispatcher() -> None:
    from agent_tools.notification_dispatcher import NotificationDispatcher

    dispatcher = NotificationDispatcher()
    # Only test if Telegram/email env vars are set; otherwise mark as skipped
    has_telegram = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    if not has_telegram:
        print(warn("notify.send_alert  SKIPPED — TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"))
        _results.append(("notify.send_alert", True, "skipped (no creds)"))
        return

    try:
        asyncio.run(
            dispatcher.send_alert(
                title="Debug test",
                body="This is a debug test from debug_agent_tools.py",
                urgency="low",
            )
        )
        record("notify.send_alert", True, "Telegram message sent")
    except Exception as exc:
        record("notify.send_alert", False, str(exc)[:120])


# ── Tool-set registry ──────────────────────────────────────────────────────────

TOOL_SETS: dict[str, list[Callable]] = {
    "market": [
        check_market_vix,
        check_market_spx,
        check_market_hv,
        check_market_macro,
    ],
    "portfolio": [
        check_portfolio_summary,
        check_portfolio_risk_limits,
        check_portfolio_gamma_by_dte,
        check_portfolio_iv_analysis,
    ],
    "llm": [
        check_llm_chat,
    ],
    "alert": [
        check_alert_dispatcher,
    ],
    "notify": [
        check_notification_dispatcher,
    ],
}


# ── Runner ─────────────────────────────────────────────────────────────────────

def _run_set(name: str, checks: list[Callable]) -> None:
    print(hdr(name.upper()))
    for fn in checks:
        try:
            fn()
        except Exception as exc:
            tool_name = fn.__name__.replace("check_", "").replace("_", ".", 1)
            record(tool_name, False, f"EXCEPTION: {traceback.format_exc(limit=2)}")


def _print_summary() -> None:
    total   = len(_results)
    passed  = sum(1 for _, p, _ in _results if p)
    failed  = total - passed

    print(hdr("SUMMARY"))
    for name, p, detail in _results:
        sym = ok(name) if p else fail(name)
        print(f"  {sym}")

    bar = ok(f"{passed}/{total} passed") if failed == 0 else fail(f"{passed}/{total} passed, {failed} failed")
    print(f"\n{bar}\n")
    if failed:
        sys.exit(1)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]

    if "--list" in args or "-l" in args:
        print("Available tool groups:")
        for k, checks in TOOL_SETS.items():
            print(f"  {k:12}  ({len(checks)} checks)")
        print("  all         (run all groups)")
        return

    if args:
        requested = [a.lower() for a in args]
        if "all" in requested:
            groups = list(TOOL_SETS.items())
        else:
            groups = []
            for g in requested:
                if g in TOOL_SETS:
                    groups.append((g, TOOL_SETS[g]))
                else:
                    print(fail(f"Unknown group '{g}'. Use --list to see available groups."))
                    sys.exit(1)
    else:
        # Default: run all
        groups = list(TOOL_SETS.items())

    for name, checks in groups:
        _run_set(name, checks)

    _print_summary()


if __name__ == "__main__":
    main()
