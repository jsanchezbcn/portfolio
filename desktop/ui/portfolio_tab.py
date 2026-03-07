"""desktop/ui/portfolio_tab.py — Portfolio positions table with PnL.

Shows live positions synced from IBKR:
  Symbol | Type | Qty | Avg Cost | Mkt Price | Unrealized PnL | Greeks…
Positions are color-coded: green for gains, red for losses.

Two view modes:
  Raw    — positions grouped by expiry (original behaviour)
  Trades — positions grouped into logical trade structures (strangles, spreads …)
           using the heuristic grouper in desktop/models/trade_groups.py.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTableView, QHeaderView, QGroupBox, QSplitter,
    QButtonGroup, QRadioButton, QFrame, QComboBox, QCheckBox,
)
from PySide6.QtCore import Qt, Slot, Signal, QPoint

from desktop.models.table_models import PositionsTableModel
from desktop.models.trade_groups import TradeGroupsModel
from desktop.ui.widgets.position_menu import PositionContextMenu

if TYPE_CHECKING:
    from desktop.engine.ib_engine import IBEngine, AccountSummary


class PortfolioTab(QWidget):
    """Portfolio tab: account summary cards + positions table.

    The view toggle (Raw | Trades) lives in the toolbar.
    """

    position_action_requested = Signal(dict)

    def __init__(self, engine: IBEngine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._raw_positions: list = []   # last fetched rows
        self._compact_mode = False
        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── Account summary cards ─────────────────────────────────────────
        summary_box = QGroupBox("Account Summary")
        summary_layout = QHBoxLayout(summary_box)

        self._lbl_nlv = self._metric_label("NLV", "$0.00")
        self._lbl_cash = self._metric_label("Cash", "$0.00")
        self._lbl_bp = self._metric_label("Buying Power", "$0.00")
        self._lbl_margin = self._metric_label("Margin Used", "$0.00")
        self._lbl_upnl = self._metric_label("Unrealized PnL", "$0.00")
        self._lbl_rpnl = self._metric_label("Realized PnL", "$0.00")

        for lbl in (self._lbl_nlv, self._lbl_cash, self._lbl_bp,
                    self._lbl_margin, self._lbl_upnl, self._lbl_rpnl):
            summary_layout.addWidget(lbl)

        layout.addWidget(summary_box)

        # ── Data quality warning banner ────────────────────────────────────
        self._lbl_data_quality = QLabel("")
        self._lbl_data_quality.setWordWrap(True)
        self._lbl_data_quality.setVisible(False)
        self._lbl_data_quality.setStyleSheet(
            "QLabel { background: #fff3cd; color: #856404; padding: 6px 10px; "
            "border-radius: 4px; font-size: 12px; }"
        )
        layout.addWidget(self._lbl_data_quality)

        # ── Toolbar ───────────────────────────────────────────────────────
        toolbar = QHBoxLayout()
        self._btn_refresh = QPushButton("🔄 Refresh Positions")
        self._btn_refresh.setFixedHeight(32)
        toolbar.addWidget(self._btn_refresh)

        # View toggle (Raw | Trades)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        toolbar.addSpacing(8)
        toolbar.addWidget(sep)
        toolbar.addSpacing(8)

        view_lbl = QLabel("View:")
        view_lbl.setStyleSheet("color:#aaa;")
        toolbar.addWidget(view_lbl)

        self._btn_raw    = QRadioButton("Raw")
        self._btn_trades = QRadioButton("Trades")
        self._btn_raw.setChecked(True)
        self._btn_raw.setStyleSheet("color:white;")
        self._btn_trades.setStyleSheet("color:white;")
        self._view_group = QButtonGroup(self)
        self._view_group.addButton(self._btn_raw,    0)
        self._view_group.addButton(self._btn_trades, 1)
        toolbar.addWidget(self._btn_raw)
        toolbar.addWidget(self._btn_trades)

        toolbar.addSpacing(12)
        sort_lbl = QLabel("Sort by:")
        sort_lbl.setStyleSheet("color:#aaa;")
        toolbar.addWidget(sort_lbl)

        self._cmb_sort_metric = QComboBox()
        self._cmb_sort_metric.addItem("Default", userData="none")
        self._cmb_sort_metric.addItem("Unrealized PnL", userData="unrealized_pnl")
        self._cmb_sort_metric.addItem("Market Value", userData="market_value")
        self._cmb_sort_metric.addItem("Delta (Δ)", userData="delta")
        self._cmb_sort_metric.addItem("Gamma (Γ)", userData="gamma")
        self._cmb_sort_metric.addItem("Theta (Θ)", userData="theta")
        self._cmb_sort_metric.addItem("Vega (V)", userData="vega")
        self._cmb_sort_metric.addItem("SPX Delta", userData="spx_delta")
        toolbar.addWidget(self._cmb_sort_metric)

        self._chk_sort_abs = QCheckBox("Abs")
        self._chk_sort_abs.setChecked(True)
        self._chk_sort_abs.setToolTip("Sort by absolute value (magnitude)")
        toolbar.addWidget(self._chk_sort_abs)

        self._cmb_sort_order = QComboBox()
        self._cmb_sort_order.addItem("Desc", userData=True)
        self._cmb_sort_order.addItem("Asc", userData=False)
        toolbar.addWidget(self._cmb_sort_order)

        toolbar.addStretch()
        self._lbl_status = QLabel("Ready")
        self._lbl_status.setStyleSheet("color: #888;")
        toolbar.addWidget(self._lbl_status)
        layout.addLayout(toolbar)

        # ── Positions table ───────────────────────────────────────────────
        self._raw_model    = PositionsTableModel()
        self._trades_model = TradeGroupsModel()
        self._model = self._raw_model
        self._table = QTableView()
        self._table.setModel(self._raw_model)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(False)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setVisible(False)

        layout.addWidget(self._table, stretch=1)

    def set_compact_mode(self, enabled: bool) -> None:
        self._compact_mode = bool(enabled)
        hidden_columns = {2, 4, 8, 9, 10, 11, 16} if self._compact_mode else set()
        for col in range(self._table.model().columnCount() if self._table.model() else 18):
            self._table.setColumnHidden(col, col in hidden_columns)


    def _metric_label(self, title: str, value: str) -> QLabel:
        lbl = QLabel(f"<small>{title}</small><br/><b>{value}</b>")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setMinimumWidth(120)
        lbl.setStyleSheet(
            "QLabel { background: #2d2d2d; color: white; padding: 8px; "
            "border-radius: 6px; font-size: 13px; }"
        )
        return lbl

    def _connect_signals(self) -> None:
        self._btn_refresh.clicked.connect(self._on_refresh)
        self._engine.positions_updated.connect(self._on_positions_updated)
        self._engine.account_updated.connect(self._on_account_updated)
        self._view_group.idToggled.connect(self._on_view_toggled)
        self._table.customContextMenuRequested.connect(self._on_context_menu_requested)
        self._cmb_sort_metric.currentIndexChanged.connect(self._on_sort_changed)
        self._cmb_sort_order.currentIndexChanged.connect(self._on_sort_changed)
        self._chk_sort_abs.stateChanged.connect(self._on_sort_changed)

    # ── slots ─────────────────────────────────────────────────────────────

    @Slot(int, bool)
    def _on_view_toggled(self, btn_id: int, checked: bool) -> None:
        if not checked:
            return
        self._apply_current_view_data()
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.set_compact_mode(self._compact_mode)

    @Slot()
    def _on_sort_changed(self) -> None:
        self._apply_current_view_data()

    @Slot()
    def _on_refresh(self) -> None:
        self._lbl_status.setText("Refreshing…")
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        try:
            await self._engine.refresh_positions()
            await self._engine.refresh_account()
            self._lbl_status.setText("✅ Updated")
        except Exception as exc:
            self._lbl_status.setText(f"❌ {exc}")

    @Slot(list)
    def _on_positions_updated(self, rows: list) -> None:
        self._raw_positions = rows
        self._apply_current_view_data()
        self._lbl_status.setText(f"{len(rows)} positions loaded")

        # ── Data quality banner ──────────────────────────────────────────
        missing_greeks = sum(
            1 for r in rows
            if getattr(r, "sec_type", "") in ("OPT", "FOP") and getattr(r, "delta", None) is None
        )
        missing_spx = sum(
            1 for r in rows
            if getattr(r, "sec_type", "") == "STK" and getattr(r, "spx_delta", None) is None
        )
        estimated_greeks = sum(
            1 for r in rows
            if getattr(r, "sec_type", "") in ("OPT", "FOP") and getattr(r, "greeks_source", None) == "estimated_bsm"
        )
        parts: list[str] = []
        if missing_greeks:
            parts.append(f"⚠ {missing_greeks} option(s) missing Greeks")
        if estimated_greeks:
            parts.append(f"≈ {estimated_greeks} option(s) using local estimated Greeks")
        if missing_spx:
            parts.append(f"⚠ {missing_spx} stock(s) missing SPX Δ")
        self._lbl_data_quality.setText("   |   ".join(parts))
        self._lbl_data_quality.setVisible(bool(parts))
        self.set_compact_mode(self._compact_mode)

    def _apply_current_view_data(self) -> None:
        metric = str(self._cmb_sort_metric.currentData() or "none")
        descending = bool(self._cmb_sort_order.currentData())
        absolute = self._chk_sort_abs.isChecked()

        # Trade-groups view is intentionally grouped/structured and does not support
        # numeric metric sorting. When a sort metric is selected, force Raw view.
        if metric != "none" and self._view_group.checkedId() != 0:
            self._btn_raw.setChecked(True)
            self._lbl_status.setText("Sorted view uses Raw mode")

        if self._view_group.checkedId() == 0:
            self._raw_model.set_sorting(metric, descending=descending, absolute=absolute)
            self._raw_model.set_data(self._raw_positions)
            self._model = self._raw_model
            self._table.setModel(self._raw_model)
            return

        self._trades_model.set_data(self._raw_positions)
        self._model = self._trades_model
        self._table.setModel(self._trades_model)

    @Slot(object)
    def _on_account_updated(self, summary: AccountSummary) -> None:
        self._lbl_nlv.setText(f"<small>NLV</small><br/><b>${summary.net_liquidation:,.2f}</b>")
        self._lbl_cash.setText(f"<small>Cash</small><br/><b>${summary.total_cash:,.2f}</b>")
        self._lbl_bp.setText(f"<small>Buying Power</small><br/><b>${summary.buying_power:,.2f}</b>")
        self._lbl_margin.setText(f"<small>Init Margin</small><br/><b>${summary.init_margin:,.2f}</b>")

        upnl_color = "#27ae60" if summary.unrealized_pnl >= 0 else "#e74c3c"
        self._lbl_upnl.setText(
            f"<small>Unrealized PnL</small><br/>"
            f"<b style='color:{upnl_color}'>${summary.unrealized_pnl:+,.2f}</b>"
        )
        rpnl_color = "#27ae60" if summary.realized_pnl >= 0 else "#e74c3c"
        self._lbl_rpnl.setText(
            f"<small>Realized PnL</small><br/>"
            f"<b style='color:{rpnl_color}'>${summary.realized_pnl:+,.2f}</b>"
        )

    @Slot(QPoint)
    def _on_context_menu_requested(self, point: QPoint) -> None:
        index = self._table.indexAt(point)
        if not index.isValid():
            return
        payload = self._payload_for_row(index.row())
        if not payload:
            return
        menu = PositionContextMenu(self, payload)
        action = menu.exec_for_table(self._table, point)
        if action:
            self._emit_position_action(payload, action)

    def _payload_for_row(self, row: int) -> dict | None:
        model = self._table.model()
        if model is self._raw_model:
            position = self._raw_model.position_at(row)
            if position is None:
                return None
            return {
                "kind": "position",
                "view": "raw",
                "description": position.symbol,
                "legs": [position],
            }
        if model is self._trades_model:
            payload = self._trades_model.payload_at(row)
            if payload is None:
                return None
            payload["view"] = "trades"
            return payload
        return None

    def _emit_position_action(self, payload: dict, action: str) -> None:
        outbound = dict(payload)
        outbound["action"] = action.upper()
        self.position_action_requested.emit(outbound)
