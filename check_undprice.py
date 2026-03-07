import asyncio
from ib_async import IB, Option

async def run():
    ib = IB()
    await ib.connectAsync('127.0.0.1', 4001, clientId=95)
    contracts = await ib.qualifyContractsAsync(Option('HPQ', '20260116', 35, 'C', 'SMART', 'USD'))
    if contracts:
        ticker = ib.reqMktData(contracts[0], '106')
        await asyncio.sleep(2)
        if ticker.modelGreeks:
            print('undPrice:', ticker.modelGreeks.undPrice)
            print('delta:', ticker.modelGreeks.delta)
    ib.disconnect()

asyncio.run(run())
