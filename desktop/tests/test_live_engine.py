"""Headless live integration test for IBEngine.

Run with:  .venv/bin/python desktop/tests/test_live_engine.py

Connects to IB TWS on localhost:7496, exercises each engine method,
and reports what worked / what failed.  Requires TWS running.
"""
import asyncio
import os
import sys
import time

# Ensure project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from dotenv import load_dotenv
load_dotenv()

from desktop.engine.ib_engine import IBEngine

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def main():
    host = os.getenv("IB_SOCKET_HOST", "127.0.0.1")
    port = int(os.getenv("IB_SOCKET_PORT", "4001"))
    db_dsn = (
        f"postgresql://{os.getenv('DB_USER','portfolio')}:{os.getenv('DB_PASS','yazooo')}"
        f"@{os.getenv('DB_HOST','localhost')}:{os.getenv('DB_PORT','5432')}"
        f"/{os.getenv('DB_NAME','portfolio_engine')}"
    )

    client_id = int(os.getenv("IB_TEST_CLIENT_ID", "140"))
    eng = IBEngine(host=host, port=port, client_id=client_id, db_dsn=db_dsn)
    results: dict[str, str] = {}

    # ── 1. Connect ────────────────────────────────────────────────────────
    log("Step 1: Connecting...")
    try:
        await eng.connect()
        results["connect"] = f"OK — account {eng.account_id}"
    except Exception as e:
        results["connect"] = f"FAIL: {e}"
        print_results(results)
        return

    # ── 2. Refresh Account ────────────────────────────────────────────────
    log("Step 2: Refresh Account...")
    try:
        summary = await eng.refresh_account()
        if summary:
            results["account"] = (
                f"OK — NLV=${summary.net_liquidation:,.0f} "
                f"BP=${summary.buying_power:,.0f} "
                f"UPnL=${summary.unrealized_pnl:+,.0f}"
            )
        else:
            results["account"] = "WARN — No summary returned"
    except Exception as e:
        results["account"] = f"FAIL: {e}"

    # ── 3. Refresh Positions + Greeks + Risk ──────────────────────────────
    log("Step 3: Refresh Positions + Greeks...")
    try:
        positions = await eng.refresh_positions()
        opts = [p for p in positions if p.sec_type in ("OPT", "FOP")]
        with_greeks = [p for p in opts if p.delta is not None]
        total_spx_delta = sum((p.spx_delta or 0.0) for p in positions)
        stock_spx_delta = sum((p.spx_delta or 0.0) for p in positions if p.sec_type == "STK")
        total_theta = sum((p.theta or 0.0) for p in positions)
        results["positions"] = (
            f"OK — {len(positions)} total, {len(opts)} options, "
            f"{len(with_greeks)}/{len(opts)} have Greeks, "
            f"SPXΔ={total_spx_delta:+.1f}, StockSPXΔ={stock_spx_delta:+.1f}, Θ={total_theta:+.1f}"
        )
        for p in with_greeks[:5]:
            print(f"  Greeks: {p.symbol:30s} D={p.delta:+8.2f} G={p.gamma:+8.4f} T={p.theta:+8.2f} V={p.vega:+8.2f}")
    except Exception as e:
        results["positions"] = f"FAIL: {e}"

    # ── 4. Open Orders ────────────────────────────────────────────────────
    log("Step 4: Open Orders...")
    try:
        orders = await eng.get_open_orders()
        results["orders"] = f"OK — {len(orders)} open orders"
        for o in orders[:3]:
            print(f"  Order {o.order_id}: {o.symbol} {o.action} {o.quantity:.0f} @ {o.order_type} — {o.status}")
    except Exception as e:
        results["orders"] = f"FAIL: {e}"

    # ── 5. Market Snapshot — ES front month (FUT) ─────────────────────────
    log("Step 5: Market Snapshot ES...")
    try:
        snap = await eng.get_market_snapshot("ES", "FUT", "CME")
        results["market_ES"] = f"OK — Last={snap.last}, Bid={snap.bid}, Ask={snap.ask}, Vol={snap.volume}"
    except Exception as e:
        results["market_ES"] = f"FAIL: {e}"

    # ── 6. Market Snapshot — SPY (STK) ────────────────────────────────────
    log("Step 6: Market Snapshot SPY...")
    try:
        snap = await eng.get_market_snapshot("SPY", "STK", "SMART")
        results["market_SPY"] = f"OK — Last={snap.last}, Bid={snap.bid}, Ask={snap.ask}, Vol={snap.volume}"
    except Exception as e:
        err_str = str(e)
        if "10089" in err_str or "subscription" in err_str.lower():
            results["market_SPY"] = "WARN — No API market data subscription for SPY (Error 10089)"
        else:
            results["market_SPY"] = f"FAIL: {e}"

    # ── 7. Available Expiries — ES ──────────────────────────────────────
    log("Step 7: Available Expiries ES...")
    try:
        expiries = await eng.get_available_expiries("ES", sec_type="FOP", exchange="CME")
        if expiries:
            results["expiries_ES"] = f"OK — {len(expiries)} expiries, next: {expiries[:3]}"
        else:
            results["expiries_ES"] = "WARN — 0 expiries (market may be closed)"
    except Exception as e:
        results["expiries_ES"] = f"FAIL: {type(e).__name__}: {e}"

    # ── 8. Options Chain — ES (FOP, 10 strikes) ──────────────────────────
    log("Step 8: Options Chain ES (10 strikes)...")
    try:
        chain = await asyncio.wait_for(
            eng.get_chain("ES", sec_type="FOP", exchange="CME", max_strikes=10),
            timeout=45,
        )
        results["chain_ES"] = f"OK — {len(chain)} contracts"
    except asyncio.TimeoutError:
        results["chain_ES"] = "WARN — Chain timed out (market may be closed)"
    except Exception as e:
        results["chain_ES"] = f"FAIL: {type(e).__name__}: {e}"
    log("Step 9: Disconnect...")
    try:
        await eng.disconnect()
        results["disconnect"] = "OK"
    except Exception as e:
        results["disconnect"] = f"FAIL: {e}"

    print_results(results)


def print_results(results: dict):
    print("\n" + "=" * 70)
    print("  LIVE ENGINE TEST RESULTS")
    print("=" * 70)
    for name, status in results.items():
        if status.startswith("OK"):
            icon = "pass"
        elif status.startswith("WARN"):
            icon = "warn"
        else:
            icon = "FAIL"
        print(f"  [{icon}] {name:20s}: {status}")
    print("=" * 70)
    passed = sum(1 for s in results.values() if s.startswith("OK"))
    warned = sum(1 for s in results.values() if s.startswith("WARN"))
    total = len(results)
    print(f"  {passed}/{total} passed, {warned} warnings")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
