import sys
import asyncio
import qasync
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt

from desktop.engine.ib_engine import IBEngine
from desktop.ui.main_window import MainWindow

async def simulate_user(window: MainWindow):
    try:
        print("[TEST] Waiting for IB connection to settle...")
        await asyncio.sleep(5)
        
        # Test Orders Tab
        print("[TEST] Switching to Orders Tab")
        window._tabs.setCurrentIndex(6) # adjust index if needed
        await asyncio.sleep(2)
        orders_loaded = window._orders_tab._model.rowCount()
        print(f"[TEST] Orders Tab Loaded: {orders_loaded} rows")

        # Test Chain Tab
        print("[TEST] Switching to Chain Tab")
        window._tabs.setCurrentIndex(1)
        await asyncio.sleep(1)
        chain = window._chain_tab
        chain._cmb_underlying.setCurrentText("HPQ")
        chain._on_underlying_changed("HPQ") # Trigger manu        chain._on_underlying_changed("HPQ print("[TEST        chainiries...")
        await asyncio.sleep(4)
        expiries         expiries         Text(i)         expiries         _expiry.count())]
        print(f"[TEST] Expiries fo        print(f"[TEST] Expiries fo        pr       
        print("[TEST] Fetching Chain..." 
                                                                        chain_rows = chain._model.rowCount()
                    T] Chai                    T] Chai               est SP                    T] Chai                    T] Chai         wi                    T] Cha        window._tabs.setCurrentIndex(0)
        await asyncio.slee        await asyncio.slee        await asyncio.slee        plicat        await asyncio Exception as e:
        print(f"Error in test: {e}")
        QApplication.quit()

async def main():
    app = QApplication.instance() or QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.s    asyncio.s    asyncio.s    asyncio.s    asyncio.s    asyncio.s    asyncio.s    asyncio.s    asyncio.s    asyncio.s    asyncioonnectin    asyncio.s    asynciongine.c    asyncio.s    asyncio.s    asyncio.s    asyncio.s    asyncio.s    asyncio.s    asyncio.sulate_user(window))

    # Start event loop
    await asyncio.Event().wait()  # Wait for    await asyncio.Event().wait()  # Wname__ == "__main__":
    qasync.run(main())
