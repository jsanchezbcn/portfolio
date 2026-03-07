import asyncio
from ib_async import IB, Stock

async def run():
    ib = IB()
    await ib.connectAsync('127.0.0.1', 4001, clientId=85)
    contracts = await ib.qualifyContractsAsync(Stock('HPQ', 'SMART', 'USD'))
    if contracts:
        ticker = ib.reqMktData(contracts[0])
        await asyncio.sleep(2)
        print('Price:', ticker.marketPrice())
        xml = await ib.reqFundamentalDataAsync(contracts[0], 'ReportSnapshot')
        print(xml[:200])
    ib.disconnect()

asyncio.run(run())
