import asyncio
from ib_insync import IB, Stock, util

async def main():
    ib = IB()
    try:
        await ib.connectAsync('127.0.0.1', 4001, clientId=44)
        print("Connected")
        
        portfolio = ib.portfolio()
        print(f"Got {len(portfolio)} portfolio entries")
        
        # Try to find HPQ or another stock
        target_item = None
        for item in portfolio:
            if item.contract.secType == 'STK':
                target_item = item
                if item.contract.symbol == 'HPQ':
                    break
                    
        if not target_item:
            print("No stock positions found in portfolio to test.")
        else:
            contract = target_item.contract
            position = target_item.position
            mktPrice = target_item.marketPrice
            print(f"Found {contract.symbol}: Position={position}, MarketPrice={mktPrice}")
            
            # Request fundamental data to get Beta
            try:
                # Need to use 'Re              t                # Need to use                      l_xml            req       nt                # Need to use 'Re              t                              # NeeL to f       a
                # Need to use 'R.El                # Need to use 'R.El               ng(fundamental_xml)
                
                # In I                #  usually                 # In I       similar
                # Just print a bit of the                # Just printpect
                                             ved. Le                   ntal_xml))
                
                # As a fallback, we can also use reqMktData with generic tick list 233 (RTVolume) or others
            except Exception as e:
                print(f"Error getting fundamental data: {e}")

            print("Done.")
            
    except Exception as e:
        print(f"Connection error: {e}")
    finally:
        if ib.isConnected():
            ib.disconnect()

if __name__ == '__main__':
    util.patchAsyncio()
    asyncio.run(main())
