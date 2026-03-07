import asyncio
from ib_async import IB, Stock

async def main():
    ib = IB()
    await ib.connectAsync('127.0.0.1', 7497, clientId=888)
    stock = Stock('HPQ', 'SMART', 'USD')
    try:
        xml = await ib.reqFundamentalDataAsync(stock, 'ReportSnapshot')
        print(xml[:200])
    except Exception as e:
        print("Error:", e)

    ib.disconnect()

asyncio.run(main())
