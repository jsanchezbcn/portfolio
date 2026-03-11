"""desktop/main.py — Entry point for the PySide6 desktop trading app.

Integrates PySide6's QApplication event loop with asyncio via qasync,
allowing ib_async and asyncpg to run on the same loop as the GUI.

Usage:
    cd portfolioIBKR
    python -m desktop.main                           # defaults
    python -m desktop.main --host 127.0.0.1 --port 4001 --client-id 30
    python -m desktop.main --db postgresql://user:pass@localhost/mydb
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

if __package__ in (None, ""):
    _project_root = Path(__file__).resolve().parents[1]
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))

# Load .env BEFORE anything reads os.environ
_dotenv_path = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(_dotenv_path)

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont

# qasync bridges PySide6's event loop with asyncio so that ib_async
# and asyncpg coroutines run natively without background threads.
import qasync

from desktop.engine.ib_engine import IBEngine
from desktop.engine.token_manager import TokenManager
from desktop.ui.main_window import MainWindow

logger = logging.getLogger("desktop")


def _build_db_dsn() -> str:
    """Build asyncpg DSN from individual env vars or PORTFOLIO_DB_URL."""
    explicit = os.environ.get("PORTFOLIO_DB_URL")
    if explicit:
        # asyncpg needs postgresql:// not postgresql+psycopg2://
        return explicit.replace("postgresql+psycopg2://", "postgresql://")

    # Fall back to individual env vars from .env
    host = os.environ.get("DB_HOST", "localhost").strip()
    port = os.environ.get("DB_PORT", "5432").strip()
    name = os.environ.get("DB_NAME", "portfolio_engine").strip()
    user = os.environ.get("DB_USER", "portfolio").strip()
    pw   = os.environ.get("DB_PASS", "yazooo").strip()
    return f"postgresql://{user}:{pw}@{host}:{port}/{name}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Desktop Portfolio Risk Manager")
    parser.add_argument(
        "--host",
        default=os.environ.get("IB_SOCKET_HOST", "127.0.0.1"),
        help="IB Gateway host (default from IB_SOCKET_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("IB_SOCKET_PORT", "4001")),
        help="IB Gateway port (default from IB_SOCKET_PORT or 4001)",
    )
    parser.add_argument("--client-id", type=int, default=30, help="Client ID (default: 30)")
    parser.add_argument(
        "--db",
        default=_build_db_dsn(),
        help="PostgreSQL DSN (default from DB_* env vars)",
    )
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet noisy loggers
    logging.getLogger("ib_async").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)


def apply_dark_theme(app: QApplication) -> None:
    """Apply a dark palette matching the Streamlit dashboard aesthetic."""
    from PySide6.QtGui import QPalette, QColor
    from PySide6.QtCore import Qt

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(30, 30, 30))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(35, 35, 35))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(50, 50, 50))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Text, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Button, QColor(45, 45, 45))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.ColorRole.Link, QColor(52, 152, 219))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(52, 152, 219))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))

    # Disabled state
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(100, 100, 100))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(100, 100, 100))

    app.setPalette(palette)

    # Global stylesheet tweaks
    app.setStyleSheet("""
        QToolTip {
            background: #2d2d2d;
            color: #ddd;
            border: 1px solid #555;
            padding: 4px;
        }
        QGroupBox {
            border: 1px solid #444;
            border-radius: 6px;
            margin-top: 12px;
            padding-top: 16px;
            font-weight: bold;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 6px;
        }
        QTabWidget::pane {
            border: 1px solid #444;
        }
        QTabBar::tab {
            background: #2d2d2d;
            color: #ccc;
            padding: 8px 16px;
            border: 1px solid #444;
            border-bottom: none;
            border-top-left-radius: 4px;
            border-top-right-radius: 4px;
        }
        QTabBar::tab:selected {
            background: #1e1e1e;
            color: white;
        }
        QPushButton {
            border: 1px solid #555;
            border-radius: 4px;
            padding: 6px 12px;
        }
        QPushButton:hover {
            background: #3d3d3d;
        }
        QPushButton:pressed {
            background: #2a2a2a;
        }
        QTableView {
            gridline-color: #333;
            selection-background-color: #3498db;
        }
        QHeaderView::section {
            background: #2d2d2d;
            color: #ccc;
            padding: 4px 8px;
            border: 1px solid #444;
            font-weight: bold;
        }
        QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
            background: #1e1e1e;
            color: #ddd;
            border: 1px solid #555;
            border-radius: 4px;
            padding: 4px 8px;
        }
        QTextEdit {
            background: #1e1e1e;
            color: #ddd;
            border: 1px solid #555;
            border-radius: 4px;
        }
        QDockWidget::title {
            background: #2d2d2d;
            padding: 6px;
        }
        QStatusBar {
            background: #1e1e1e;
            color: #aaa;
        }
    """)


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)

    logger.info("IB target:  %s:%d  clientId=%d", args.host, args.port, args.client_id)
    logger.info("DB target:  %s", args.db.split("@")[-1] if "@" in args.db else args.db)

    # Create QApplication BEFORE the event loop
    app = QApplication(sys.argv)
    app.setApplicationName("Portfolio Risk Manager")
    app.setFont(QFont("SF Pro Display", 12))
    apply_dark_theme(app)

    # Create the qasync event loop (bridges Qt ↔ asyncio)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    # Create engine + main window
    token_manager = TokenManager()
    logger.info("Active Copilot profile: %s", token_manager.active_profile)
    engine = IBEngine(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        db_dsn=args.db,
    )
    window = MainWindow(engine, token_manager=token_manager)
    window.show()

    logger.info("Desktop app started")

    # Run the event loop (blocks until app quits)
    with loop:
        loop.run_forever()

    # Cleanup
    logger.info("Application closed")


if __name__ == "__main__":
    main()
