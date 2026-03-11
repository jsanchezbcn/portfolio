"""desktop/tests/conftest.py — pytest-qt + qasync fixtures for desktop UI tests.

Usage:
    cd portfolioIBKR
    python -m pytest desktop/tests/ -v
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
from PySide6.QtWidgets import QApplication

from desktop.engine.ib_engine import IBEngine, PositionRow, AccountSummary


# ── Qt / qasync fixtures ─────────────────────────────────────────────────


@pytest.fixture(scope="session")
def qapp():
    """Reuse a single QApplication across all tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def qtbot(qapp, qtbot):
    """Wrap the qtbot fixture to ensure our qapp is active."""
    return qtbot


# ── Mock Engine ───────────────────────────────────────────────────────────


@pytest.fixture
def mock_engine(qapp):
    """Create a mock IBEngine that doesn't connect to real IB/DB.

    All async methods are replaced with AsyncMock.
    Signals still work (they are real Qt signals on a real QObject).
    """
    engine = IBEngine(host="127.0.0.1", port=7496, client_id=99, db_dsn="postgresql://x:x@localhost/x")

    # Prevent real connections
    engine._ib = MagicMock()
    engine._ib.isConnected.return_value = False
    engine._ib.managedAccounts.return_value = ["U12345678"]
    engine._ib.positions.return_value = []

    # Mark DB as unavailable
    engine._db_ok = False

    return engine


@pytest.fixture
def sample_positions() -> list[PositionRow]:
    """A handful of test positions."""
    return [
        PositionRow(
            conid=1001, symbol="ES", sec_type="FOP", underlying="ES",
            strike=5500.0, right="C", expiry="20260320",
            quantity=-1.0, avg_cost=50.0,
            market_price=45.0, market_value=-4500.0,
            unrealized_pnl=500.0, realized_pnl=0.0,
            delta=-0.35, gamma=0.01, theta=-5.0, vega=12.0,
            iv=0.18, spx_delta=-17.5,
        ),
        PositionRow(
            conid=1002, symbol="SPY", sec_type="STK", underlying="",
            strike=None, right=None, expiry=None,
            quantity=100.0, avg_cost=550.0,
            market_price=555.0, market_value=55500.0,
            unrealized_pnl=500.0, realized_pnl=0.0,
            delta=100.0, gamma=0.0, theta=0.0, vega=0.0,
            iv=None, spx_delta=100.0,
        ),
        PositionRow(
            conid=1003, symbol="MES", sec_type="FOP", underlying="MES",
            strike=5600.0, right="P", expiry="20260320",
            quantity=2.0, avg_cost=30.0,
            market_price=35.0, market_value=700.0,
            unrealized_pnl=100.0, realized_pnl=0.0,
            delta=0.40, gamma=0.02, theta=-3.0, vega=8.0,
            iv=0.20, spx_delta=4.0,
        ),
    ]


@pytest.fixture
def sample_account_summary() -> AccountSummary:
    """Test account summary."""
    return AccountSummary(
        account_id="U12345678",
        net_liquidation=250000.0,
        total_cash=50000.0,
        buying_power=500000.0,
        init_margin=100000.0,
        maint_margin=80000.0,
        unrealized_pnl=1100.0,
        realized_pnl=0.0,
    )
