#!/usr/bin/env python3
"""Diagnostic v2: test reqSecDefOptParams with debug output."""

import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from ib_async import IB, Future, Stock
import logging

# Only show our own logs
logging.getLogger("ib_async.wrapper").setLevel(logging.CRITICAL)
logging.getLogger("ib_async.ib").setLevel(logging.CRITICAL)
logging.getLogger("ib_async.client").setLevel(logging.CRITICAL)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("diag")


async def main():
    ib = IB()
    print("Connecting...", flush=True)
    await ib.connectAsync("127.0.0.1", 7496, clientId=42, timeout=30)
    print(f"Connected: {ib.managedAccounts()}", flush=True)

    # 1. Qualify ES front-month
    print("Qualifying ES...", flush=True)
    es = Future(symbol="ES", exchange="CME", currency="USD")
    details = await ib.reqContractDetailsAsync(es)
    if not details:
        print("FATAL: Cannot qualify ES!", flush=True)
        ib.disconnect()
        return

    details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
    front = details[0].contract
    print(f"ES front: conId={front.conId}, local={front.localSymbol}, "
          f"exchange={front.exchange}, secType={front.secType}", flush=True)

    # 2. Qualify SPY
    print("Qualifying SPY...", flush=True)
    spy = Stock(symbol="SPY", exchange="SMART", currency="USD")
    spy_q = await ib.qualifyContractsAsync(spy)
    spy_con = spy_q[0]
    print(f"SPY: conId={spy_con.conId}, exchange={spy_con.exchange}", flush=True)

    # 3. Test SPY first (should always work)
    print("\n--- TEST SPY (STK, '', conId) ---", flush=True)
    try:
        result = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync("SPY", "", "STK", spy_con.conId),
            timeout=15,
        )
        print(f"SPY result: {len(result)} chains", flush=True)
        for ch in result[:3]:
            n_exp = len(ch.expirations) if ch.expirations else 0
            n_str = len(ch.strikes) if ch.strikes else 0
            print(f"  exchange={ch.exchange}, class={ch.tradingClass}, "
                  f"{n_exp} expirations, {n_str} strikes", flush=True)
    except asyncio.TimeoutError:
        print("SPY: TIMEOUT", flush=True)
    except Exception as e:
        print(f"SPY: ERROR {e}", flush=True)

    # 4. Test ES with CME exchange
    print("\n--- TEST ES (FUT, 'CME', conId) ---", flush=True)
    try:
        result = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync("ES", "CME", "FUT", front.conId),
            timeout=15,
        )
        print(f"ES/CME result: {len(result)} chains", flush=True)
        for ch in result[:5]:
            n_exp = len(ch.expirations) if ch.expirations else 0
            n_str = len(ch.strikes) if ch.strikes else 0
            print(f"  exchange={ch.exchange}, class={ch.tradingClass}, "
                  f"{n_exp} expirations, {n_str} strikes", flush=True)
            if n_exp > 0:
                exps = sorted(ch.expirations)
                print(f"  first 5 expiries: {exps[:5]}", flush=True)
    except asyncio.TimeoutError:
        print("ES/CME: TIMEOUT", flush=True)
    except Exception as e:
        print(f"ES/CME: ERROR {e}", flush=True)

    # 5. Test ES with empty exchange
    print("\n--- TEST ES (FUT, '', conId) ---", flush=True)
    try:
        result = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync("ES", "", "FUT", front.conId),
            timeout=15,
        )
        print(f"ES/empty result: {len(result)} chains", flush=True)
        for ch in result[:5]:
            n_exp = len(ch.expirations) if ch.expirations else 0
            n_str = len(ch.strikes) if ch.strikes else 0
            print(f"  exchange={ch.exchange}, class={ch.tradingClass}, "
                  f"{n_exp} expirations, {n_str} strikes", flush=True)
    except asyncio.TimeoutError:
        print("ES/empty: TIMEOUT", flush=True)
    except Exception as e:
        print(f"ES/empty: ERROR {e}", flush=True)

    # 6. Test ES with GLOBEX exchange
    print("\n--- TEST ES (FUT, 'GLOBEX', conId) ---", flush=True)
    try:
        result = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync("ES", "GLOBEX", "FUT", front.conId),
            timeout=15,
        )
        print(f"ES/GLOBEX result: {len(result)} chains", flush=True)
        for ch in result[:5]:
            n_exp = len(ch.expirations) if ch.expirations else 0
            n_str = len(ch.strikes) if ch.strikes else 0
            print(f"  exchange={ch.exchange}, class={ch.tradingClass}, "
                  f"{n_exp} expirations, {n_str} strikes", flush=True)
    except asyncio.TimeoutError:
        print("ES/GLOBEX: TIMEOUT", flush=True)
    except Exception as e:
        print(f"ES/GLOBEX: ERROR {e}", flush=True)

    # 7. Test ES with conId=0
    print("\n--- TEST ES (FUT, 'CME', conId=0) ---", flush=True)
    try:
        result = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync("ES", "CME", "FUT", 0),
            timeout=15,
        )
        print(f"ES/conId0 result: {len(result)} chains", flush=True)
        for ch in result[:5]:
            n_exp = len(ch.expirations) if ch.expirations else 0
            n_str = len(ch.strikes) if ch.strikes else 0
            print(f"  exchange={ch.exchange}, class={ch.tradingClass}, "
                  f"{n_exp} expirations, {n_str} strikes", flush=True)
    except asyncio.TimeoutError:
        print("ES/conId0: TIMEOUT", flush=True)
    except Exception as e:
        print(f"ES/conId0: ERROR {e}", flush=True)

    print("\nDisconnecting...", flush=True)
    ib.disconnect()
    print("Done.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
