"""desktop/ui/journal_tab.py — Trade Journal tab.

Two sub-tabs:
  1. Strategy Journal  — LocalStore-backed entries (same as Streamlit journal page)
  2. Order Log         — DB-backed order history with status, fills, and rationale
     Covers both executed (FILLED) and pending/cancelled orders so nothing is lost.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QLineEdit,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QHeaderView,
)

from database.local_store import LocalStore

if TYPE_CHECKING:
    from desktop.engine.ib_engine import IBEngine


# ─────────────────────────────────────────────────────────────────────────────
# Strategy Journal sub-tab (unchanged logic)
# ─────────────────────────────────────────────────────────────────────────────

class _StrategyJournalPane(QWidget):
    """LocalStore-backed strategy journal (unchanged from original JournalTab)."""

    _HEADERS = [
        "Timestamp", "Underlying", "Strategy", "Status",
        "Net Cr/Dr", "VIX", "Regime", "Rationale",
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._store = LocalStore()
        self._rows: list[dict] = []
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Range:"))
        self._cmb_range = QComboBox()
        self._cmb_range.addItems(["Last 7 days", "Last 30 days", "Last 90 days", "All time"])
        toolbar.addWidget(self._cmb_range)

        toolbar.addWidget(QLabel("Instrument:"))
        self._txt_instrument = QLineEdit()
        self._txt_instrument.setPlaceholderText("SPX, SPY, ES…")
        self._txt_instrument.setMaximumWidth(160)
        toolbar.addWidget(self._txt_instrument)

        toolbar.addWidget(QLabel("Regime:"))
        self._cmb_regime = QComboBox()
        self._cmb_regime.addItems(["All", "low_volatility", "neutral_volatility",
                                   "high_volatility", "crisis_mode"])
        toolbar.addWidget(self._cmb_regime)

        self._btn_refresh = QPushButton("🔄 Refresh")
        self._btn_refresh.clicked.connect(self._on_refresh)
        toolbar.addWidget(self._btn_refresh)

        self._btn_export = QPushButton("⬇ Export CSV")
        self._btn_export.clicked.connect(self._on_export)
        self._btn_export.setEnabled(False)
        toolbar.addWidget(self._btn_export)

        toolbar.addStretch()
        self._lbl_status = QLabel("Ready")
        self._lbl_status.setStyleSheet("color: #888;")
        toolbar.addWidget(self._lbl_status)
        layout.addLayout(toolbar)

        self._table = QTableWidget(0, len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._table, stretch=1)

    @Slot()
    def _on_refresh(self) -> None:
        asyncio.get_event_loop().create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        try:
            self._lbl_status.setText("Loading journal…")
            selected = self._cmb_range.currentText()
            start_dt = None
            if selected != "All time":
                days = 7 if "7" in selected else 30 if "30" in selected else 90
                start_dt = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            instrument = self._txt_instrument.text().strip() or None
            regime = self._cmb_regime.currentText()
            regime = None if regime == "All" else regime
            self._rows = await self._store.query_journal(
                start_dt=start_dt, end_dt=None,
                instrument=instrument, regime=regime, limit=500,
            )
            self._render_rows(self._rows)
            self._btn_export.setEnabled(bool(self._rows))
            self._lbl_status.setText(f"Loaded {len(self._rows)} journal rows")
        except Exception as exc:
            self._lbl_status.setText(f"❌ {exc}")

    @Slot()
    def _on_export(self) -> None:
        if not self._rows:
            return
        try:
            csv_data = self._store.export_csv(self._rows)
            export_dir = Path(__file__).resolve().parents[2] / "data" / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            fp = export_dir / f"trade_journal_{ts}.csv"
            fp.write_text(csv_data, encoding="utf-8")
            self._lbl_status.setText(f"Exported {fp.name}")
        except Exception as exc:
            self._lbl_status.setText(f"❌ Export failed: {exc}")

    def _render_rows(self, rows: list[dict]) -> None:
        self._table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            created_at = (row.get("created_at") or "")[:19].replace("T", " ")
            net = row.get("net_debit_credit")
            vix = row.get("vix_at_fill")
            display = [
                created_at,
                str(row.get("underlying") or ""),
                str(row.get("strategy_tag") or "—"),
                str(row.get("status") or ""),
                f"${float(net):+,.2f}" if net is not None else "—",
                f"{float(vix):.1f}" if vix is not None else "—",
                str(row.get("regime") or "—"),
                str((row.get("user_rationale") or "")[:80]),
            ]
            for j, value in enumerate(display):
                item = QTableWidgetItem(value)
                if j in (4, 5):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._table.setItem(i, j, item)


# ─────────────────────────────────────────────────────────────────────────────
# Order Log sub-tab
# ─────────────────────────────────────────────────────────────────────────────

class _OrderLogPane(QWidget):
    """DB-backed order history: shows DRAFT / PENDING / FILLED / CANCELLED / REJECTED."""

    _HEADERS = [
        "Created", "Status", "Side", "Type", "Limit $",
        "Filled $", "Source", "Legs", "Rationale",
    ]

    def __init__(self, engine: "IBEngine", parent=None):
        super().__init__(parent)
        self._engine = engine
        self._rows: list = []
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        toolbar = QHBoxLayout()

        toolbar.addWidget(QLabel("Status:"))
        self._cmb_status = QComboBox()
        self._cmb_status.addItems(["All", "FILLED", "PENDING", "CANCELLED", "REJECTED", "DRAFT"])
        toolbar.addWidget(self._cmb_status)

        self._btn_refresh = QPushButton("🔄 Refresh")
        self._btn_refresh.clicked.connect(self._on_refresh)
        toolbar.addWidget(self._btn_refresh)

        self._btn_export = QPushButton("⬇ Export CSV")
        self._btn_export.clicked.connect(self._on_export)
        self._btn_export.setEnabled(False)
        toolbar.addWidget(self._btn_export)

        toolbar.addStretch()
        self._lbl_status = QLabel("Ready")
        self._lbl_status.setStyleSheet("color: #888;")
        toolbar.addWidget(self._lbl_status)
        layout.addLayout(toolbar)

        self._table = QTableWidget(0, len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.Interactive)
        layout.addWidget(self._table, stretch=1)

    @Slot()
    def _on_refresh(self) -> None:
        asyncio.get_event_loop().create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        self._lbl_status.setText("Loading orders…")
        try:
            db = self._engine._db
            if not self._engine._db_ok:
                self._lbl_status.setText("⚠️ Database not available — no order log")
                return
            status_filter = self._cmb_status.currentText()
            status_arg = None if status_filter == "All" else status_filter
            self._rows = await db.get_orders(
                self._engine._account_id, status=status_arg, limit=500
            )
            self._render_rows(self._rows)
            self._btn_export.setEnabled(bool(self._rows))
            self._lbl_status.setText(f"{len(self._rows)} orders")
        except Exception as exc:
            self._lbl_status.setText(f"❌ {exc}")

    def _render_rows(self, rows) -> None:
        self._table.setRowCount(len(rows))
        _STATUS_COLORS = {
            "FILLED":    "#155724",
            "PENDING":   "#004085",
            "SUBMITTED": "#004085",
            "CANCELLED": "#6c757d",
            "CANCELED":  "#6c757d",
            "REJECTED":  "#721c24",
            "DRAFT":     "#856404",
        }
        _STATUS_BG = {
            "FILLED":    "#d4edda",
            "PENDING":   "#cce5ff",
            "SUBMITTED": "#cce5ff",
            "CANCELLED": "#e2e3e5",
            "CANCELED":  "#e2e3e5",
            "REJECTED":  "#f8d7da",
            "DRAFT":     "#fff3cd",
        }
        for i, row in enumerate(rows):
            created_at = ""
            raw_ts = row.get("created_at") if isinstance(row, dict) else getattr(row, "created_at", None)
            if raw_ts:
                created_at = str(raw_ts)[:19].replace("T", " ")

            def _g(key):
                return row.get(key) if isinstance(row, dict) else getattr(row, key, None)

            status   = str(_g("status") or "")
            side     = str(_g("side") or "")
            otype    = str(_g("order_type") or "")
            lmt      = _g("limit_price")
            filled   = _g("filled_price")
            source   = str(_g("source") or "manual")
            rationale = str(_g("rationale") or "")[:100]
            # Legs: stored as JSON string or list
            legs_raw = _g("legs_json") or _g("legs") or []
            if isinstance(legs_raw, str):
                try:
                    legs_raw = json.loads(legs_raw)
                except Exception:
                    legs_raw = []
            if isinstance(legs_raw, list):
                legs_txt = " / ".join(
                    f"{lg.get('action','?')} {lg.get('qty',1)}×{lg.get('symbol','?')} "
                    f"{lg.get('right','')}{lg.get('strike','')} {lg.get('expiry','')}"
                    for lg in legs_raw
                ).strip(" /") or "—"
            else:
                legs_txt = str(legs_raw)[:60]

            display = [
                created_at,
                status,
                side,
                otype,
                f"${float(lmt):.2f}" if lmt is not None else "—",
                f"${float(filled):.2f}" if filled is not None else "—",
                source,
                legs_txt,
                rationale,
            ]

            fg = _STATUS_COLORS.get(status.upper(), "#212529")
            bg = _STATUS_BG.get(status.upper(), "#ffffff")

            for j, value in enumerate(display):
                item = QTableWidgetItem(value)
                if j in (4, 5):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if j == 1:  # Status column — color-coded
                    item.setForeground(Qt.GlobalColor.white if bg != "#ffffff" else Qt.GlobalColor.black)
                    from PySide6.QtGui import QColor, QBrush
                    item.setBackground(QBrush(QColor(bg)))
                    item.setForeground(QBrush(QColor(fg)))
                self._table.setItem(i, j, item)

    @Slot()
    def _on_export(self) -> None:
        if not self._rows:
            return
        try:
            export_dir = Path(__file__).resolve().parents[2] / "data" / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            fp = export_dir / f"order_log_{ts}.csv"
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(self._HEADERS)
            for row in range(self._table.rowCount()):
                writer.writerow(
                    self._table.item(row, col).text() if self._table.item(row, col) else ""
                    for col in range(len(self._HEADERS))
                )
            fp.write_text(buf.getvalue(), encoding="utf-8")
            self._lbl_status.setText(f"Exported {fp.name}")
        except Exception as exc:
            self._lbl_status.setText(f"❌ Export failed: {exc}")

    def on_order_filled(self, info: dict) -> None:
        """Auto-refresh when the engine reports an order fill."""
        asyncio.get_event_loop().create_task(self._async_refresh())


# ─────────────────────────────────────────────────────────────────────────────
# Public JournalTab (combines both panes)
# ─────────────────────────────────────────────────────────────────────────────

class JournalTab(QWidget):
    """Trade journal tab with two sub-tabs: Strategy Journal and Order Log."""

    def __init__(self, engine: "IBEngine | None" = None, parent=None):
        super().__init__(parent)
        self._engine = engine
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._sub_tabs = QTabWidget()
        self._strategy_pane = _StrategyJournalPane()
        self._sub_tabs.addTab(self._strategy_pane, "📝 Strategy Journal")

        if engine is not None:
            self._order_log_pane = _OrderLogPane(engine)
            self._sub_tabs.addTab(self._order_log_pane, "📋 Order Log")
            # Wire engine's order_filled signal so the log auto-refreshes
            engine.order_filled.connect(self._order_log_pane.on_order_filled)
        else:
            self._order_log_pane = None

        layout.addWidget(self._sub_tabs)
