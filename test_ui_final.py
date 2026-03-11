import sys
import asyncio
import qasync
from PySide6.QtWidgets import QApplication
from desktop.engine.ib_engine import IBEngine
from desktop.ui.main_window import MainWindow

async def simulate_user(window):
    try:
        print("[TEST] Waiting for IB connection...")
        await asyncio.sleep(8)
        
        # Check Portfolio
        print("[TEST] Switching to Portfolio Tab")
        window._tabs.setCurrentIndex(0)
        await asyncio.sleep(2)
        p_rows = window._portfolio_tab._model.rowCount()
        print(f"[TEST] Portfolio rows: {p_rows}")
        
        # Check Orders
        print("[TEST] Switching to Orders Tab")
        window._tabs.setCurrentIndex(6)
        await asyncio.sleep(2)
        o_rows = window._orders_tab._model.rowCount()
        print(f"[TEST] Orders rows: {o_rows}")
        
        # Check Chain
        print("[TEST] Switching to Chain Tab")
        window._tabs.setCurrentIndex(1)
        ch        ch        ch        ch        ch        ch        ch        ch PQ")
        chain._on_underlying_changed("HPQ"        chain._on_underlying_changed("H  e_        chain._on_underlying_changed("HPQ"  in        chain._on_underlying_changed("HPQ    
                               UI       equ                               UI       equ       ept                            EST  UI ERRO                  Applicat                               UI       equ     tio                               UI    )
                              pp                              pp                      BEngine()
    window = MainWindow(engine)
    window.show()
    
    await engine.connect()
    asyncio.create_task(simulate_user(window))
    
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    qasync.run(main())
