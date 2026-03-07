"""desktop/tests/test_main_window.py — UI automation tests for the main window.

Uses pytest-qt to drive the PySide6 UI without needing a real IB connection.
All IB interactions are mocked — only the UI behavior is verified.

Run:
    cd portfolioIBKR
    python -m pytest desktop/tests/test_main_window.py -v
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QToolBar

from desktop.engine.token_manager import TokenManager
from desktop.ui.main_window import MainWindow


# ── helpers ───────────────────────────────────────────────────────────────

def _make_window(qtbot, engine):
    """Create a MainWindow and register it for cleanup."""
    win = MainWindow(engine)
    qtbot.addWidget(win)
    return win


# ── layout tests ──────────────────────────────────────────────────────────


class TestMainWindowLayout:
    """Verify the main window structure and widget presence."""

    def test_window_creates(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        assert win.windowTitle() == "Portfolio Risk Manager — Desktop"

    def test_minimum_size(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        assert win.minimumWidth() == 1400
        assert win.minimumHeight() == 800

    def test_has_portfolio_tab(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        assert win._tabs.count() >= 2
        assert "Portfolio" in win._tabs.tabText(0)

    def test_has_chain_tab(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        assert "Options Chain" in win._tabs.tabText(1)

    def test_has_strategies_tab(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        labels = [win._tabs.tabText(i) for i in range(win._tabs.count())]
        assert any("Strategies" in label for label in labels)

    def test_portfolio_tab_is_default(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        assert win._tabs.currentIndex() == 0

    def test_has_order_entry_dock(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        assert win._order_entry is not None

    def test_toolbar_has_connect_button(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        actions = [a.text() for tb in win.findChildren(QToolBar) for a in tb.actions()]
        assert any("Connect" in a for a in actions)

    def test_toolbar_has_disconnect_button(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        actions = [a.text() for tb in win.findChildren(QToolBar) for a in tb.actions()]
        assert any("Disconnect" in a for a in actions)

    def test_toolbar_has_refresh_button(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        actions = [a.text() for tb in win.findChildren(QToolBar) for a in tb.actions()]
        assert any("Refresh" in a for a in actions)

    def test_has_copilot_account_picker(self, qtbot, mock_engine, tmp_path):
        prefs = tmp_path / "prefs.json"
        prefs.write_text('{"copilot_profile": "personal"}\n', encoding="utf-8")
        token_manager = TokenManager(preferences_path=prefs)
        win = MainWindow(mock_engine, token_manager=token_manager)
        qtbot.addWidget(win)

        assert win._account_picker.active_profile() == "personal"

    def test_statusbar_shows_ready(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        assert "Ready" in win._statusbar.currentMessage()

    def test_connection_indicator_starts_disconnected(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        assert "Disconnected" in win._lbl_conn.text()

    def test_disconnect_button_starts_disabled(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        assert not win._act_disconnect.isEnabled()

    def test_refresh_button_starts_disabled(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        assert not win._act_refresh.isEnabled()

    def test_connect_button_starts_enabled(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        assert win._act_connect.isEnabled()

    def test_compact_mode_action_exists(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)
        assert win._act_compact_mode.isCheckable()


# ── connection state tests ────────────────────────────────────────────────


class TestMainWindowConnection:
    """Test connection state transitions driven by Qt signals."""

    def test_connected_signal_updates_label(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)

        mock_engine._account_id = "U12345678"
        mock_engine._ib.isConnected.return_value = True
        mock_engine.connected.emit()

        assert "Connected" in win._lbl_conn.text()
        assert "U12345678" in win._lbl_conn.text()

    def test_connected_signal_toggles_actions(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)

        mock_engine._account_id = "U12345678"
        mock_engine._ib.isConnected.return_value = True
        mock_engine.connected.emit()

        assert not win._act_connect.isEnabled()
        assert win._act_disconnect.isEnabled()
        assert win._act_refresh.isEnabled()

    def test_connected_signal_starts_auto_refresh(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)

        mock_engine._account_id = "U12345678"
        mock_engine._ib.isConnected.return_value = True
        mock_engine.connected.emit()

        assert win._refresh_timer.isActive()

    def test_disconnected_signal_updates_label(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)

        # Connect first, then disconnect
        mock_engine._account_id = "U12345678"
        mock_engine._ib.isConnected.return_value = True
        mock_engine.connected.emit()

        mock_engine._ib.isConnected.return_value = False
        mock_engine.disconnected.emit()

        assert "Disconnected" in win._lbl_conn.text()

    def test_disconnected_signal_toggles_actions(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)

        mock_engine._account_id = "U12345678"
        mock_engine.connected.emit()
        mock_engine.disconnected.emit()

        assert win._act_connect.isEnabled()
        assert not win._act_disconnect.isEnabled()
        assert not win._act_refresh.isEnabled()

    def test_disconnected_signal_stops_auto_refresh(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)

        mock_engine._account_id = "U12345678"
        mock_engine.connected.emit()
        mock_engine.disconnected.emit()

        assert not win._refresh_timer.isActive()

    def test_error_signal_shows_in_statusbar(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)

        mock_engine.error_occurred.emit("Test error message")
        assert "Test error message" in win._statusbar.currentMessage()

    def test_reconnecting_state_updates_status_label(self, qtbot, mock_engine):
        win = _make_window(qtbot, mock_engine)

        mock_engine.connection_state.emit("reconnecting", "Reconnecting to IBKR")

        assert "Reconnecting" in win._lbl_conn.text()
        assert "Reconnecting to IBKR" in win._statusbar.currentMessage()


class TestMainWindowCopilotProfile:
    def test_profile_picker_updates_status_message(self, qtbot, mock_engine, monkeypatch):
        monkeypatch.setenv("GITHUB_COPILOT_TOKEN_WORK", "work-token")
        win = _make_window(qtbot, mock_engine)

        win._account_picker.set_active_profile("work")

        assert win._token_manager.active_profile == "work"
        assert "Work" in win._statusbar.currentMessage()


class TestMainWindowPortfolioActions:
    def test_portfolio_stock_action_prefills_order_entry(self, qtbot, mock_engine, sample_positions):
        win = _make_window(qtbot, mock_engine)

        win._on_portfolio_action_requested({
            "action": "SELL",
            "kind": "position",
            "description": "SPY",
            "legs": [sample_positions[1]],
        })

        assert win._order_entry._staged_legs
        assert win._order_entry._staged_legs[0]["symbol"] == "SPY"
        assert win._order_entry._staged_legs[0]["action"] == "SELL"

    def test_portfolio_roll_action_prefills_closing_legs(self, qtbot, mock_engine, sample_positions):
        win = _make_window(qtbot, mock_engine)

        win._on_portfolio_action_requested({
            "action": "ROLL",
            "kind": "trade_group",
            "description": "Iron Condor",
            "legs": [sample_positions[0], sample_positions[2]],
        })

        assert len(win._order_entry._staged_legs) == 2
        assert {leg["action"] for leg in win._order_entry._staged_legs} == {"BUY", "SELL"}
        assert "Roll requested" in win._order_entry._txt_rationale.toPlainText()


class TestMainWindowCompactMode:
    def test_compact_mode_from_preferences_hides_portfolio_columns(self, qtbot, mock_engine, tmp_path):
        prefs = tmp_path / "prefs.json"
        prefs.write_text('{"copilot_profile": "personal", "compact_mode": true}\n', encoding="utf-8")
        token_manager = TokenManager(preferences_path=prefs)

        win = MainWindow(mock_engine, token_manager=token_manager)
        qtbot.addWidget(win)

        assert win._act_compact_mode.isChecked()
        assert win._portfolio_tab._table.isColumnHidden(10)

    def test_compact_mode_toggle_persists_and_updates_tables(self, qtbot, mock_engine, tmp_path):
        prefs = tmp_path / "prefs.json"
        prefs.write_text('{"copilot_profile": "personal", "compact_mode": false}\n', encoding="utf-8")
        token_manager = TokenManager(preferences_path=prefs)

        win = MainWindow(mock_engine, token_manager=token_manager)
        qtbot.addWidget(win)

        win._act_compact_mode.trigger()

        assert win._portfolio_tab._table.isColumnHidden(10)
        assert win._orders_tab._table.isColumnHidden(9)
        assert '"compact_mode": true' in prefs.read_text(encoding="utf-8")
