"""desktop/ui/chain_tab.py — Options Chain Matrix.

Layout:
  [Underlying ▼] [Expiry ▼] [Fetch Chain]
  ┌──────────────────────────────────────────────────────────────┐
  │  CALLS                    │ Strike │                  PUTS   │
  │ Bid Ask Last Vol OI IV Δ Γ│        │Δ Γ IV OI Vol Last Ask Bid│
  │  …                       │        │                    …    │
  └──────────────────────────────────────────────────────────────┘
  Double-click a row → populates Order Entry panel with that strike.
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QComboBox, QTableView, QHeaderView, QAbstractItemView,
    QTableWidget, QTableWidgetItem, QGroupBox,
)
from PySide6.QtCore import Qt, Signal, Slot, QModelIndex, QTimer

from desktop.models.table_models import ChainTableModel

if TYPE_CHECKING:
    from desktop.engine.ib_engine import IBEngine, ChainRow

# Column indices in the chain matrix
_COL_CALL_BID = 0
_COL_CALL_ASK = 1
_N_CALL_COLS   = 8   # Bid Ask Last Vol OI IV Δ Γ
_COL_STRIKE    = 8
_COL_PUT_ASK   = 15  # mirror of call ask on put side
_COL_PUT_BID   = 16  # mirror of call bid


class ChainTab(QWidget):
    """Options chain matrix with calls left / puts right / strike center."""

    # Emitted when user double-clicks a call or put row (non-bid/ask col)
    chain_row_selected = Signal(object)   # ChainRow
    # Emitted on single-click of a bid/ask cell — direct add to order entry
    leg_clicked        = Signal(object, str)  # (ChainRow, action: "BUY"|"SELL")
    # Emitted when user clicks Send Combo — sends all accumulated cart legs
    leg_cart_ready     = Signal(list)

    def __init__(self, engine: IBEngine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._last_chain_params: tuple | None = None  # (underlying, expiry, sec_type, exchange)
        self._cart: list[tuple] = []  # list of (ChainRow, action_str)
        self._setup_ui()
        self._connect_signals()
        # ── Live price refresh timer (visible strikes only) ──
        self._stream_timer = QTimer(self)
        self._stream_timer.setInterval(60_000)  # 60s streaming refresh
        self._stream_timer.timeout.connect(self._on_stream_tick)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── Toolbar ───────────────────────────────────────────────────────
        toolbar = QHBoxLayout()

        toolbar.addWidget(QLabel("Underlying:"))
        self._cmb_underlying = QComboBox()
        self._cmb_underlying.addItems(["ES", "MES", "SPY", "QQQ", "NQ"])
        self._cmb_underlying.setCurrentText("ES")
        self._cmb_underlying.setMinimumWidth(80)
        toolbar.addWidget(self._cmb_underlying)

        toolbar.addWidget(QLabel("SecType:"))
        self._cmb_sec_type = QComboBox()
        self._cmb_sec_type.addItems(["FOP", "OPT"])
        toolbar.addWidget(self._cmb_sec_type)

        toolbar.addWidget(QLabel("Exchange:"))
        self._cmb_exchange = QComboBox()
        self._cmb_exchange.addItems(["CME", "SMART", "CBOE"])
        toolbar.addWidget(self._cmb_exchange)

        toolbar.addWidget(QLabel("Expiry:"))
        self._cmb_expiry = QComboBox()
        self._cmb_expiry.setEditable(True)
        self._cmb_expiry.setMinimumWidth(100)
        toolbar.addWidget(self._cmb_expiry)

        self._btn_fetch = QPushButton("🔄 Fetch Chain")
        self._btn_fetch.setFixedHeight(30)
        toolbar.addWidget(self._btn_fetch)

        self._btn_clear_reload = QPushButton("⟳ Clear & Reload")
        self._btn_clear_reload.setFixedHeight(30)
        self._btn_clear_reload.setToolTip("Cancel streaming, clear cached data, and re-fetch fresh chain data including Greeks")
        toolbar.addWidget(self._btn_clear_reload)

        toolbar.addStretch()
        self._lbl_status = QLabel("Ready")
        self._lbl_status.setStyleSheet("color: #888;")
        toolbar.addWidget(self._lbl_status)

        layout.addLayout(toolbar)

        # ── Chain table ───────────────────────────────────────────────────
        self._model = ChainTableModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setStyleSheet("""
            QTableView {
                gridline-color: #444;
            }
            QTableView::item:selected {
                background: #3498db;
                color: white;
            }
        """)

        # ── CALL / PUT section labels ─────────────────────────────────
        header_bar = QHBoxLayout()
        header_bar.setContentsMargins(0, 0, 0, 0)
        header_bar.setSpacing(0)
        lbl_calls = QLabel("◀  CALLS")
        lbl_calls.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        lbl_calls.setStyleSheet(
            "background:#1a6b2e;color:white;font-weight:bold;"
            "padding:2px 8px;border-radius:3px;"
        )
        lbl_puts = QLabel("PUTS  ▶")
        lbl_puts.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lbl_puts.setStyleSheet(
            "background:#8b1a1a;color:white;font-weight:bold;"
            "padding:2px 8px;border-radius:3px;"
        )
        header_bar.addWidget(lbl_calls, stretch=1)
        header_bar.addWidget(lbl_puts, stretch=1)
        layout.addLayout(header_bar)

        layout.addWidget(self._table, stretch=1)

        # ── Leg Cart ──────────────────────────────────────────────────────
        cart_box = QGroupBox("Leg Cart  (click Bid to SELL / Ask to BUY in table above)")
        cart_box.setMaximumHeight(140)
        cart_v = QVBoxLayout(cart_box)

        self._tbl_cart = QTableWidget(0, 5)
        self._tbl_cart.setHorizontalHeaderLabels(["Action", "Symbol", "Strike", "Right", "Expiry"])
        self._tbl_cart.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._tbl_cart.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tbl_cart.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._tbl_cart.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._tbl_cart.setMaximumHeight(80)
        cart_v.addWidget(self._tbl_cart)

        cart_btn_row = QHBoxLayout()
        self._btn_cart_remove = QPushButton("🗑 Remove")
        self._btn_cart_remove.setEnabled(False)
        cart_btn_row.addWidget(self._btn_cart_remove)
        self._btn_cart_clear = QPushButton("🧹 Clear")
        self._btn_cart_clear.setEnabled(False)
        cart_btn_row.addWidget(self._btn_cart_clear)
        cart_btn_row.addStretch()
        self._btn_cart_send = QPushButton("📋 Send Combo to Order Entry")
        self._btn_cart_send.setStyleSheet("background:#3498db;color:white;padding:5px;")
        self._btn_cart_send.setEnabled(False)
        cart_btn_row.addWidget(self._btn_cart_send)
        cart_v.addLayout(cart_btn_row)
        layout.addWidget(cart_box)

        # Start with action buttons disabled until connected
        self._btn_fetch.setEnabled(False)
        self._btn_clear_reload.setEnabled(False)

    def _connect_signals(self) -> None:
        self._btn_fetch.clicked.connect(self._on_fetch)
        self._btn_clear_reload.clicked.connect(self._on_clear_reload)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.clicked.connect(self._on_click)  # single-click for leg cart
        self._engine.chain_ready.connect(self._on_chain_ready)
        self._engine.connected.connect(self._on_connected)
        self._engine.disconnected.connect(self._on_disconnected)
        self._engine.positions_updated.connect(self._on_positions_loaded)
        self._cmb_sec_type.currentTextChanged.connect(self._on_sec_type_changed)
        self._cmb_underlying.currentTextChanged.connect(self._on_underlying_changed)
        # Cart buttons
        self._btn_cart_remove.clicked.connect(self._on_cart_remove)
        self._btn_cart_clear.clicked.connect(self._on_cart_clear)
        self._btn_cart_send.clicked.connect(self._on_cart_send)

    @Slot(list)
    def _on_positions_loaded(self, _rows: list) -> None:
        """Supplement the expiry list with expiries found in live positions."""
        try:
            underlying = self._cmb_underlying.currentText()
            pos_expiries = self._engine.get_position_expiries(underlying)
            if not pos_expiries:
                return
            current_items = [self._cmb_expiry.itemText(i) for i in range(self._cmb_expiry.count())]
            current_text = self._cmb_expiry.currentText()
            new_expiries = sorted(set(current_items) | set(pos_expiries))
            if new_expiries == current_items:
                return  # nothing to add
            self._cmb_expiry.blockSignals(True)
            self._cmb_expiry.clear()
            self._cmb_expiry.addItems(new_expiries[:30])
            # Restore selection
            idx = self._cmb_expiry.findText(current_text)
            if idx >= 0:
                self._cmb_expiry.setCurrentIndex(idx)
            self._cmb_expiry.blockSignals(False)
        except RuntimeError:
            pass  # widget destroyed

    @Slot()
    def _on_connected(self) -> None:
        self._btn_fetch.setEnabled(True)
        self._btn_clear_reload.setEnabled(True)
        # Auto-load expiries for current underlying
        self._load_expiries()

    @Slot()
    def _on_disconnected(self) -> None:
        self._btn_fetch.setEnabled(False)
        self._btn_clear_reload.setEnabled(False)
        self._cmb_expiry.clear()
        self._stream_timer.stop()

    @Slot()
    def _on_clear_reload(self) -> None:
        """Cancel all streaming subscriptions, clear the model, and re-fetch."""
        self._stream_timer.stop()
        try:
            self._engine.cancel_chain_streaming()
        except Exception:
            pass
        self._model.set_data([])
        self._lbl_status.setText("Reloading…")
        self._on_fetch()

    def _load_expiries(self) -> None:
        """Fetch available expiries from IB and populate the dropdown."""
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_load_expiries())

    async def _async_load_expiries(self) -> None:
        try:
            underlying = self._cmb_underlying.currentText()
            sec_type = self._cmb_sec_type.currentText()
            exchange = self._cmb_exchange.currentText()
            expiries = await self._engine.get_available_expiries(
                underlying, sec_type=sec_type, exchange=exchange,
            )
            # Supplement with expiries found in the user's live positions
            # (e.g. weekly ES series: EW1, E1D, E2D that reqSecDefOptParams misses)
            pos_expiries = self._engine.get_position_expiries(underlying)
            all_expiries = sorted(set(expiries) | set(pos_expiries))
            try:
                self._cmb_expiry.clear()
                self._cmb_expiry.addItems(all_expiries[:30])  # limit to 30 nearest
                if all_expiries:
                    self._cmb_expiry.setCurrentIndex(0)
                    # Auto-fetch chain for the first expiry immediately
                    self._on_fetch()
            except RuntimeError:
                return  # Widget deleted during shutdown
        except Exception as exc:
            try:
                self._lbl_status.setText(f"Expiry load failed: {exc}")
            except RuntimeError:
                pass  # Widget deleted during shutdown

    # ── slots ─────────────────────────────────────────────────────────────

    @Slot()
    def _on_fetch(self) -> None:
        self._lbl_status.setText("Fetching…")
        self._btn_fetch.setEnabled(False)
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_fetch())

    async def _async_fetch(self) -> None:
        try:
            underlying = self._cmb_underlying.currentText()
            sec_type = self._cmb_sec_type.currentText()
            exchange = self._cmb_exchange.currentText()
            expiry_text = self._cmb_expiry.currentText().strip()

            expiry = None
            if expiry_text:
                try:
                    expiry = date(int(expiry_text[:4]), int(expiry_text[4:6]), int(expiry_text[6:8]))
                except (ValueError, IndexError):
                    pass

            # Remember params for streaming refresh
            self._last_chain_params = (underlying, expiry, sec_type, exchange)

            rows = await self._engine.get_chain(
                underlying, expiry=expiry, sec_type=sec_type, exchange=exchange,
            )
            try:
                self._lbl_status.setText(f"✅ {len(rows)} contracts")
                # Start live price refresh when chain is loaded
                if rows and not self._stream_timer.isActive():
                    self._stream_timer.start()
            except RuntimeError:
                return  # Widget deleted during shutdown
        except Exception as exc:
            try:
                self._lbl_status.setText(f"❌ {exc}")
            except RuntimeError:
                return  # Widget deleted during shutdown
        finally:
            try:
                self._btn_fetch.setEnabled(True)
            except RuntimeError:
                pass  # Widget deleted during shutdown

    @Slot(list)
    def _on_chain_ready(self, rows: list) -> None:
        self._model.set_data(rows)

    @Slot()
    def _on_stream_tick(self) -> None:
        """Periodic live price refresh — re-fetches prices for currently displayed chain."""
        if not self._last_chain_params or not self._engine.is_connected:
            return
        underlying, expiry, sec_type, exchange = self._last_chain_params
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_stream_refresh(underlying, expiry, sec_type, exchange))

    async def _async_stream_refresh(
        self, underlying: str, expiry, sec_type: str, exchange: str
    ) -> None:
        """Refresh prices for the visible chain.

        If the engine already has live streaming tickers for the current chain
        (populated during the initial fetch), we read directly from those tickers
        — no new IB market-data requests.  This is efficient and respects IB’s
        concurrent-subscription limits.

        Falls back to a full re-fetch only when no live tickers are cached.
        """
        try:
            if self._engine._chain_tickers:
                # Fast path: read from already-live tickers (no IB request needed)
                current_rows = self._model.get_all_rows()
                if current_rows:
                    updated = self._engine.read_chain_from_live_tickers(current_rows)
                    if updated:
                        try:
                            self._model.set_data(updated)
                        except RuntimeError:
                            pass  # Widget deleted
            else:
                # No live tickers — do a full re-fetch (also starts a new live stream)
                await self._engine.get_chain(
                    underlying,
                    expiry=expiry,
                    sec_type=sec_type,
                    exchange=exchange,
                    force_refresh=True,
                )
        except Exception:
            pass  # silent — streaming failures are non-fatal

    @Slot(QModelIndex)
    def _on_double_click(self, index: QModelIndex) -> None:
        """Double-click on non-bid/ask column: emit ChainRow for single-leg order entry.

        Bid/Ask columns are reserved for single-click leg-cart staging.
        """
        col = index.column()
        # Skip bid/ask columns — those belong to single-click cart actions
        if col in (_COL_CALL_BID, _COL_CALL_ASK, _COL_PUT_ASK, _COL_PUT_BID):
            return
        row_idx = index.row()
        right = "C" if col < _N_CALL_COLS else "P"
        chain_row = self._model.get_chain_row_at(row_idx, right)
        if chain_row:
            self.chain_row_selected.emit(chain_row)

    @Slot(QModelIndex)
    def _on_click(self, index: QModelIndex) -> None:
        """Single-click on Bid or Ask column → add leg directly to order entry.

        Call Bid col (0)  → SELL Call
        Call Ask col (1)  → BUY  Call
        Put  Ask col (15) → BUY  Put
        Put  Bid col (16) → SELL Put

        Emits ``leg_clicked`` immediately so the leg lands in Order Entry without
        requiring the user to press any extra button.
        """
        col = index.column()
        row_idx = index.row()

        # Map column to (side, action)
        if col == _COL_CALL_BID:
            right, action = "C", "SELL"
        elif col == _COL_CALL_ASK:
            right, action = "C", "BUY"
        elif col == _COL_PUT_ASK:
            right, action = "P", "BUY"
        elif col == _COL_PUT_BID:
            right, action = "P", "SELL"
        else:
            return  # non-price column — ignore

        chain_row = self._model.get_chain_row_at(row_idx, right)
        if chain_row is None:
            self._lbl_status.setText("⚠ No option data for this strike/side")
            return

        # 1) Emit immediately — order entry appends this leg right away
        self.leg_clicked.emit(chain_row, action)

        # 2) Also accumulate in cart for combo building
        self._cart.append((chain_row, action))
        self._update_cart()
        side_label = "Call" if right == "C" else "Put"
        self._lbl_status.setText(
            f"✅ {action} {side_label} {chain_row.strike:.0f} → Order Entry  "
            f"({len(self._cart)} leg(s) staged)"
        )

    def _update_cart(self) -> None:
        """Refresh the cart table widget."""
        self._tbl_cart.setRowCount(len(self._cart))
        for i, (cr, action) in enumerate(self._cart):
            cells = [
                action,
                cr.underlying,
                f"{cr.strike:.0f}",
                {"C": "Call", "P": "Put"}.get(cr.right, cr.right),
                (cr.expiry or "")[:8],
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 0:
                    item.setForeground(Qt.GlobalColor.green if action == "BUY" else Qt.GlobalColor.red)
                self._tbl_cart.setItem(i, col, item)
        has = bool(self._cart)
        self._btn_cart_remove.setEnabled(has)
        self._btn_cart_clear.setEnabled(has)
        self._btn_cart_send.setEnabled(has)

    @Slot()
    def _on_cart_remove(self) -> None:
        row = self._tbl_cart.currentRow()
        if 0 <= row < len(self._cart):
            self._cart.pop(row)
            self._update_cart()

    @Slot()
    def _on_cart_clear(self) -> None:
        self._cart.clear()
        self._update_cart()

    @Slot()
    def _on_cart_send(self) -> None:
        """Convert cart entries to leg dicts and emit leg_cart_ready."""
        legs: list[dict] = []
        for cr, action in self._cart:
            sec_type = "FOP" if cr.underlying in ("ES","MES","NQ","MNQ","RTY","YM","MYM","M2K") else "OPT"
            exchange = "CME" if sec_type == "FOP" else "SMART"
            expiry = (cr.expiry or "")[:8]
            legs.append({
                "symbol": cr.underlying,
                "sec_type": sec_type,
                "exchange": exchange,
                "action": action,
                "qty": 1,
                "strike": cr.strike,
                "right": cr.right,
                "expiry": expiry,
                "conid": cr.conid or 0,
            })
        if legs:
            self.leg_cart_ready.emit(legs)
            # Clear cart after sending
            self._cart.clear()
            self._update_cart()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._last_chain_params and self._engine.is_connected:
            self._stream_timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._stream_timer.stop()
        # Release live IB market-data subscriptions when the tab is not visible
        self._engine.cancel_chain_streaming()

    @Slot(str)
    def _on_sec_type_changed(self, sec_type: str) -> None:
        if sec_type == "FOP":
            self._cmb_exchange.setCurrentText("CME")
        else:
            self._cmb_exchange.setCurrentText("SMART")

    @Slot(str)
    def _on_underlying_changed(self, underlying: str) -> None:
        if underlying in ("ES", "MES", "NQ", "MNQ"):
            self._cmb_sec_type.setCurrentText("FOP")
        else:
            self._cmb_sec_type.setCurrentText("OPT")
        # Reload expiries for new underlying
        if self._engine.is_connected:
            self._load_expiries()
