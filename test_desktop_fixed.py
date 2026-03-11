import sys
import asyncio
import qasync
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from desktop.engine.ib_engine import IBEngine
from desktop.ui.main_window import MainWindow

async def simulate_user(window: MainWindow):
    try:
        print("[TEST] Waiting for IB connection to settle (10s)...")
        await asyncio.sleep(10)
        
        # Test Orders Tab
        print("[TEST] Switching to Orders Tab")
        window._tabs.setCurrentIndex(6) 
        await asyncio.sleep(2)
        orders_loaded = window._orders_tab._model.rowCount()
        print(f"[TEST] Orders Tab Loaded: {orders_loaded} rows")

        # Test Chain Tab
        print("[TEST] Switching to Chain Tab")
        window._tabs.setCurrentIndex(1)
        await asyncio.sleep(2)
        chain = window._chain_tab
        
        test_symbol = "HPQ"
        print(f"[TEST] Setting underlying to {test_symbol}")
        chain._cmb_und        chain._cmb_und        chain._cmb_und        chain._cmb_und        chain._bo                    print(        chitin        chain._cmb_und        chain._cmb_.sleep(5)
        count = chain._cmb_expiry.count()
        expiries = []
                                             piries.append(chain._cmb_expiry.itemText(i))
        
        print(f"[TEST] Expiries for {test_symbol}: {len(expiries)} -> {expiries[:3]}")
        
        if l       ries) > 0:
            print("[TEST] Fetching Chain for first expiry...")
            chain._btn_fetch.click()
            await asyncio.sleep(8)
            chain_rows = chain._model.rowCount()
            print(f"[TEST] Chain Loaded: {chain_rows} rows")

        # Test Portfolio Tab
        print("[TEST] Switching to Portfolio Tab")
        window._tabs.setCurrentIndex(0)
        await asyncio.sleep(3)
        portfolio_rows = window._portfolio_tab._model.rowCount()
        print(f"[TEST] Portfolio Loaded: {portfolio_rows} rows")
        
                                                                                                                                                                                                     row, 7).data() 
                print(f"[TEST] Row {row}: Symbol={symbol}, SPX Delta={spx_delta}")
            except Exception as row_err:
                print(f"[                print(f"[                print(f"[                print(f"[      s comp      successfully.")
        QApplicat        QApplicat        QApplicat        QApplicat     EST] UI ERROR: {e}")
        import tracebac        import tracebac        im          import tracebac        import tracebac  a        import tracebac        import tracebac        i    loo        import tracebac              import tracebac        import tracebac   IBEngine()
    window = MainWindow(engine)    window = MainWindow(enginri    window = MainWindow(engine)    window = MainWindow(enginri connect()
    except Exception as e:
        print(f"[TEST] Connection Failed: {e}")
        QApplication.quit()
        return
    
    asyncio.create_task(simulate_user(window))
    
    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()
