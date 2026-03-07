"""desktop/models/table_models.py — QAbstractTableModel subclasses for PySide6 views.

Every table in the UI (positions, orders, chain) has a dedicated model that
owns the data and emits change signals.  No raw lists-of-dicts in the view.
"""
from __future__ import annotations

import dataclasses
from collections import defaultdict
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor, QFont


# ── Positions ─────────────────────────────────────────────────────────────

_POS_HEADERS = [
    "Symbol", "Type", "Underlying", "Qty", "Avg Cost",
    "Mkt Price", "Mkt Value", "Unrealized PnL", "Und Price",
    "Strike", "Right", "Expiry",
    "Δ Delta", "Γ Gamma", "Θ Theta", "V Vega", "IV",
    "SPX Δ",
]
# Column indices for the Greeks (used in group row rendering)
_COL_DELTA, _COL_GAMMA, _COL_THETA, _COL_VEGA, _COL_IV, _COL_SPX = 12, 13, 14, 15, 16, 17

# Background colour used for expiry-group header rows
_GROUP_BG  = QColor(30, 50, 80)    # dark blue
_GROUP_FG  = QColor(200, 220, 255)  # light text
_STK_BG    = QColor(45, 45, 45)     # slightly lighter for stocks/futures section
_STK_FG    = QColor(220, 220, 180)  # warm white


@dataclasses.dataclass
class _ExpiryGroupRow:
    """Synthetic summary row inserted before each expiry group in the table."""
    expiry: str
    count: int
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    spx_delta: float | None

class PositionsTableModel(QAbstractTableModel):
    """Model for the Portfolio tab's positions table.

    Display order:
      1. Options/FOPs grouped by expiry (earliest first).
         Each group is preceded by a shaded summary row showing net Greeks.
      2. Stocks and futures appended at the end.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[Any] = []  # mix of PositionRow and _ExpiryGroupRow
        self._sort_metric: str = "none"
        self._sort_desc: bool = True
        self._sort_abs: bool = True

    # ── public ────────────────────────────────────────────────────────────

    def set_data(self, rows: list) -> None:
        """Rebuild display list: options grouped by expiry, then stocks/futures."""
        self.beginResetModel()

        if self._sort_metric != "none":
            self._rows = self._sort_rows(rows)
            self.endResetModel()
            return

        options = [r for r in rows if getattr(r, "sec_type", "") in ("OPT", "FOP")]
        non_options = [r for r in rows if getattr(r, "sec_type", "") not in ("OPT", "FOP")]

        # Group options by expiry
        groups: dict[str, list] = defaultdict(list)
        for r in options:
            groups[r.expiry or "Unknown"].append(r)

        display: list[Any] = []
        for expiry in sorted(groups.keys()):
            grp = groups[expiry]

            def _sum(attr: str, _grp=grp) -> float | None:
                vals = [getattr(r, attr) for r in _grp if getattr(r, attr) is not None]
                return sum(vals) if vals else None

            summary = _ExpiryGroupRow(
                expiry=expiry,
                count=len(grp),
                delta=_sum("delta"),
                gamma=_sum("gamma"),
                theta=_sum("theta"),
                vega=_sum("vega"),
                spx_delta=_sum("spx_delta"),
            )
            display.append(summary)
            display.extend(grp)

        # Non-options (stocks/futures) at end
        display.extend(non_options)

        self._rows = display
        self.endResetModel()

    def set_sorting(self, metric: str = "none", *, descending: bool = True, absolute: bool = True) -> None:
        self._sort_metric = (metric or "none").strip().lower()
        self._sort_desc = bool(descending)
        self._sort_abs = bool(absolute)

    def _sort_rows(self, rows: list) -> list[Any]:
        metric = self._sort_metric
        if metric == "none":
            return list(rows)

        def _value(position: Any) -> float:
            raw = getattr(position, metric, None)
            if raw is None:
                return float("-inf") if self._sort_desc else float("inf")
            numeric = float(raw)
            return abs(numeric) if self._sort_abs else numeric

        return sorted(rows, key=_value, reverse=self._sort_desc)

    def is_group_row(self, logical_row: int) -> bool:
        """Return True if the row at *logical_row* is a group-summary row."""
        if 0 <= logical_row < len(self._rows):
            return isinstance(self._rows[logical_row], _ExpiryGroupRow)
        return False

    def position_at(self, logical_row: int):
        """Return the concrete position row at *logical_row*, if any."""
        if 0 <= logical_row < len(self._rows):
            row = self._rows[logical_row]
            if not isinstance(row, _ExpiryGroupRow):
                return row
        return None

    # ── QAbstractTableModel interface ─────────────────────────────────────

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_POS_HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return _POS_HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()

        if isinstance(row, _ExpiryGroupRow):
            return self._group_row_data(row, col, role)

        # — regular position row —
        is_non_option = getattr(row, "sec_type", "") not in ("OPT", "FOP")
        is_option_missing_greeks = (
            getattr(row, "sec_type", "") in ("OPT", "FOP") and 
            getattr(row, "delta", None) is None
        )
        is_estimated_greeks = (
            getattr(row, "sec_type", "") in ("OPT", "FOP")
            and getattr(row, "greeks_source", None) == "estimated_bsm"
        )
        
        if role == Qt.ItemDataRole.BackgroundRole:
            if is_non_option:
                return QBrush(_STK_BG)
            elif is_option_missing_greeks:
                # Highlight options with missing Greeks in yellow/amber
                return QBrush(QColor("#fff3cd"))  # Light amber background
            elif is_estimated_greeks:
                return QBrush(QColor("#fff8e1"))
        if role == Qt.ItemDataRole.ForegroundRole:
            if is_non_option:
                return QBrush(_STK_FG)
            elif is_option_missing_greeks:
                # Darker text for missing Greeks
                return QBrush(QColor("#856404"))  # Amber-ish text
            elif is_estimated_greeks:
                return QBrush(QColor("#8a6d3b"))
        if role == Qt.ItemDataRole.FontRole and is_estimated_greeks and col in {_COL_DELTA, _COL_GAMMA, _COL_THETA, _COL_VEGA, _COL_IV}:
            f = QFont()
            f.setItalic(True)
            return f
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return self._format_cell(row, col)

    # ── private helpers ───────────────────────────────────────────────────

    def _group_row_data(self, row: _ExpiryGroupRow, col: int, role):
        if role == Qt.ItemDataRole.BackgroundRole:
            return QBrush(_GROUP_BG)
        if role == Qt.ItemDataRole.ForegroundRole:
            return QBrush(_GROUP_FG)
        if role == Qt.ItemDataRole.FontRole:
            f = QFont()
            f.setBold(True)
            return f
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if col == 0:
            return f"── {row.expiry}  ({row.count} contracts) ──"
        if col == 1:
            return "GROUP"
        if col == _COL_DELTA:
            return f"{row.delta:+.2f}" if row.delta is not None else ""
        if col == _COL_GAMMA:
            return f"{row.gamma:+.4f}" if row.gamma is not None else ""
        if col == _COL_THETA:
            return f"{row.theta:+.2f}" if row.theta is not None else ""
        if col == _COL_VEGA:
            return f"{row.vega:+.2f}"  if row.vega is not None else ""
        if col == _COL_SPX:
            return f"{row.spx_delta:+.2f}" if row.spx_delta is not None else ""
        return ""

    def _format_cell(self, row, col: int) -> str:
        match col:
            case 0: return row.symbol
            case 1: return row.sec_type
            case 2: return row.underlying or ""
            case 3: return f"{row.quantity:,.0f}"
            case 4: return f"${row.avg_cost:,.2f}" if row.avg_cost else ""
            case 5: return f"${row.market_price:,.2f}" if row.market_price else ""
            case 6: return f"${row.market_value:,.2f}" if row.market_value else ""
            case 7:
                pnl = row.unrealized_pnl
                return f"${pnl:+,.2f}" if pnl else ""
            case 8: return f"${row.underlying_price:,.2f}" if row.underlying_price else ""
            case 9: return f"{row.strike:,.0f}" if row.strike else ""
            case 10: return row.right or ""
            case 11: return row.expiry or ""
            case 12: return f"{row.delta:+.4f}" if row.delta is not None else ""
            case 13: return f"{row.gamma:+.4f}" if row.gamma is not None else ""
            case 14: return f"{row.theta:+.2f}" if row.theta is not None else ""
            case 15: return f"{row.vega:+.2f}" if row.vega is not None else ""
            case 16: return f"{row.iv:.1%}" if row.iv is not None else ""
            case 17: return f"{row.spx_delta:+.2f}" if row.spx_delta is not None else ""
            case _: return ""


# ── Option Chain ──────────────────────────────────────────────────────────

_CHAIN_CALL_HEADERS = ["Bid", "Ask", "Last", "Vol", "OI", "IV", "Δ", "Γ"]
_CHAIN_CENTER       = ["Strike"]
_CHAIN_PUT_HEADERS  = ["Δ", "Γ", "IV", "OI", "Vol", "Last", "Ask", "Bid"]
CHAIN_HEADERS       = _CHAIN_CALL_HEADERS + _CHAIN_CENTER + _CHAIN_PUT_HEADERS


class ChainTableModel(QAbstractTableModel):
    """Options chain matrix: calls on the left, puts on the right, strikes in the middle.

    Data is a list of tuples: (call_row|None, strike, put_row|None)
    where call_row/put_row are ChainRow dataclass instances.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[tuple] = []  # (ChainRow|None, float, ChainRow|None)

    def set_data(self, chain_rows: list) -> None:
        """Organize flat ChainRow list into call/put pairs per strike."""
        self.beginResetModel()

        # Group by strike
        by_strike: dict[float, dict[str, Any]] = {}
        for cr in chain_rows:
            s = cr.strike
            if s not in by_strike:
                by_strike[s] = {"C": None, "P": None}
            by_strike[s][cr.right] = cr

        self._rows = [
            (by_strike[s]["C"], s, by_strike[s]["P"])
            for s in sorted(by_strike.keys())
        ]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(CHAIN_HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return CHAIN_HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        call, strike, put = self._rows[index.row()]
        col = index.column()
        n_call = len(_CHAIN_CALL_HEADERS)
        n_center = len(_CHAIN_CENTER)

        if col < n_call:
            return self._format_chain_cell(call, col, is_call=True)
        elif col < n_call + n_center:
            return f"{strike:,.0f}"
        else:
            put_col = col - n_call - n_center
            return self._format_chain_cell(put, put_col, is_call=False)

    def get_chain_row_at(self, row_idx: int, right: str) -> Any | None:
        """Return the ChainRow at the given table row for the specified right ('C' or 'P')."""
        if 0 <= row_idx < len(self._rows):
            call, strike, put = self._rows[row_idx]
            return call if right == "C" else put
        return None

    def get_all_rows(self) -> list:
        """Return all ChainRow objects (calls and puts) from the current model data."""
        result = []
        for call, _strike, put in self._rows:
            if call is not None:
                result.append(call)
            if put is not None:
                result.append(put)
        return result

    def _format_chain_cell(self, cr, col: int, *, is_call: bool) -> str:
        if cr is None:
            return ""
        # Call order: Bid Ask Last Vol OI IV Δ Γ
        # Put order:  Δ   Γ  IV  OI Vol Last Ask Bid
        if is_call:
            match col:
                case 0: return f"{cr.bid:.2f}" if cr.bid is not None else ""
                case 1: return f"{cr.ask:.2f}" if cr.ask is not None else ""
                case 2: return f"{cr.last:.2f}" if cr.last is not None else ""
                case 3: return str(cr.volume) if cr.volume else ""
                case 4: return str(cr.open_interest) if cr.open_interest else ""
                case 5: return f"{cr.iv:.1%}" if cr.iv is not None else ""
                case 6: return f"{cr.delta:+.3f}" if cr.delta is not None else ""
                case 7: return f"{cr.gamma:+.4f}" if cr.gamma is not None else ""
                case _: return ""
        else:
            match col:
                case 0: return f"{cr.delta:+.3f}" if cr.delta is not None else ""
                case 1: return f"{cr.gamma:+.4f}" if cr.gamma is not None else ""
                case 2: return f"{cr.iv:.1%}" if cr.iv is not None else ""
                case 3: return str(cr.open_interest) if cr.open_interest else ""
                case 4: return str(cr.volume) if cr.volume else ""
                case 5: return f"{cr.last:.2f}" if cr.last is not None else ""
                case 6: return f"{cr.ask:.2f}" if cr.ask is not None else ""
                case 7: return f"{cr.bid:.2f}" if cr.bid is not None else ""
                case _: return ""


# ── Orders ────────────────────────────────────────────────────────────────

_ORDER_HEADERS = [
    "ID", "Status", "Type", "Side", "Limit",
    "Filled", "Source", "Created", "Updated",
]


class OrdersTableModel(QAbstractTableModel):
    """Model for the Orders panel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []

    def set_data(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

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
        row = self._rows[index.row()]
        col = index.column()
        match col:
            case 0: return str(row.get("id", ""))[:8]
            case 1: return row.get("status", "")
            case 2: return row.get("order_type", "")
            case 3: return row.get("side", "")
            case 4:
                lp = row.get("limit_price")
                return f"${lp:.2f}" if lp else ""
            case 5:
                fp = row.get("filled_price")
                return f"${fp:.2f}" if fp else ""
            case 6: return row.get("source", "")
            case 7: return str(row.get("created_at", ""))[:19]
            case 8: return str(row.get("updated_at", ""))[:19]
            case _: return ""
