#!/usr/bin/env python3
"""Diagnostic v4: try reqContractDetails approach for FOP expirations."""

import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from ib_async import IB, Future, Stock, FuturesOption
import logging

logging.getLogger("ib_async").setLevel(logging.CRITICAL)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


async def main():
    ib = IB()
    print("Connecting...", flush=True)
    await ib.connectAsync("127.0.0.1", 7496, clientId=44, timeout=30)
    print(f"Connected: {ib.managedAccounts()}", flush=True)

    # Qualify ES front-month
    print("\nQualifying ES...", flush=True)
    es = Future(symbol="ES", exchange="CME", currency="USD")
    details = await ib.reqContractDetailsAsync(es)
    details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
    front = details[0].contract
    print(f"ES front: conId={front.conId}, local={front.localSymbol}", flush=True)

    # === SPY control: reqSecDefOptParams ===
    print("\n--- SPY reqSecDefOptParams (control) ---", flush=True)
    spy = Stock("SPY", "SMART", "USD")
    spy_q = await ib.qualifyContractsAsync(spy)
    try:
        result = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync("SPY", "", "STK", spy_q[0].conId),
            timeout=10,
        )
        print(f"  SPY: {len(result)} chains, first has {len(result[0].expirations)} expirations", flush=True)
    except asyncio.TimeoutError:
        print("  SPY: TIMEOUT!", flush=True)

    # === ES: try reqContractDetailsAsync for FuturesOption ===
    print("\n--- ES reqContractDetailsAsync(FuturesOption) ---", flush=True)
    print("  Requesting all ES FOP contracts (may take a few seconds)...", flush=True)
    try:
        fop = FuturesOption(symbol="ES", exchange="CME", currency="USD")
        fop_details = await asyncio.wait_for(
            ib.reqContractDetailsAsync(fop),
            timeout=30,
        )
        if fop_details:
            expirations = sorted(set(
                d.contract.lastTradeDateOrContractMonth
                for d in fop_details
                if d.contract.lastTradeDateOrContractMonth
            ))
            strikes = sorted(set(d.contract.strike for d in fop_details if d.contract.strike))
            rights = set(d.contract.right for d in fop_details)
            trading_classes = set(d.contract.tradingClass for d in fop_details)
            print(f"  Found {len(fop_details)} contracts", flush=True)
            print(f"  {len(expirations)} unique expirations, {len(strikes)} strikes", flush=True)
            print(f"  Rights: {rights}, Trading classes: {trading_classes}", flush=True)
            print(f"  First 10 expirations: {expirations[:10]}", flush=True)
            if strikes:
                print(f"  Strike range: {min(strikes)} - {max(strikes)}", flush=True)
        else:
            print("  No FOP contracts found", flush=True)
    except asyncio.TimeoutError:
        print("  TIMEOUT (30s)", flush=True)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}", flush=True)

    # === ES: try with specific front-month expiry ===
    print("\n--- ES reqContractDetailsAsync(FOP, front-month) ---", flush=True)
    try:
        fop = FuturesOption(
            symbol="ES", exchange="CME", currency="USD",
            lastTradeDateOrContractMonth=front.lastTradeDateOrContractMonth[:6]
        )
        fop_details = await asyncio.wait_for(
            ib.reqContractDetailsAsync(fop),
            timeout=15,
        )
        if fop_details:
            expirations = sorted(set(
                d.contract.lastTradeDateOrContractMonth for d in fop_details
                if d.contract.lastTradeDateOrContractMonth
            ))
            strikes = sorted(set(d.contract.strike for d in fop_details if d.contract.strike))
            print(f"  Found {len(fop_details)} contracts for {front.lastTradeDateOrContractMonth[:6]}", flush=True)
            print(f"  {len(expirations)} expirations, {len(strikes)} strikes", flush=True)
            print(f"  Expirations: {expirations[:10]}", flush=True)
        else:
            print("  No results", flush=True)
    except asyncio.TimeoutError:
        print("  TIMEOUT (15s)", flush=True)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}", flush=True)

    # === Now try reqSecDefOptParamsAsync for ES (likely to timeout) ===
    print("\n--- ES reqSecDefOptParamsAsync (last, will timeout) ---", flush=True)
    try:
        result = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync("ES", "CME", "FUT", front.conId),
            timeout=10,
        )
        print(f"  ES: {len(result)} chains", flush=True)
        for ch in result[:3]:
            print(f"  ex={ch.exchange}, class={ch.tradingClass}, "
                  f"{len(ch.expirations)} exp, {len(ch.strikes)} strikes", flush=True)
    except asyncio.TimeoutError:
        print("  TIMEOUT (10s) — confirmed", flush=True)
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}", flush=True)

    print("\nDisconnecting...", flush=True)
    ib.disconnect()
    print("Done.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
