"""desktop/ui/journal_tab.py — Trade Journal tab.

Two sub-tabs:
    1. Journal Notes — PostgreSQL-backed note history for strategy/thesis review
    2. Order Log     — DB-backed order history with status, fills, and rationale
         Covers both executed (FILLED) and pending/cancelled orders so nothing is lost.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import datetime, timezone
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
    QTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QHeaderView,
)

if TYPE_CHECKING:
    from desktop.engine.ib_engine import IBEngine


# ─────────────────────────────────────────────────────────────────────────────
# Journal Notes sub-tab
# ─────────────────────────────────────────────────────────────────────────────

class _StrategyJournalPane(QWidget):
    """Postgres-backed journal notes pane for discretionary strategy context."""

    _HEADERS = [
        "Created", "Title", "Tags", "Body",
    ]

    def __init__(self, engine: "IBEngine | None" = None, journal_store=None, parent=None):
        super().__init__(parent)
        self._engine = engine
        self._store = journal_store or (getattr(engine, "_db", None) if engine is not None else None)
        self._rows: list[dict] = []
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("Search:"))
        self._txt_search = QLineEdit()
        self._txt_search.setPlaceholderText("Title or body…")
        self._txt_search.setMaximumWidth(220)
        toolbar.addWidget(self._txt_search)

        toolbar.addWidget(QLabel("Tag:"))
        self._txt_tag = QLineEdit()
        self._txt_tag.setPlaceholderText("earnings, hedge, thesis…")
        self._txt_tag.setMaximumWidth(180)
        toolbar.addWidget(self._txt_tag)

        self._btn_refresh = QPushButton("🔄 Refresh")
        self._btn_refresh.clicked.connect(self._on_refresh)
        toolbar.addWidget(self._btn_refresh)

        self._btn_save = QPushButton("💾 Save Note")
        self._btn_save.clicked.connect(self._on_save)
        toolbar.addWidget(self._btn_save)

        self._btn_export = QPushButton("⬇ Export CSV")
        self._btn_export.clicked.connect(self._on_export)
        self._btn_export.setEnabled(False)
        toolbar.addWidget(self._btn_export)

        toolbar.addStretch()
        self._lbl_status = QLabel("Ready")
        self._lbl_status.setStyleSheet("color: #888;")
        toolbar.addWidget(self._lbl_status)
        layout.addLayout(toolbar)

        editor = QVBoxLayout()
        self._txt_title = QLineEdit()
        self._txt_title.setPlaceholderText("Note title")
        editor.addWidget(self._txt_title)

        self._txt_tags = QLineEdit()
        self._txt_tags.setPlaceholderText("Comma-separated tags")
        editor.addWidget(self._txt_tags)

        self._txt_body = QTextEdit()
        self._txt_body.setPlaceholderText("Write your thesis, post-mortem, or follow-up note…")
        self._txt_body.setMaximumHeight(120)
        editor.addWidget(self._txt_body)
        layout.addLayout(editor)

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

    @Slot()
    def _on_save(self) -> None:
        asyncio.get_event_loop().create_task(self._async_save_note())

    async def _async_refresh(self) -> None:
        try:
            if self._store is None or not hasattr(self._store, "list_journal_notes"):
                self._lbl_status.setText("⚠️ Journal database not available")
                self._rows = []
                self._render_rows([])
                self._btn_export.setEnabled(False)
                return
            if self._engine is not None and not getattr(self._engine, "_db_ok", True):
                self._lbl_status.setText("⚠️ Database not available")
                self._rows = []
                self._render_rows([])
                self._btn_export.setEnabled(False)
                return

            self._lbl_status.setText("Loading notes…")
            search = self._txt_search.text().strip() or None
            tag = self._txt_tag.text().strip() or None
            account_id = getattr(self._engine, "account_id", None) or getattr(self._engine, "_account_id", None)
            rows = await self._store.list_journal_notes(
                account_id=account_id,
                search=search,
                tag=tag,
                limit=500,
            )
            self._rows = [dict(row) if not isinstance(row, dict) else row for row in rows]
            self._render_rows(self._rows)
            self._btn_export.setEnabled(bool(self._rows))
            self._lbl_status.setText(f"Loaded {len(self._rows)} notes")
        except Exception as exc:
            self._lbl_status.setText(f"❌ {exc}")

    async def _async_save_note(self) -> None:
        title = self._txt_title.text().strip()
        body = self._txt_body.toPlainText().strip()
        tags = [part.strip() for part in self._txt_tags.text().split(",") if part.strip()]
        if not title:
            self._lbl_status.setText("⚠️ Title is required")
            return
        if self._store is None or not hasattr(self._store, "create_journal_note"):
            self._lbl_status.setText("⚠️ Journal database not available")
            return
        if self._engine is not None and not getattr(self._engine, "_db_ok", True):
            self._lbl_status.setText("⚠️ Database not available")
            return
        try:
            account_id = getattr(self._engine, "account_id", None) or getattr(self._engine, "_account_id", None)
            await self._store.create_journal_note(
                account_id=account_id,
                title=title,
                body=body,
                tags=tags,
            )
            self._txt_title.clear()
            self._txt_tags.clear()
            self._txt_body.clear()
            self._lbl_status.setText("Saved journal note")
            await self._async_refresh()
        except Exception as exc:
            self._lbl_status.setText(f"❌ Save failed: {exc}")

    @Slot()
    def _on_export(self) -> None:
        if not self._rows:
            return
        try:
            export_dir = Path(__file__).resolve().parents[2] / "data" / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            fp = export_dir / f"journal_notes_{ts}.csv"
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(self._HEADERS)
            for row in self._rows:
                writer.writerow([
                    str(row.get("created_at") or "")[:19].replace("T", " "),
                    str(row.get("title") or ""),
                    ", ".join(row.get("tags") or []),
                    str(row.get("body") or ""),
                ])
            fp.write_text(buf.getvalue(), encoding="utf-8")
            self._lbl_status.setText(f"Exported {fp.name}")
        except Exception as exc:
            self._lbl_status.setText(f"❌ Export failed: {exc}")

    def _render_rows(self, rows: list[dict]) -> None:
        self._table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            created_at = str(row.get("created_at") or "")[:19].replace("T", " ")
            tags = row.get("tags") or []
            display = [
                created_at,
                str(row.get("title") or ""),
                ", ".join(str(tag) for tag in tags),
                str((row.get("body") or "")[:160]),
            ]
            for j, value in enumerate(display):
                item = QTableWidgetItem(value)
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
                row_values = []
                for col in range(len(self._HEADERS)):
                    item = self._table.item(row, col)
                    row_values.append(item.text() if item is not None else "")
                writer.writerow(row_values)
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
    """Trade journal tab with journal notes and order log sub-tabs."""

    def __init__(self, engine: "IBEngine | None" = None, parent=None):
        super().__init__(parent)
        self._engine = engine
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._sub_tabs = QTabWidget()
        self._strategy_pane = _StrategyJournalPane(engine=engine)
        self._sub_tabs.addTab(self._strategy_pane, "📝 Journal Notes")

        if engine is not None:
            self._order_log_pane = _OrderLogPane(engine)
            self._sub_tabs.addTab(self._order_log_pane, "📋 Order Log")
            # Wire engine's order_filled signal so the log auto-refreshes
            engine.order_filled.connect(self._order_log_pane.on_order_filled)
        else:
            self._order_log_pane = None

        layout.addWidget(self._sub_tabs)
