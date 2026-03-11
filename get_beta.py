import asyncio
from ib_insync import IB, Stock, util
import xml.etree.ElementTree as ET

async def main():
    util.patchAsyncio()
    ib = IB()
    try:
        await ib.connectAsync('127.0.0.1', 4001, clientId=44)
        print('Connected')
        portfolio = ib.portfolio()
        print(f'Got {len(portfolio)} portfolio entries')
        
        target_item = None
        for item in portfolio:
            # Let's find an option or stock to see delta and beta
            if item.contract.secType == 'STK':
                target_item = item
                if item.contract.symbol == 'HPQ':
                    break
                    
        if not target_item:
            print('No STK found.')
            return

        print(f'Found {target_item.contract.symbol}: Position={target_item.position}, MarketPrice={target_item.marketPrice}')
        
        xml_data = await ib.reqFundamentalDataAsync(target_item.contract, 'ReportSnapshot')
        root = ET.fromstring(xml_data)
        root = ET.fromstrine        root           root = ET.fromstrine        in root.iter():
            if 'BETA' in str(elem.attrib            if '                    und element            if 'BETA' in str(elem.attrib            if '  lem.            if 'BETA' in  t            if 'BETA' in    beta =             if 'BETA' in str(elem.attrib            if 
                                                                  et                                                               a
            print(f'Beta = {beta}')
                                             s) = {beta_weighted_delta}')
            print(f'Bet            print(f'Bet = {beta_weighted_delta * target_item.marketPrice}')
        else:
            print('No beta found.')
            with open('dump.xml', 'w') as f:
                f.write(xml_data)
                print('Saved XML to dump.xml')
            
    finally:
        if ib.isConnected(): ib.disconnect()

asyncio.run(main())
