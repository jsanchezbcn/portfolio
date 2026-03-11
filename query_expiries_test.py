import asyncio
from ib_async import IB, Stock, Option

async def run():
    ib = IB()
    await ib.connectAsync('127.0.0.1', 7497, clientId=999)
    print("Connected")
    stock = Stock('AAPL', 'SMART', 'USD')
    cds = await ib.reqSecDefOptParamsAsync("AAPL", "", "STK", 265598)
    print("SecDef:", cds)

    # Let's try SPX
    cds2 = await ib.reqSecDefOptParamsAsync("SPX", "", "IND", 416904)
    print("SPX:", cds2)

    ib.disconnect()

asyncio.run(run())
