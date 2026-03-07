#!/usr/bin/env python3
"""Diagnostic: test reqSecDefOptParams with various parameter combos."""

import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from ib_async import IB, Future, Stock
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("diag")


async def main():
    ib = IB()
    await ib.connectAsync("127.0.0.1", 7496, clientId=42, timeout=30)
    log.info("Connected: %s", ib.managedAccounts())

    # 1. Qualify ES front-month futures contract
    es = Future(symbol="ES", exchange="CME", currency="USD")
    details = await ib.reqContractDetailsAsync(es)
    if not details:
        log.error("Cannot qualify ES futures!")
        ib.disconnect()
        return

    details.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)
    front = details[0].contract
    log.info("Qualified ES front-month: conId=%s, localSymbol=%s, exchange=%s, secType=%s, lastTradeDate=%s",
             front.conId, front.localSymbol, front.exchange, front.secType, front.lastTradeDateOrContractMonth)

    # 2. Try many parameter combos for reqSecDefOptParamsAsync
    combos = [
        # (label, symbol, futFopExchange, underlyingSecType, underlyingConId)
        ("A: sym=ES, fopEx='CME', secType='FUT', conId=front",
         "ES", "CME", "FUT", front.conId),

        ("B: sym=ES, fopEx='', secType='FUT', conId=front",
         "ES", "", "FUT", front.conId),

        ("C: sym=ES, fopEx='SMART', secType='FUT', conId=front",
         "ES", "SMART", "FUT", front.conId),

        ("D: sym=ES, fopEx='CME', secType='FUT', conId=0",
         "ES", "CME", "FUT", 0),

        ("E: sym=ES, fopEx='', secType='FUT', conId=0",
         "ES", "", "FUT", 0),

        ("F: sym=ES, fopEx='GLOBEX', secType='FUT', conId=front",
         "ES", "GLOBEX", "FUT", front.conId),

        ("G: sym=ES, fopEx='CME', secType='IND', conId=0",
         "ES", "CME", "IND", 0),
    ]

    # Also try SPY (equity options - should always work)
    spy = Stock(symbol="SPY", exchange="SMART", currency="USD")
    spy_q = await ib.qualifyContractsAsync(spy)
    spy_contract = spy_q[0]
    log.info("Qualified SPY: conId=%s, exchange=%s", spy_contract.conId, spy_contract.exchange)

    combos.append(
        ("H: sym=SPY, fopEx='', secType='STK', conId=spy",
         "SPY", "", "STK", spy_contract.conId)
    )
    combos.append(
        ("I: sym=SPY, fopEx='', secType='STK', conId=0",
         "SPY", "", "STK", 0)
    )

    for label, sym, fopEx, secType, conId in combos:
        try:
            result = await asyncio.wait_for(
                ib.reqSecDefOptParamsAsync(sym, fopEx, secType, conId),
                timeout=10,
            )
            if result:
                for chain in result:
                    n_exp = len(chain.expirations) if chain.expirations else 0
                    n_strikes = len(chain.strikes) if chain.strikes else 0
                    log.info("  [OK] %s → exchange=%s, tradingClass=%s, multiplier=%s, %d expirations, %d strikes",
                             label, chain.exchange, chain.tradingClass, chain.multiplier, n_exp, n_strikes)
                    if n_exp > 0:
                        exps_sorted = sorted(chain.expirations)
                        log.info("       First 5 expirations: %s", exps_sorted[:5])
            else:
                log.info("  [EMPTY] %s → returned empty list", label)
        except asyncio.TimeoutError:
            log.info("  [TIMEOUT] %s → timed out after 10s", label)
        except Exception as exc:
            log.info("  [ERROR] %s → %s", label, exc)

    ib.disconnect()
    log.info("Done.")

if __name__ == "__main__":
    asyncio.run(main())
