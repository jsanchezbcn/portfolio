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
import logging
import math
from datetime import date
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QComboBox, QTableView, QHeaderView, QAbstractItemView,
)
from PySide6.QtCore import Qt, Signal, Slot, QModelIndex, QTimer

from desktop.models.table_models import ChainTableModel

if TYPE_CHECKING:
    from desktop.engine.ib_engine import IBEngine, ChainRow

logger = logging.getLogger(__name__)

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

    def __init__(self, engine: IBEngine, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._last_chain_params: tuple | None = None  # (underlying, expiry, sec_type, exchange)
        self._loading_expiries = False
        self._full_chain_rows: list = []
        self._underlying_price: float | None = None
        self._setup_ui()
        self._connect_signals()
        # ── Live price refresh timer (visible strikes only) ──
        self._stream_timer = QTimer(self)
        self._stream_timer.setInterval(5_000)  # 5s streaming refresh
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

        toolbar.addWidget(QLabel("Range:"))
        self._cmb_sd_range = QComboBox()
        self._cmb_sd_range.addItems(["±1σ", "±2σ", "±3σ"])
        self._cmb_sd_range.setCurrentIndex(1)
        toolbar.addWidget(self._cmb_sd_range)

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
        lbl_strike = QLabel("STRIKE")
        lbl_strike.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_strike.setStyleSheet(
            "background:#8b0000;color:white;font-weight:bold;"
            "padding:2px 8px;border-radius:3px;"
        )
        header_bar.addWidget(lbl_calls, stretch=8)
        header_bar.addWidget(lbl_strike, stretch=1)
        header_bar.addWidget(lbl_puts, stretch=8)
        layout.addLayout(header_bar)

        layout.addWidget(self._table, stretch=1)

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
        self._cmb_expiry.currentTextChanged.connect(self._on_expiry_changed)
        self._cmb_sd_range.currentTextChanged.connect(self._on_sd_range_changed)

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

    @Slot(str)
    def _on_expiry_changed(self, _expiry_text: str) -> None:
        self._clear_chain_view("Loading new expiry…")
        if self._loading_expiries or not self._engine.is_connected:
            return
        if self._cmb_expiry.currentText().strip():
            self._on_fetch()

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
            
            # Format with DTE: "20260320 (11 days)"
            today = date.today()
            formatted_expiries = []
            for exp_str in all_expiries:
                try:
                    exp_date = date(int(exp_str[:4]), int(exp_str[4:6]), int(exp_str[6:8]))
                    dte = (exp_date - today).days
                    formatted_expiries.append(f"{exp_str} ({dte} days)")
                except (ValueError, IndexError):
                    formatted_expiries.append(exp_str)
            
            try:
                self._loading_expiries = True
                self._cmb_expiry.blockSignals(True)
                self._cmb_expiry.clear()
                # Show ALL available expirations with DTE (no limit) — user can scroll if needed
                self._cmb_expiry.addItems(formatted_expiries)
                
                # Show data source indicator
                source_indicator = ""
                if hasattr(self._engine, '_last_expiry_source'):
                    if self._engine._last_expiry_source == "database":
                        source_indicator = " [CACHED]"
                    elif self._engine._last_expiry_source == "memory":
                        source_indicator = " [MEMORY]"
                
                logger.debug("Loaded %d expirations for %s%s: %s", len(all_expiries), underlying, source_indicator,
                           ", ".join(all_expiries[:10]) + ("..." if len(all_expiries) > 10 else ""))
                if formatted_expiries:
                    self._cmb_expiry.setCurrentIndex(0)
                self._cmb_expiry.blockSignals(False)
                if formatted_expiries:
                    # Extract the raw expiry string (before the space) for the changed handler
                    raw_expiry = formatted_expiries[0].split()[0] if formatted_expiries else ""
                    self._on_expiry_changed(raw_expiry)
            except RuntimeError:
                return  # Widget deleted during shutdown
            finally:
                self._loading_expiries = False
                try:
                    self._cmb_expiry.blockSignals(False)
                except RuntimeError:
                    pass
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
                underlying, expiry=expiry, sec_type=sec_type, exchange=exchange, max_strikes=200,
            )

            # Fetch underlying price FIRST (before filtering) and set on model IMMEDIATELY
            self._underlying_price = self._engine.last_price(underlying)
            if self._underlying_price is None:
                try:
                    snap = await self._engine.get_market_snapshot(underlying, sec_type="FUT" if sec_type == "FOP" else "STK", exchange=exchange)
                    if snap:
                        self._underlying_price = snap.last or snap.close or snap.bid or snap.ask
                except Exception as fetch_exc:
                    logger.warning("Failed to fetch market snapshot for %s: %s", underlying, fetch_exc)
                    self._underlying_price = None
            
            # Log underlying price fetch for diagnostics
            if self._underlying_price is not None:
                logger.info("Underlying price fetched: %s = %.2f", underlying, self._underlying_price)
            else:
                logger.warning("Failed to fetch underlying price for %s (ATM highlighting disabled)", underlying)
            
            # Set underlying price on model IMMEDIATELY before filtering
            self._model.set_underlying_price(self._underlying_price)
            
            try:
                # Add cache indicator if using database-cached expirations
                cache_indicator = ""
                if hasattr(self._engine, '_last_expiry_source') and self._engine._last_expiry_source == "database":
                    cache_indicator = " [CACHED DATA]"
                
                if self._underlying_price is not None:
                    self._lbl_status.setText(f"✅ {len(rows)} contracts · Underlying {underlying}: {self._underlying_price:,.2f}{cache_indicator}")
                else:
                    self._lbl_status.setText(f"✅ {len(rows)} contracts{cache_indicator}")
                # Start live price refresh when chain is loaded
                if rows and not self._stream_timer.isActive():
                    self._stream_timer.start()
            except RuntimeError:
                return  # Widget deleted during shutdown
        except Exception as exc:
            try:
                self._lbl_status.setText(f"❌ {exc}")
                logger.error("_async_fetch failed: %s", exc, exc_info=True)
            except RuntimeError:
                return  # Widget deleted during shutdown
        finally:
            try:
                self._btn_fetch.setEnabled(True)
            except RuntimeError:
                pass  # Widget deleted during shutdown

    @Slot(list)
    def _on_chain_ready(self, rows: list) -> None:
        self._full_chain_rows = list(rows)
        self._apply_chain_filters()

    def _selected_sigma_band(self) -> int:
        text = self._cmb_sd_range.currentText().strip()
        return 1 if text.startswith("±1") else 2 if text.startswith("±2") else 3

    def _apply_chain_filters(self) -> None:
        # Extract raw expiry (strip DTE if present)
        current_expiry_raw = self._cmb_expiry.currentText().strip().split()[0] if self._cmb_expiry.currentText() else ""
        rows = list(self._full_chain_rows)
        filtered_rows = [
            row for row in rows
            if not current_expiry_raw or str(getattr(row, "expiry", "")).startswith(current_expiry_raw)
        ]

        sigma_band = self._selected_sigma_band()
        expiry_text = current_expiry_raw
        if filtered_rows and self._underlying_price and expiry_text and len(expiry_text) >= 8:
            try:
                expiry_date = date(int(expiry_text[:4]), int(expiry_text[4:6]), int(expiry_text[6:8]))
                dte = max(1, (expiry_date - date.today()).days)
                atm_iv = self._estimate_atm_iv(filtered_rows, float(self._underlying_price))
                if atm_iv and atm_iv > 0:
                    one_sigma_move = float(self._underlying_price) * float(atm_iv) * math.sqrt(dte / 365.0)
                    move = one_sigma_move * float(sigma_band)
                    low = float(self._underlying_price) - move
                    high = float(self._underlying_price) + move
                    filtered_rows = [r for r in filtered_rows if low <= float(getattr(r, "strike", 0.0)) <= high]
                    logger.debug("Applied ±%dσ filter: %.0f-%.0f (underlying=%.2f, IV=%.2f%%, DTE=%d)", 
                               sigma_band, low, high, self._underlying_price, atm_iv * 100, dte)
            except Exception as exc:
                logger.warning("_apply_chain_filters failed: %s", exc)

        # Model already has underlying_price set in _async_fetch, just update data
        self._model.set_data(filtered_rows)
        
        # Auto-scroll to center the underlying price row
        self._scroll_to_underlying()

    def _scroll_to_underlying(self) -> None:
        """Scroll the table to center on the nearest underlying strike row."""
        if self._model._underlying_row_idx is None:
            return
        
        underlying_row = self._model._underlying_row_idx
        row_height = self._table.rowHeight(underlying_row) if underlying_row < self._model.rowCount() else 30
        
        # Calculate scroll position to center the underlying row in the view
        # Get the viewport height and scroll to place the row approximately in the middle
        viewport_height = self._table.viewport().height()
        rows_to_center = viewport_height // (2 * row_height)
        
        # Scroll to a row that places the underlying row roughly in the middle
        target_row = max(0, underlying_row - rows_to_center)
        index = self._model.index(target_row, 0)
        self._table.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtTop)
        
        logger.debug("Scrolled chain table to underlying row %d (strike at idx %d)", target_row, underlying_row)

    def _estimate_atm_iv(self, rows: list, under_price: float) -> float | None:
        iv_samples: list[float] = []
        try:
            sorted_rows = sorted(rows, key=lambda r: abs(float(getattr(r, "strike", 0.0)) - under_price))
        except Exception:
            sorted_rows = rows
        for row in sorted_rows[:12]:
            iv = getattr(row, "iv", None)
            if isinstance(iv, (int, float)) and iv > 0 and iv < 5:
                iv_samples.append(float(iv))
        if not iv_samples:
            return None
        return sum(iv_samples) / len(iv_samples)

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
                        self._full_chain_rows = list(updated)
                        try:
                            self._apply_chain_filters()
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

    def _clear_chain_view(self, status: str) -> None:
        self._stream_timer.stop()
        try:
            self._engine.cancel_chain_streaming()
        except Exception:
            pass
        self._full_chain_rows = []
        self._model.set_data([])
        self._lbl_status.setText(status)

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

        # Emit immediately — order entry appends this leg right away
        self.leg_clicked.emit(chain_row, action)
        side_label = "Call" if right == "C" else "Put"
        self._lbl_status.setText(
            f"✅ {action} {side_label} {chain_row.strike:.0f} → Order Entry"
        )

    @Slot(str)
    def _on_sd_range_changed(self, _text: str) -> None:
        self._apply_chain_filters()

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
