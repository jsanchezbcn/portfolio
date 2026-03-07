"""desktop/ui/orders_tab.py — Open Orders management tab.

Shows:
  1. List of all open/working orders (QTableView)
  2. Cancel individual orders
  3. Cancel all orders
  4. Auto-refresh via engine signals
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTableView, QHeaderView, QAbstractItemView, QMessageBox,
    QInputDialog,
)
from PySide6.QtCore import Qt, Slot, QAbstractTableModel, QModelIndex, QTimer
from PySide6.QtCore import QSortFilterProxyModel

if TYPE_CHECKING:
    from desktop.engine.ib_engine import IBEngine, OpenOrder


# ── Table Model ───────────────────────────────────────────────────────────

_ORDER_HEADERS = [
    "Order ID", "Symbol", "Action", "Qty", "Type",
    "Limit", "Status", "Filled", "Remaining", "Avg Fill",
]


class OpenOrdersTableModel(QAbstractTableModel):
    """Model for the open orders table."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[OpenOrder] = []

    def set_data(self, rows: list) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def get_order_at(self, row: int):
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_ORDER_HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _ORDER_HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        o = self._rows[index.row()]
        col = index.column()
        match col:
            case 0: return str(o.order_id)
            case 1: return o.symbol
            case 2: return o.action
            case 3: return f"{o.quantity:.0f}"
            case 4: return o.order_type
            case 5: return f"${o.limit_price:.2f}" if o.limit_price else "MKT"
            case 6: return o.status
            case 7: return f"{o.filled:.0f}"
            case 8: return f"{o.remaining:.0f}"
            case 9: return f"${o.avg_fill_price:.2f}" if o.avg_fill_price else ""
            case _: return ""


# ── Orders Tab Widget ─────────────────────────────────────────────────────


class OrdersTab(QWidget):
    """Orders management tab: view and cancel open orders."""

    def __init__(self, engine: IBEngine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._compact_mode = False
        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── Toolbar ───────────────────────────────────────────────────────
        toolbar = QHBoxLayout()

        self._btn_refresh = QPushButton("🔄 Refresh Orders")
        self._btn_refresh.setFixedHeight(32)
        toolbar.addWidget(self._btn_refresh)

        self._btn_cancel_selected = QPushButton("❌ Cancel Selected")
        self._btn_cancel_selected.setFixedHeight(32)
        self._btn_cancel_selected.setEnabled(False)
        self._btn_cancel_selected.setStyleSheet("background: #e67e22; color: white; padding: 4px 12px;")
        toolbar.addWidget(self._btn_cancel_selected)

        self._btn_cancel_all = QPushButton("🚨 Cancel All Orders")
        self._btn_cancel_all.setFixedHeight(32)
        self._btn_cancel_all.setStyleSheet("background: #e74c3c; color: white; padding: 4px 12px;")
        toolbar.addWidget(self._btn_cancel_all)

        self._btn_modify_price = QPushButton("✏️ Modify Price")
        self._btn_modify_price.setFixedHeight(32)
        self._btn_modify_price.setEnabled(False)
        self._btn_modify_price.setStyleSheet("background: #8e44ad; color: white; padding: 4px 12px;")
        toolbar.addWidget(self._btn_modify_price)

        toolbar.addStretch()

        self._lbl_status = QLabel("Ready")
        self._lbl_status.setStyleSheet("color: #888;")
        toolbar.addWidget(self._lbl_status)

        layout.addLayout(toolbar)

        # ── Orders Table ──────────────────────────────────────────────────
        self._model = OpenOrdersTableModel()
        self._proxy = QSortFilterProxyModel()
        self._proxy.setSourceModel(self._model)
        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setVisible(False)

        layout.addWidget(self._table, stretch=1)

    def set_compact_mode(self, enabled: bool) -> None:
        self._compact_mode = bool(enabled)
        hidden_columns = {7, 8, 9} if self._compact_mode else set()
        for col in range(self._proxy.columnCount() if self._proxy else 10):
            self._table.setColumnHidden(col, col in hidden_columns)

    def _connect_signals(self) -> None:
        self._btn_refresh.clicked.connect(self._on_refresh)
        self._btn_cancel_selected.clicked.connect(self._on_cancel_selected)
        self._btn_cancel_all.clicked.connect(self._on_cancel_all)
        self._btn_modify_price.clicked.connect(self._on_modify_price)
        self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self._engine.orders_updated.connect(self._on_orders_updated)
        self._engine.order_status.connect(self._on_order_status_change)

    @Slot()
    def _on_selection_changed(self) -> None:
        selected = self._table.selectionModel().selectedRows()
        has_sel = len(selected) > 0
        self._btn_cancel_selected.setEnabled(has_sel)
        # Only enable modify for LIMIT orders (check the model)
        if has_sel:
            source_index = self._proxy.mapToSource(selected[0])
            order = self._model.get_order_at(source_index.row())
            is_limit = order is not None and (order.order_type or "").upper() in ("LMT", "LIMIT")
            self._btn_modify_price.setEnabled(is_limit)
        else:
            self._btn_modify_price.setEnabled(False)

    @Slot()
    def _on_refresh(self) -> None:
        self._lbl_status.setText("Refreshing…")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        loop.create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        try:
            await self._engine.get_open_orders()
            self._lbl_status.setText("✅ Updated")
        except Exception as exc:
            self._lbl_status.setText(f"❌ {exc}")

    @Slot()
    def _on_cancel_selected(self) -> None:
        selected = self._table.selectionModel().selectedRows()
        if not selected:
            return
        source_index = self._proxy.mapToSource(selected[0])
        order = self._model.get_order_at(source_index.row())
        if not order:
            return

        reply = QMessageBox.warning(
            self,
            "Cancel Order",
            f"Cancel order {order.order_id} ({order.symbol} {order.action} {order.quantity:.0f})?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        loop.create_task(self._async_cancel(order.order_id))

    async def _async_cancel(self, order_id: int) -> None:
        try:
            await self._engine.cancel_order(order_id)
            self._lbl_status.setText(f"✅ Cancelled order {order_id}")
            await asyncio.sleep(1)
            await self._engine.get_open_orders()
        except Exception as exc:
            self._lbl_status.setText(f"❌ {exc}")

    @Slot()
    def _on_modify_price(self) -> None:
        selected = self._table.selectionModel().selectedRows()
        if not selected:
            return
        source_index = self._proxy.mapToSource(selected[0])
        order = self._model.get_order_at(source_index.row())
        if not order:
            return

        current_price = order.limit_price or 0.0
        # PySide6 QInputDialog.getDouble uses positional args — no keyword 'min'/'max'
        new_price, ok = QInputDialog.getDouble(
            self,
            "Modify Limit Price",
            f"New limit price for order {order.order_id}\n"
            f"({order.symbol} {order.action} {order.quantity:.0f} @ ${current_price:.2f}):",
            current_price,   # value (positional)
            0.0,             # min
            999999.0,        # max
            2,               # decimals
        )
        if not ok or new_price <= 0:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        loop.create_task(self._async_modify_price(order.order_id, new_price))

    async def _async_modify_price(self, order_id: int, new_price: float) -> None:
        try:
            await self._engine.modify_order_price(order_id, new_price)
            self._lbl_status.setText(f"✅ Order {order_id} repriced to ${new_price:.2f}")
            await asyncio.sleep(1)
            await self._engine.get_open_orders()
        except Exception as exc:
            self._lbl_status.setText(f"❌ {exc}")

    @Slot()
    def _on_cancel_all(self) -> None:
        reply = QMessageBox.warning(
            self,
            "⚠ Cancel ALL Orders",
            "This will cancel ALL open orders.\nAre you sure?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        loop.create_task(self._async_cancel_all())

    async def _async_cancel_all(self) -> None:
        try:
            count = await self._engine.cancel_all_orders()
            self._lbl_status.setText(f"✅ Cancelled {count} orders")
            await asyncio.sleep(1)
            await self._engine.get_open_orders()
        except Exception as exc:
            self._lbl_status.setText(f"❌ {exc}")

    @Slot(list)
    def _on_orders_updated(self, orders: list) -> None:
        self._model.set_data(orders)
        self._lbl_status.setText(f"{len(orders)} open orders")
        self.set_compact_mode(self._compact_mode)

    @Slot(dict)
    def _on_order_status_change(self, info: dict) -> None:
        """Auto-refresh when an order status changes (debounced 1s)."""
        if not hasattr(self, '_debounce_timer'):
            self._debounce_timer = QTimer(self)
            self._debounce_timer.setSingleShot(True)
            self._debounce_timer.setInterval(1000)
            self._debounce_timer.timeout.connect(self._on_refresh)
        self._debounce_timer.start()  # restart = debounce
