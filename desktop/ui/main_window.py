"""desktop/ui/main_window.py — Main application window.

Layout:
  ┌────────────────────────────────────────────────────────┐
  │                     Menu / Toolbar                      │
  ├────────────────────────────────────────────────────────┤
  │  ┌─────────────────────┬──────────────────────────────┐│
  │  │                     │                              ││
  │  │   Tab Widget        │   Order Entry (right dock)   ││
  │  │   - Portfolio Tab   │                              ││
  │  │   - Chain Tab       │                              ││
  │  │   - Orders Tab      │                              ││
  │  │                     │                              ││
  │  └─────────────────────┴──────────────────────────────┘│
  ├────────────────────────────────────────────────────────┤
  │                     Status Bar                         │
  └────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QDockWidget, QStatusBar,
    QToolBar, QLabel, QMessageBox,
)
from PySide6.QtCore import Qt, Slot, QTimer
from PySide6.QtGui import QAction

from desktop.config.preferences import load_preferences, save_preferences
from desktop.engine.sound_engine import SoundEngine
from desktop.ui.portfolio_tab import PortfolioTab
from desktop.ui.order_entry import OrderEntryPanel
from desktop.ui.chain_tab import ChainTab
from desktop.ui.risk_tab import RiskTab
from desktop.ui.strategies_tab import StrategiesTab
from desktop.ui.orders_tab import OrdersTab
from desktop.ui.market_tab import MarketTab
from desktop.ui.journal_tab import JournalTab
from desktop.ui.ai_risk_tab import AIRiskTab
from desktop.ui.widgets.account_picker import AccountPicker
from desktop.workers.agent_runner import AgentRunner
from desktop.engine.token_manager import TokenManager

if TYPE_CHECKING:
    from desktop.engine.ib_engine import IBEngine


class MainWindow(QMainWindow):
    """Top-level window for the desktop trading application."""

    def __init__(self, engine: IBEngine, token_manager: TokenManager | None = None, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._token_manager = token_manager or TokenManager()
        self._preferences_path = getattr(self._token_manager, "_preferences_path", None)
        self._preferences = load_preferences(self._preferences_path)
        self._sound_engine = SoundEngine(enabled=bool(self._preferences.get("sound_enabled", True)))
        self.setWindowTitle("Portfolio Risk Manager — Desktop")
        self.setMinimumSize(1400, 800)
        self.resize(1600, 900)

        self._setup_ui()
        self._setup_toolbar()
        self._setup_statusbar()
        # Background agents must be created before _connect_signals() wires their signals
        self._agent_runner = AgentRunner(self._engine, parent=self)
        self._connect_signals()
        self._setup_auto_refresh()
        # Auto-connect as soon as the Qt event loop starts running
        QTimer.singleShot(200, self._on_connect)

    def _setup_ui(self) -> None:
        # ── Central: Tab widget ───────────────────────────────────────────
        self._tabs = QTabWidget()
        self._tabs.setTabPosition(QTabWidget.TabPosition.North)
        self._tabs.setDocumentMode(True)

        # Portfolio tab
        self._portfolio_tab = PortfolioTab(self._engine)
        self._tabs.addTab(self._portfolio_tab, "📊 Portfolio")

        # Options Chain tab
        self._chain_tab = ChainTab(self._engine)
        self._tabs.addTab(self._chain_tab, "📈 Options Chain")

        # Risk tab
        self._risk_tab = RiskTab(self._engine)
        self._tabs.addTab(self._risk_tab, "⚠ Risk")

        # Strategies tab
        self._strategies_tab = StrategiesTab(self._engine)
        self._tabs.addTab(self._strategies_tab, "🧠 Strategies")

        # Orders tab
        self._orders_tab = OrdersTab(self._engine)
        self._tabs.addTab(self._orders_tab, "📋 Orders")

        # Journal tab
        self._journal_tab = JournalTab(self._engine)
        self._tabs.addTab(self._journal_tab, "📓 Journal")

        # AI / Risk tab
        self._ai_tab = AIRiskTab(self._engine)
        self._tabs.addTab(self._ai_tab, "🤖 AI / Risk")

        # Market Data tab
        self._market_tab = MarketTab(self._engine)
        self._tabs.addTab(self._market_tab, "💹 Market Data")

        self.setCentralWidget(self._tabs)

        # ── Right dock: Order Entry ───────────────────────────────────────
        self._order_entry = OrderEntryPanel(self._engine)
        dock = QDockWidget("Order Entry", self)
        dock.setWidget(self._order_entry)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        dock.setMinimumWidth(320)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def _setup_toolbar(self) -> None:
        toolbar = QToolBar("Main Toolbar")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        # Connect / Disconnect
        self._act_connect = QAction("🔌 Connect", self)
        self._act_connect.triggered.connect(self._on_connect)
        toolbar.addAction(self._act_connect)

        self._act_disconnect = QAction("🔌 Disconnect", self)
        self._act_disconnect.triggered.connect(self._on_disconnect)
        self._act_disconnect.setEnabled(False)
        toolbar.addAction(self._act_disconnect)

        toolbar.addSeparator()

        # Refresh
        self._act_refresh = QAction("🔄 Refresh All", self)
        self._act_refresh.triggered.connect(self._on_refresh_all)
        self._act_refresh.setEnabled(False)
        toolbar.addAction(self._act_refresh)

        self._act_compact_mode = QAction("🗜 Compact Mode", self)
        self._act_compact_mode.setCheckable(True)
        self._act_compact_mode.setChecked(bool(self._preferences.get("compact_mode", False)))
        self._act_compact_mode.triggered.connect(self._on_compact_mode_toggled)
        toolbar.addAction(self._act_compact_mode)

        toolbar.addSeparator()

        def token_checker(profile: str) -> bool:
            return self._token_manager.has_configured_token(profile)

        self._account_picker = AccountPicker(
            active_profile=self._token_manager.active_profile,
            token_checker=token_checker,
            parent=self
        )
        self._account_picker.profile_changed.connect(self._on_copilot_profile_changed)
        toolbar.addWidget(self._account_picker)

        toolbar.addSeparator()

        # Connection status label
        self._lbl_conn = QLabel("  ⚪ Disconnected  ")
        self._lbl_conn.setStyleSheet(
            "background: #e74c3c; color: white; padding: 4px 10px; border-radius: 4px;"
        )
        toolbar.addWidget(self._lbl_conn)

    def _setup_statusbar(self) -> None:
        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._statusbar.showMessage(
            f"Ready — click Connect to start · Copilot profile: {self._token_manager.active_profile.title()}"
        )

    def _setup_auto_refresh(self) -> None:
        """Auto-refresh positions every 60 seconds while connected."""
        self._refresh_timer = QTimer(self)
        refresh_ms = max(5_000, int(os.getenv("DESKTOP_PORTFOLIO_REFRESH_MS", "60000")))
        self._refresh_timer.setInterval(refresh_ms)
        self._refresh_timer.timeout.connect(self._on_refresh_all)

    def _connect_signals(self) -> None:
        self._engine.connected.connect(self._on_engine_connected)
        self._engine.disconnected.connect(self._on_engine_disconnected)
        self._engine.error_occurred.connect(self._on_engine_error)
        self._engine.connection_state.connect(self._on_connection_state_changed)

        # Chain click → Order Entry prefill
        self._chain_tab.chain_row_selected.connect(self._order_entry.prefill_from_chain)
        self._chain_tab.leg_clicked.connect(self._on_chain_leg_clicked)
        self._portfolio_tab.position_action_requested.connect(self._on_portfolio_action_requested)
        self._ai_tab.suggestion_authorized.connect(self._on_ai_suggestion_authorized)

        # Order submitted → auto-refresh orders
        self._order_entry.order_submitted.connect(self._on_order_submitted)

        # Order filled → refresh positions & orders
        self._engine.order_filled.connect(self._on_order_filled)
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # Background agents → AI/Risk tab
        self._agent_runner.alert_raised.connect(self._ai_tab.on_risk_alert)
        self._agent_runner.arb_signal.connect(self._ai_tab.on_arb_signal)
        self._agent_runner.trade_suggestion.connect(self._ai_tab.on_trade_suggestion)
        self._apply_compact_mode(self._act_compact_mode.isChecked())

    @Slot(dict)
    def _on_order_submitted(self, result: dict) -> None:
        oid = str(result.get('order_id', '?'))[:8]
        self._statusbar.showMessage(f"Order {result.get('status', '?')} — ID: {oid}")
        loop = asyncio.get_event_loop()
        loop.create_task(self._engine.get_open_orders())

    @Slot(dict)
    def _on_order_filled(self, fill_info: dict) -> None:
        self._sound_engine.play("order_filled")
        self._statusbar.showMessage(
            f"Order filled — avg ${fill_info.get('avg_price', 0):.2f}"
        )
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_refresh_all())

    @Slot(object, str)
    def _on_chain_leg_clicked(self, chain_row, action: str) -> None:
        """Single bid/ask click — append one leg to order entry immediately."""
        self._order_entry.add_chain_leg(chain_row, action)
        self.statusBar().showMessage(
            f"✅ Added {action} {chain_row.underlying} "
            f"{chain_row.strike:.0f}{'P' if chain_row.right == 'P' else 'C'} to Order Entry",
            3000,
        )

    @Slot(dict)
    def _on_portfolio_action_requested(self, payload: dict) -> None:
        action = str(payload.get("action") or "BUY").upper()
        rows = list(payload.get("legs") or [])
        if not rows:
            self._statusbar.showMessage("No tradable legs found for the selected row")
            return

        staged_legs = self._build_portfolio_order_legs(rows, action, payload.get("kind") == "trade_group")
        rationale = ""
        if action == "ROLL":
            rationale = (
                "Roll requested — current position legs are staged as closing legs. "
                "The chain tab has been opened on the same underlying with a target expiry about 7 days out so you can pick the opening leg(s)."
            )
        self._order_entry.prefill_from_legs(staged_legs, rationale=rationale, source=f"Portfolio {action.title()}")
        if action == "ROLL":
            self._tabs.setCurrentWidget(self._chain_tab)
            loop = asyncio.get_event_loop()
            loop.create_task(self._async_focus_roll_chain(rows))
        self._statusbar.showMessage(
            f"Portfolio action staged: {action.title()} {payload.get('description') or 'position'}"
        )

    async def _async_focus_roll_chain(self, rows: list) -> None:
        option_rows = [row for row in rows if str(getattr(row, "sec_type", "") or "").upper() in {"OPT", "FOP"}]
        if not option_rows:
            return
        first_row = option_rows[0]
        underlying = getattr(first_row, "underlying", None) or getattr(first_row, "symbol", "")
        sec_type = str(getattr(first_row, "sec_type", "OPT") or "OPT").upper()
        exchange = "CME" if sec_type == "FOP" else "SMART"
        expiry = getattr(first_row, "expiry", None)
        await self._chain_tab.focus_roll_target(
            underlying=str(underlying or ""),
            sec_type=sec_type,
            exchange=exchange,
            current_expiry=str(expiry or ""),
            min_days_forward=7,
        )

    def _build_portfolio_order_legs(self, rows: list, action: str, from_trade_group: bool) -> list[dict]:
        staged: list[dict] = []
        for row in rows:
            sec_type = str(getattr(row, "sec_type", "OPT") or "OPT").upper()
            current_action = "BUY" if float(getattr(row, "quantity", 0) or 0) > 0 else "SELL"
            if action == "ROLL":
                leg_action = "SELL" if current_action == "BUY" else "BUY"
            elif from_trade_group:
                leg_action = current_action if action == "SELL" else ("SELL" if current_action == "BUY" else "BUY")
            else:
                leg_action = action

            staged.append({
                "symbol": getattr(row, "underlying", None) or getattr(row, "symbol", ""),
                "sec_type": sec_type,
                "exchange": "CME" if sec_type in {"FOP", "FUT"} else "SMART",
                "action": leg_action,
                "qty": max(1, int(abs(float(getattr(row, "quantity", 0) or 1)))),
                "strike": float(getattr(row, "strike", 0) or 0),
                "right": getattr(row, "right", None) or "C",
                "expiry": getattr(row, "expiry", None) or "",
                "conid": int(getattr(row, "conid", 0) or 0),
            })
        return staged

    @Slot(dict)
    def _on_ai_suggestion_authorized(self, payload: dict) -> None:
        legs = payload.get("legs") or []
        rationale = payload.get("rationale") or ""
        model = payload.get("model") or ""
        if not legs:
            self._statusbar.showMessage("AI suggestion had no tradable legs")
            return
        self._order_entry.prefill_from_legs(legs, rationale=rationale, source=f"AI ({model})")
        # Stay on current tab — the Order Entry panel is always visible as a dock widget
        self._statusbar.showMessage(
            f"AI suggestion staged in Order Entry ({len(legs)} leg(s)) — review and submit manually"
        )

    # ── toolbar slots ─────────────────────────────────────────────────────

    @Slot()
    def _on_connect(self) -> None:
        self._statusbar.showMessage("Connecting to IBKR…")
        self._act_connect.setEnabled(False)
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_connect())

    async def _async_connect(self) -> None:
        try:
            await self._engine.connect()
        except Exception as exc:
            self._statusbar.showMessage(f"❌ Connection failed: {exc}")
            self._act_connect.setEnabled(True)

    @Slot()
    def _on_disconnect(self) -> None:
        loop = asyncio.get_event_loop()
        loop.create_task(self._engine.disconnect())

    @Slot()
    def _on_refresh_all(self) -> None:
        if not self._engine.is_connected:
            return
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_refresh_all())

    async def _async_refresh_all(self) -> None:
        try:
            self._statusbar.showMessage("Refreshing…")
            await self._engine.refresh_positions()
            await self._engine.refresh_account()
            await self._engine.get_open_orders()
            self._statusbar.showMessage("✅ Updated")
        except Exception as exc:
            self._statusbar.showMessage(f"❌ Refresh error: {exc}")

    # ── engine signal slots ───────────────────────────────────────────────

    @Slot()
    def _on_engine_connected(self) -> None:
        self._lbl_conn.setText(f"  🟢 Connected — {self._engine.account_id}  ")
        self._lbl_conn.setStyleSheet(
            "background: #27ae60; color: white; padding: 4px 10px; border-radius: 4px;"
        )
        self._act_connect.setEnabled(False)
        self._act_disconnect.setEnabled(True)
        self._act_refresh.setEnabled(True)
        self._refresh_timer.start()
        self._statusbar.showMessage(f"Connected to IBKR — account {self._engine.account_id}")

        # Start background agents
        self._agent_runner.start()

        # Auto-load positions on connect
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_refresh_all())

    @Slot()
    def _on_engine_disconnected(self) -> None:
        self._sound_engine.play("connection_lost")
        self._lbl_conn.setText("  ⚪ Disconnected  ")
        self._lbl_conn.setStyleSheet(
            "background: #e74c3c; color: white; padding: 4px 10px; border-radius: 4px;"
        )
        self._act_connect.setEnabled(True)
        self._act_disconnect.setEnabled(False)
        self._act_refresh.setEnabled(False)
        self._refresh_timer.stop()
        # Stop background agents
        self._agent_runner.stop()
        self._statusbar.showMessage("Disconnected from IBKR")

    @Slot(str)
    def _on_engine_error(self, msg: str) -> None:
        self._statusbar.showMessage(f"⚠ {msg}")

    @Slot(str, str)
    def _on_connection_state_changed(self, state: str, detail: str) -> None:
        self._statusbar.showMessage(detail)
        if state == "reconnecting":
            self._lbl_conn.setText("  🟡 Reconnecting…  ")
            self._lbl_conn.setStyleSheet(
                "background: #f39c12; color: white; padding: 4px 10px; border-radius: 4px;"
            )
            self._act_connect.setEnabled(False)
            self._act_disconnect.setEnabled(False)
        elif state == "failed":
            self._sound_engine.play("connection_failed")
            self._lbl_conn.setText("  🔴 Reconnect Failed  ")
            self._lbl_conn.setStyleSheet(
                "background: #c0392b; color: white; padding: 4px 10px; border-radius: 4px;"
            )
            self._act_connect.setEnabled(True)

    @Slot(bool)
    def _on_compact_mode_toggled(self, checked: bool) -> None:
        self._apply_compact_mode(checked)
        self._preferences["compact_mode"] = bool(checked)
        save_preferences(self._preferences, self._preferences_path)
        mode = "Compact" if checked else "Full"
        self._statusbar.showMessage(f"{mode} mode enabled")

    def _apply_compact_mode(self, enabled: bool) -> None:
        if hasattr(self._portfolio_tab, "set_compact_mode"):
            self._portfolio_tab.set_compact_mode(enabled)
        if hasattr(self._orders_tab, "set_compact_mode"):
            self._orders_tab.set_compact_mode(enabled)

    @Slot(str)
    def _on_copilot_profile_changed(self, profile: str) -> None:
        state = self._token_manager.set_active_profile(profile)
        token_state = "configured" if state.token_available else "missing token"
        self._statusbar.showMessage(
            f"Copilot profile switched to {state.profile.title()} — {token_state}"
        )

    @Slot(int)
    def _on_tab_changed(self, _: int) -> None:
        if hasattr(self._market_tab, "_sync_refresh_timer_state"):
            self._market_tab._sync_refresh_timer_state()

    def closeEvent(self, event) -> None:
        """Gracefully disconnect IB before Qt event loop shuts down."""
        self._refresh_timer.stop()
        self._agent_runner.stop()
        try:
            self._engine._manual_disconnect_requested = True
            if self._engine._ib.isConnected():
                self._engine._ib.disconnect()
        except Exception:
            pass
        # Cancel all pending async tasks to suppress "Task was destroyed" warnings
        for task in asyncio.all_tasks(asyncio.get_event_loop()):
            if not task.done():
                task.cancel()
        event.accept()
