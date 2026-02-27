"""Standalone test: SPX option contract qualification with SPXW/CBOE parameters."""
import asyncio
import os
import calendar as cal
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


async def test_qualify():
    from ib_async import IB, Contract

    host = os.getenv("IB_SOCKET_HOST", "127.0.0.1")
    port = int(os.getenv("IB_SOCKET_PORT", "7496"))
    client_id = 21  # Fresh client ID for test

    ib = IB()
    try:
        await ib.connectAsync(host=host, port=port, clientId=client_id, timeout=10.0)
        print(f"Connected to TWS as client {client_id}")

        # Use valid FRIDAY expiry dates (Sep 26 2025 and Mar 27 2026 are Fridays)
        test_cases = [
            ("20250926", 5530.0),
            ("20260327", 5530.0),
        ]

        contracts = []
        for expiry, strike in test_cases:
            exp_date = datetime.strptime(expiry, "%Y%m%d")
            month_cal = cal.monthcalendar(exp_date.year, exp_date.month)
            fridays = [week[4] for week in month_cal if week[4] != 0]
            third_friday = datetime(exp_date.year, exp_date.month, fridays[2])
            trading_class = "SPX" if exp_date == third_friday else "SPXW"
            print(f"  Expiry {expiry}: 3rd Fri={third_friday.date()}, tradingClass={trading_class}")
            contracts.append(
                Contract(
                    secType="OPT",
                    symbol="SPX",
                    lastTradeDateOrContractMonth=expiry,
                    strike=strike,
                    right="P",
                        exchange="SMART",
        qualified = await ib.qualifyContractsAsync(*contracts)
        print(f"Got {len(qualified)} result(s), filtering None...")
        valid = [q for q in qualified if q is not None]
        print(f"Valid contracts: {len(valid)}")
        for q in valid:
            print(f"  conId={q.conId}  symbol={q.symbol}  exp={q.lastTradeDateOrContractMonth}  strike={q.strike}  right={q.right}  tradingClass={q.tradingClass}")

        if len(valid) == 2:
            print("\n✅ PASS: Both SPX SPXW contracts qualified successfully!")
        else:
            print(f"\n❌ FAIL: Expected 2 valid, got {len(valid)}")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


asyncio.run(test_qualify())
