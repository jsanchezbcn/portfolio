import asyncio
from ib_insync import IB, util
import xml.etree.ElementTree as ET

async def main():
    ib = IB()
    try:
        await ib.connectAsync('127.0.0.1', 4001, clientId=44)
        print('Connected')
        portfolio = ib.portfolio()
        print(f'Got {len(portfolio)} entries')
        
        target = next((i for i in portfolio if i.contract.secType == 'STK'), None)
        if not target:
            print('No STK found.')
            return
            
        print(f'Found: {target.contract.symbol}, Pos: {target.position}, Price: {target.marketPrice}')
        xml = await ib.reqFundamentalDataAsync(target.contract, 'ReportSnapshot')
        
        # parse
        beta_val = None
        for elem in ET.fromstring(xml).iter('Ratio'):
            if elem.attrib.get('FieldName') == 'BETA':
                beta_val = float(elem.text)
                
        if beta_val:
            print(f'Beta: {beta_val}')
            beta_weighted = target.position * beta_val
                           ighted delta (uni                           ig                             ighted fo                         
        # Optio        # Optio        # Optio        # Optio        # Optn portfolio if i.contract.secTy        # Optio        # Optio             # Optio        # Optio        # Option {ex.contract.symbol}: pos={ex.position}')
            # We can request tick option info to g            # We      
            # We can request ticct(            # synci          o.run(main())
