#!/usr/bin/env python3
"""Diagnostic v3: try both reqSecDefOptParams and reqContractDetails for FOP."""

import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from ib_async import IB, Future, Stock, FuturesOption, Option, Contract
import logging

logging.getLogger("ib_async.wrapper").setLevel(logging.CRITICAL)
logging.getLogger("ib_async.ib").setLevel(logging.CRITICAL)
logging.getLogger("ib_async.client").setLevel(logging.CRITICAL)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


async def main():
    ib = IB()
    print("Connecting...", flush=True)
    await ib.connectAsync("127.0.0.1", 7496, clientId=43, timeout=30)
    print(f"Connected: {ib.managedAccounts()}", flush=True)

    # Qualify ES front-month
    es = Future(symbol="ES", exchange="CME", currency="USD")
    details = await ib.reqContractDetailsAsync(es)
    details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
    front = details[0].contract
    print(f"ES front: conId={front.conId}, local={front.localSymbol}, "
          f"lastTradeDate={front.lastTradeDateOrContractMonth}", flush=True)

    # === TEST 1: reqSecDefOptParamsAsync for ES as FUT (expecting timeout) ===
    print("\n=== TEST 1: reqSecDefOptParamsAsync(ES, CME, FUT, conId) ===", flush=True)
    try:
        result = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync("ES", "CME", "FUT", front.conId),
            timeout=10,
        )
        print(f"  Result: {len(result)} chains", flush=True)
        for ch in result[:3]:
            print(f"  ex={ch.exchange}, class={ch.tradingClass}, "
                  f"{len(ch.expirations)} exp, {len(ch.strikes)} strikes", flush=True)
    except asyncio.TimeoutError:
        print("  TIMEOUT (10s)", flush=True)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

    # === TEST 2: reqSecDefOptParamsAsync with conId=0 ===
    print("\n=== TEST 2: reqSecDefOptParamsAsync(ES, CME, FUT, 0) ===", flush=True)
    try:
        result = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync("ES", "CME", "FUT", 0),
            timeout=10,
        )
        print(f"  Result: {len(result)} chains", flush=True)
        for ch in result[:3]:
            print(f"  ex={ch.exchange}, class={ch.tradingClass}, "
                  f"{len(ch.expirations)} exp, {len(ch.strikes)} strikes", flush=True)
    except asyncio.TimeoutError:
        print("  TIMEOUT (10s)", flush=True)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

    # === TEST 3: reqSecDefOptParamsAsync with '' exchange ===
    print("\n=== TEST 3: reqSecDefOptParamsAsync(ES, '', FUT, conId) ===", flush=True)
    try:
        result = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync("ES", "", "FUT", front.conId),
            timeout=10,
        )
        print(f"  Result: {len(result)} chains", flush=True)
        for ch in result[:3]:
            print(f"  ex={ch.exchange}, class={ch.tradingClass}, "
                  f"{len(ch.expirations)} exp, {len(ch.strikes)} strikes", flush=True)
    except asyncio.TimeoutError:
        print("  TIMEOUT (10s)", flush=True)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

    # === TEST 4: reqContractDetailsAsync for FuturesOption (alternative approach) ===
    print("\n=== TEST 4: reqContractDetailsAsync(FuturesOption ES) ===", flush=True)
    try:
        fop = FuturesOption(symbol="ES", exchange="CME", currency="USD")
        fop_details = await asyncio.wait_for(
            ib.reqContractDetailsAsync(fop),
            timeout=15,
        )
        if fop_details:
            # Extract unique expirations
            expirations = set()
            for d in fop_details:
                exp = d.contract.lastTradeDateOrContractMonth
                if exp:
                    expirations.add(exp)
            expirations = sorted(expirations)
            print(f"  Found {len(fop_details)} FOP contracts, {len(expirations)} unique expirations", flush=True)
            print(f"  First 10 expirations: {expirations[:10]}", flush=True)
            
            # Sample a few contracts
            for d in fop_details[:3]:
                c = d.contract
                print(f"  {c.localSymbol} {c.secType} {c.right} strike={c.strike} "
                      f"exp={c.lastTradeDateOrContractMonth} exchange={c.exchange}", flush=True)
        else:
            print("  No results", flush=True)
    except asyncio.TimeoutError:
        print("  TIMEOUT (15s)", flush=True)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

    # === TEST 5: reqContractDetailsAsync with specific FOP expiry ===
    print("\n=== TEST 5: reqContractDetailsAsync(FOP ES, front-month expiry) ===", flush=True)
    try:
        fop = FuturesOption(
            symbol="ES",
            exchange="CME",
            currency="USD",
            lastTradeDateOrContractMonth=front.lastTradeDateOrContractMonth[:6],  # YYYYMM
        )
        fop_details = await asyncio.wait_for(
            ib.reqContractDetailsAsync(fop),
            timeout=15,
        )
        if fop_details:
            expirations = sorted(set(
                d.contract.lastTradeDateOrContractMonth for d in fop_details if d.contract.lastTradeDateOrContractMonth
            ))
            strikes = sorted(set(d.contract.strike for d in fop_details if d.contract.strike))
            rights = set(d.contract.right for d in fop_details if d.contract.right)
            print(f"  Found {len(fop_details)} contracts, {len(expirations)} expirations, "
                  f"{len(strikes)} strikes, rights={rights}", flush=True)
            print(f"  Expirations: {expirations[:10]}", flush=True)
            print(f"  Strike range: {min(strikes) if strikes else 'N/A'} - {max(strikes) if strikes else 'N/A'}", flush=True)
        else:
            print("  No results", flush=True)
    except asyncio.TimeoutError:
        print("  TIMEOUT (15s)", flush=True)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

    # === TEST 6: SPY control (should work) ===
    print("\n=== TEST 6: reqSecDefOptParamsAsync(SPY, '', STK, conId) ===", flush=True)
    spy = Stock(symbol="SPY", exchange="SMART", currency="USD")
    spy_q = await ib.qualifyContractsAsync(spy)
    try:
        result = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync("SPY", "", "STK", spy_q[0].conId),
            timeout=10,
        )
        print(f"  Result: {len(result)} chains", flush=True)
        if result:
            ch = result[0]
            print(f"  ex={ch.exchange}, {len(ch.expirations)} exp, {len(ch.strikes)} strikes", flush=True)
    except asyncio.TimeoutError:
        print("  TIMEOUT", flush=True)

    print("\nDisconnecting...", flush=True)
    ib.disconnect()
    print("Done.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
