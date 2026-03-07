"""desktop/ui/market_tab.py — Market Data & Quotes tab.

Shows:
  1. Quick quote lookup (symbol → snapshot)
  2. Watchlist of monitored symbols with live prices
  3. Market data snapshot cards (SPX, ES, VIX…)
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QLineEdit, QPushButton, QComboBox, QTableView, QListWidget,
    QHeaderView, QAbstractItemView,
)
from PySide6.QtCore import Qt, Slot, QAbstractTableModel, QModelIndex, QTimer

from desktop.models.favorites import FavoriteSymbol, FavoritesStore

if TYPE_CHECKING:
    from desktop.engine.ib_engine import IBEngine, MarketSnapshot


# ── Watchlist Model ───────────────────────────────────────────────────────

_WATCH_HEADERS = ["Symbol", "Last", "Bid", "Ask", "High", "Low", "Close", "Volume", "Updated"]


class WatchlistModel(QAbstractTableModel):
    """Table model for the market watchlist."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[MarketSnapshot] = []

    def upsert(self, snap: MarketSnapshot) -> None:
        """Add or update a snapshot in the watchlist."""
        for i, existing in enumerate(self._rows):
            if existing.symbol == snap.symbol:
                self._rows[i] = snap
                self.dataChanged.emit(self.index(i, 0), self.index(i, len(_WATCH_HEADERS) - 1))
                return
        # New symbol
        self.beginInsertRows(QModelIndex(), len(self._rows), len(self._rows))
        self._rows.append(snap)
        self.endInsertRows()

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_WATCH_HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _WATCH_HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        s = self._rows[index.row()]
        col = index.column()
        match col:
            case 0: return s.symbol
            case 1: return f"${s.last:,.2f}" if s.last else "—"
            case 2: return f"${s.bid:,.2f}" if s.bid else "—"
            case 3: return f"${s.ask:,.2f}" if s.ask else "—"
            case 4: return f"${s.high:,.2f}" if s.high else "—"
            case 5: return f"${s.low:,.2f}" if s.low else "—"
            case 6: return f"${s.close:,.2f}" if s.close else "—"
            case 7: return f"{s.volume:,}" if s.volume else "—"
            case 8: return s.timestamp[11:19] if s.timestamp else "—"
            case _: return ""


# ── Market Tab Widget ─────────────────────────────────────────────────────


class MarketTab(QWidget):
    """Market data tab: watchlist + quote lookup."""

    # Default symbols to track: (symbol, sec_type, exchange)
    DEFAULT_WATCHLIST = [
        ("ES", "FUT", "CME"),
        ("SPY", "STK", "SMART"),
        ("QQQ", "STK", "SMART"),
        ("IWM", "STK", "SMART"),
    ]

    def __init__(self, engine: IBEngine, parent=None, favorites_store: FavoritesStore | None = None):
        super().__init__(parent)
        self._engine = engine
        self._favorites_store = favorites_store or FavoritesStore()
        self._favorites: list[FavoriteSymbol] = []
        self._favorite_lookup: set[FavoriteSymbol] = set()
        self._refresh_in_flight = False
        self._setup_ui()
        self._load_persisted_favorites()
        self._connect_signals()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1_000)  # 1s
        self._refresh_timer.timeout.connect(self._on_refresh_favorites)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── Quote Lookup ──────────────────────────────────────────────────
        lookup_box = QGroupBox("Quick Quote")
        lookup_layout = QHBoxLayout(lookup_box)

        lookup_layout.addWidget(QLabel("Symbol:"))
        self._txt_symbol = QLineEdit()
        self._txt_symbol.setPlaceholderText("SPY, ES, QQQ…")
        self._txt_symbol.setMaximumWidth(150)
        lookup_layout.addWidget(self._txt_symbol)

        lookup_layout.addWidget(QLabel("Type:"))
        self._cmb_sec_type = QComboBox()
        self._cmb_sec_type.addItems(["STK", "FUT"])
        lookup_layout.addWidget(self._cmb_sec_type)

        lookup_layout.addWidget(QLabel("Exchange:"))
        self._cmb_exchange = QComboBox()
        self._cmb_exchange.addItems(["SMART", "CME", "CBOE", "GLOBEX"])
        lookup_layout.addWidget(self._cmb_exchange)

        self._btn_quote = QPushButton("📊 Get Quote")
        self._btn_quote.setFixedHeight(30)
        lookup_layout.addWidget(self._btn_quote)

        self._btn_add_default = QPushButton("➕ Load Defaults")
        self._btn_add_default.setFixedHeight(30)
        self._btn_add_default.setToolTip("Load SPY, QQQ, IWM quotes")
        lookup_layout.addWidget(self._btn_add_default)

        self._btn_add_favorite = QPushButton("⭐ Add Favorite")
        self._btn_add_favorite.setFixedHeight(30)
        lookup_layout.addWidget(self._btn_add_favorite)

        self._btn_refresh_favorites = QPushButton("🔄 Refresh Favorites")
        self._btn_refresh_favorites.setFixedHeight(30)
        lookup_layout.addWidget(self._btn_refresh_favorites)

        lookup_layout.addStretch()
        self._lbl_status = QLabel("Ready")
        self._lbl_status.setStyleSheet("color: #888;")
        lookup_layout.addWidget(self._lbl_status)

        layout.addWidget(lookup_box)

        # ── Quote Result ──────────────────────────────────────────────────
        self._lbl_result = QLabel("")
        self._lbl_result.setWordWrap(True)
        self._lbl_result.setStyleSheet("padding: 8px; font-size: 13px;")
        layout.addWidget(self._lbl_result)

        favorites_box = QGroupBox("Favorites")
        fav_layout = QVBoxLayout(favorites_box)
        self._lst_favorites = QListWidget()
        self._lst_favorites.setMaximumHeight(120)
        fav_layout.addWidget(self._lst_favorites)
        fav_actions = QHBoxLayout()
        self._btn_remove_favorite = QPushButton("🗑 Remove Selected")
        self._btn_remove_favorite.setFixedHeight(28)
        fav_actions.addWidget(self._btn_remove_favorite)
        fav_actions.addStretch()
        fav_layout.addLayout(fav_actions)
        layout.addWidget(favorites_box)

        # ── Watchlist Table ───────────────────────────────────────────────
        self._model = WatchlistModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setSortingEnabled(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setVisible(False)

        layout.addWidget(self._table, stretch=1)

        # Start with action buttons disabled until connected
        self._btn_quote.setEnabled(False)
        self._btn_add_default.setEnabled(False)
        self._btn_add_favorite.setEnabled(False)
        self._btn_refresh_favorites.setEnabled(False)
        self._btn_remove_favorite.setEnabled(False)

    def _load_persisted_favorites(self) -> None:
        for favorite in self._favorites_store.load():
            self._favorite_lookup.add(favorite)
            self._favorites.append(favorite)
            self._lst_favorites.addItem(self._favorite_label(favorite))

    def _connect_signals(self) -> None:
        self._btn_quote.clicked.connect(self._on_get_quote)
        self._btn_add_default.clicked.connect(self._on_load_defaults)
        self._btn_add_favorite.clicked.connect(self._on_add_favorite)
        self._btn_refresh_favorites.clicked.connect(self._on_refresh_favorites)
        self._btn_remove_favorite.clicked.connect(self._on_remove_favorite)
        self._txt_symbol.returnPressed.connect(self._on_get_quote)
        self._engine.market_snapshot.connect(self._on_market_snapshot)
        self._engine.connected.connect(self._on_connected)
        self._engine.disconnected.connect(self._on_disconnected)

        # Auto-set exchange when symbol changes
        self._cmb_sec_type.currentTextChanged.connect(self._on_sec_type_changed)

    @Slot()
    def _on_connected(self) -> None:
        self._btn_quote.setEnabled(True)
        self._btn_add_default.setEnabled(True)
        self._btn_add_favorite.setEnabled(True)
        self._btn_refresh_favorites.setEnabled(True)
        self._btn_remove_favorite.setEnabled(True)
        self._sync_refresh_timer_state()

    @Slot()
    def _on_disconnected(self) -> None:
        self._btn_quote.setEnabled(False)
        self._btn_add_default.setEnabled(False)
        self._btn_add_favorite.setEnabled(False)
        self._btn_refresh_favorites.setEnabled(False)
        self._btn_remove_favorite.setEnabled(False)
        self._refresh_timer.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_refresh_timer_state()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._sync_refresh_timer_state()

    def _sync_refresh_timer_state(self) -> None:
        should_run = self.isVisible() and self._engine.is_connected and bool(self._favorites)
        if should_run:
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()
        else:
            self._refresh_timer.stop()

    @Slot(str)
    def _on_sec_type_changed(self, sec_type: str) -> None:
        if sec_type == "FUT":
            self._cmb_exchange.setCurrentText("CME")
        else:
            self._cmb_exchange.setCurrentText("SMART")

    @Slot()
    def _on_get_quote(self) -> None:
        symbol = self._txt_symbol.text().strip().upper()
        if not symbol:
            return
        self._lbl_status.setText(f"Fetching {symbol}…")
        self._btn_quote.setEnabled(False)
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_get_quote(
            symbol,
            self._cmb_sec_type.currentText(),
            self._cmb_exchange.currentText(),
        ))

    async def _async_get_quote(self, symbol: str, sec_type: str, exchange: str) -> None:
        try:
            snap = await self._engine.get_market_snapshot(symbol, sec_type, exchange)
            self._lbl_result.setText(
                f"<b>{snap.symbol}</b> — "
                f"Last: <b>${snap.last:,.2f}</b> | "
                f"Bid: ${snap.bid:,.2f} | Ask: ${snap.ask:,.2f} | "
                f"Vol: {snap.volume:,}"
                if snap.last else
                f"<b>{snap.symbol}</b> — No data available (market may be closed)"
            )
            self._lbl_result.setStyleSheet("background: #d4edda; color: #155724; padding: 8px;")
            self._lbl_status.setText(f"✅ {symbol}")
        except Exception as exc:
            self._lbl_result.setText(f"❌ {exc}")
            self._lbl_result.setStyleSheet("background: #f8d7da; color: #721c24; padding: 8px;")
            self._lbl_status.setText(f"❌ {symbol} failed")
        finally:
            self._btn_quote.setEnabled(True)

    @Slot()
    def _on_load_defaults(self) -> None:
        self._lbl_status.setText("Loading default watchlist…")
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_load_defaults())

    async def _async_load_defaults(self) -> None:
        for sym, sec_type, exchange in self.DEFAULT_WATCHLIST:
            self._add_favorite(sym, sec_type, exchange)
            try:
                await self._engine.get_market_snapshot(sym, sec_type, exchange)
            except Exception as exc:
                self._lbl_status.setText(f"⚠ {sym}: {exc}")
        self._lbl_status.setText(f"✅ Loaded {len(self.DEFAULT_WATCHLIST)} symbols")
        self._sync_refresh_timer_state()

    @Slot()
    def _on_add_favorite(self) -> None:
        symbol = self._txt_symbol.text().strip().upper()
        if not symbol:
            return
        sec_type = self._cmb_sec_type.currentText()
        exchange = self._cmb_exchange.currentText()
        if self._add_favorite(symbol, sec_type, exchange):
            self._lbl_status.setText(f"⭐ Added {symbol} {sec_type}@{exchange}")
        else:
            self._lbl_status.setText(f"{symbol} already in favorites")
        self._sync_refresh_timer_state()

    @Slot()
    def _on_remove_favorite(self) -> None:
        current = self._lst_favorites.currentItem()
        if current is None:
            return
        item_text = current.text()
        for i, entry in enumerate(self._favorites):
            if self._favorite_label(entry) == item_text:
                self._favorite_lookup.discard(entry)
                del self._favorites[i]
                self._lst_favorites.takeItem(self._lst_favorites.row(current))
                self._favorites_store.save(self._favorites)
                self._lbl_status.setText(f"Removed {item_text}")
                break
        self._sync_refresh_timer_state()

    @Slot()
    def _on_refresh_favorites(self) -> None:
        if not self._favorites or self._refresh_in_flight:
            return
        if not self._engine.is_connected:
            return
        loop = asyncio.get_event_loop()
        loop.create_task(self._async_refresh_favorites())

    async def _async_refresh_favorites(self) -> None:
        self._refresh_in_flight = True
        try:
            for favorite in list(self._favorites):
                try:
                    await self._engine.get_market_snapshot(
                        favorite.symbol,
                        favorite.sec_type,
                        favorite.exchange,
                    )
                except Exception:
                    continue
            self._lbl_status.setText(f"✅ Refreshed {len(self._favorites)} favorites")
        finally:
            self._refresh_in_flight = False

    def _add_favorite(self, symbol: str, sec_type: str, exchange: str) -> bool:
        entry = FavoriteSymbol(symbol=symbol.upper(), sec_type=sec_type, exchange=exchange)
        if entry in self._favorite_lookup:
            return False
        self._favorite_lookup.add(entry)
        self._favorites.append(entry)
        self._lst_favorites.addItem(self._favorite_label(entry))
        self._favorites_store.save(self._favorites)
        return True

    @staticmethod
    def _favorite_label(entry: FavoriteSymbol) -> str:
        return f"{entry.symbol} ({entry.sec_type}@{entry.exchange})"

    @Slot(object)
    def _on_market_snapshot(self, snap) -> None:
        self._model.upsert(snap)
