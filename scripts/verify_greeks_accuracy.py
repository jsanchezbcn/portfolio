"""scripts/verify_greeks_accuracy.py

Fetches live positions from IBKR for both accounts, runs our BetaWeighter pipeline,
and compares the resulting portfolio SPX delta / theta / vega against the reference
values captured in the Feb 19 2026 IBKR TWS screenshots.

Reference values (from screenshots):
  U2052408   : SPX Delta = -89.974 | Theta =  9359.595 | Vega = -748.477
  U19664833  : SPX Delta =   3.562 | Theta =    39.137 | Vega =  -38.739

Usage:
    python scripts/verify_greeks_accuracy.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Ensure project root on path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from ibkr_portfolio_client import IBKRClient
from adapters.ibkr_adapter import IBKRAdapter
from risk_engine.beta_weighter import BetaWeighter

# ── Reference values from TWS screenshots (Feb 19 2026, 1:08 PM) ──────────
REFERENCE = {
    "U2052408":  {"spx_delta": -89.974,  "theta":  9_359.595, "vega": -748.477},
    "U19664833": {"spx_delta":   3.562,  "theta":     39.137,  "vega":  -38.739},
}

TOLERANCE_DELTA_PCT = 15.0   # % — beta sources differ from TWS; 15% is acceptable
TOLERANCE_THETA_PCT =  5.0   # % — theta/vega are raw sums, should be close
TOLERANCE_VEGA_PCT  =  5.0   # %


def _pct_err(computed: float, reference: float) -> float:
    if reference == 0:
        return abs(computed)
    return abs((computed - reference) / reference) * 100


async def verify_account(client: IBKRClient, adapter: IBKRAdapter, account_id: str) -> dict:
    print(f"\n{'=' * 60}")
    print(f"Account: {account_id}")
    print(f"{'=' * 60}")

    # 1. Fetch positions via IBKR REST
    positions = await adapter.fetch_positions(account_id)
    print(f"  Positions fetched: {len(positions)}")

    # 2. Enrich Greeks (Tastytrade cache)
    positions = await adapter.fetch_greeks(positions)
    spx_price: float = adapter.last_greeks_status.get("spx_price") or 0.0
    print(f"  SPX price: {spx_price:.2f}")
    print(f"  Greeks cache misses: {adapter.last_greeks_status.get('cache_miss_count', '?')}")

    # 3. Portfolio aggregation
    total_spx_delta = sum(float(p.spx_delta) for p in positions)
    total_theta = sum(float(p.theta) for p in positions)
    total_vega = sum(float(p.vega) for p in positions)

    ref = REFERENCE.get(account_id, {})
    ref_delta = ref.get("spx_delta", None)
    ref_theta = ref.get("theta", None)
    ref_vega  = ref.get("vega", None)

    print(f"\n  {'Metric':<20} {'Computed':>12} {'IBKR Ref':>12} {'% Error':>10} {'Status':>8}")
    print(f"  {'-'*65}")

    results = {}
    for label, computed, reference, tol in [
        ("SPX Delta (β-wtd)", total_spx_delta, ref_delta, TOLERANCE_DELTA_PCT),
        ("Theta",             total_theta,     ref_theta, TOLERANCE_THETA_PCT),
        ("Vega",              total_vega,      ref_vega,  TOLERANCE_VEGA_PCT),
    ]:
        err = _pct_err(computed, reference) if reference is not None else None
        status = "✓ PASS" if (err is not None and err <= tol) else "✗ FAIL"
        err_str = f"{err:.1f}%" if err is not None else "N/A"
        print(f"  {label:<20} {computed:>12.3f} {reference if reference else 'N/A':>12} {err_str:>10} {status:>8}")
        results[label] = {"computed": computed, "reference": reference, "pct_error": err, "pass": err is None or err <= tol}

    # Per-position detail (for debugging)
    beta_unavailable_count = sum(1 for p in positions if getattr(p, "beta_unavailable", False))
    if beta_unavailable_count:
        print(f"\n  ⚠  {beta_unavailable_count} position(s) with beta_unavailable=True (defaulted to β=1.0):")
        for p in positions:
            if getattr(p, "beta_unavailable", False):
                print(f"     - {p.symbol:<30} underlying={p.underlying}")

    print(f"\n  Per-position SPX delta breakdown:")
    print(f"  {'Symbol':<35} {'Qty':>6} {'β-delta':>10} {'Theta':>10} {'Vega':>10} {'β?':>8}")
    print(f"  {'-'*80}")
    for p in sorted(positions, key=lambda x: abs(float(x.spx_delta)), reverse=True)[:15]:
        beta_flag = "⚠ unavail" if getattr(p, "beta_unavailable", False) else ""
        print(f"  {p.symbol:<35} {float(p.quantity):>6.1f} {float(p.spx_delta):>10.3f} {float(p.theta):>10.3f} {float(p.vega):>10.3f} {beta_flag:>8}")

    return results


async def main() -> None:
    client = IBKRClient()
    adapter = IBKRAdapter(client=client)
    
    all_pass = True
    for account_id in REFERENCE:
        try:
            results = await verify_account(client, adapter, account_id)
            account_pass = all(v["pass"] for v in results.values())
            all_pass = all_pass and account_pass
        except Exception as exc:
            print(f"\n  ERROR for {account_id}: {exc}")
            import traceback; traceback.print_exc()
            all_pass = False

    print(f"\n{'=' * 60}")
    print(f"OVERALL: {'✓ ALL PASS' if all_pass else '✗ SOME FAILURES (check tolerances above)'}")
    print(f"{'=' * 60}")
    print("\nNote: SPX delta tolerance is ±15% because IBKR uses proprietary beta estimates")
    print("      while we use Tastytrade/yfinance/config sources. Theta/Vega tolerance ±5%.")


if __name__ == "__main__":
    asyncio.run(main())
