"""desktop/models/trade_groups.py — strategy-group model for the portfolio tab.

The portfolio "Strategies" view shows a grouped, human-friendly reconstruction of
raw positions. Each top-level row represents a strategy association and expands
into the individual legs that were matched into that structure.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor, QFont

from desktop.models.strategy_reconstructor import (
    StrategyGroup,
    StrategyReconstructor,
    reconstruct_strategy_groups,
)

_HEADERS = [
    "Strategy", "Symbol/Trade", "Legs", "Expiry",
    "Net Δ Delta", "Net Θ Theta", "Net Vega", "SPX Δ",
    "Mkt Value", "Unrealized PnL",
]

_TRADE_BG = QColor(20, 60, 40)
_TRADE_FG = QColor(180, 240, 180)
_LEG_ALT = QColor(38, 38, 38)
_STOCK_BG = QColor(45, 50, 20)
_STOCK_FG = QColor(230, 220, 160)

TradeGroup = StrategyGroup
group_positions = reconstruct_strategy_groups


class _TradeRow:
    __slots__ = ("is_header", "group", "leg", "row_idx_in_group")

    def __init__(self, *, is_header: bool, group: StrategyGroup, leg=None, row_idx: int = 0):
        self.is_header = is_header
        self.group = group
        self.leg = leg
        self.row_idx_in_group = row_idx


class TradeGroupsModel(QAbstractTableModel):
    """Flat strategy model: strategy header rows followed by constituent legs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[_TradeRow] = []
        self._groups: list[StrategyGroup] = []
        self._sort_metric: str = "none"
        self._sort_desc: bool = True
        self._sort_abs: bool = True

    def set_sorting(self, metric: str = "none", *, descending: bool = True, absolute: bool = True) -> None:
        self._sort_metric = (metric or "none").strip().lower()
        self._sort_desc = bool(descending)
        self._sort_abs = bool(absolute)

    def set_data(self, positions: list, *, account_id: str | None = None) -> None:
        self.beginResetModel()
        self._rows = []
        reconstructor = StrategyReconstructor(account_id=account_id)
        self._groups = reconstructor.reconstruct(positions or []) if positions else []
        self._groups = self._sort_groups(self._groups)
        for group in self._groups:
            self._rows.append(_TradeRow(is_header=True, group=group))
            for idx, leg in enumerate(group.legs):
                self._rows.append(_TradeRow(is_header=False, group=group, leg=leg, row_idx=idx))
        self.endResetModel()

    def payload_at(self, logical_row: int) -> dict[str, Any] | None:
        if not (0 <= logical_row < len(self._rows)):
            return None
        row_obj = self._rows[logical_row]
        if row_obj.is_header:
            return {
                "kind": "trade_group",
                "description": row_obj.group.strategy_name,
                "association_id": row_obj.group.association_id,
                "strategy_name": row_obj.group.strategy_name,
                "underlying": row_obj.group.underlying,
                "legs": list(row_obj.group.legs),
            }
        if row_obj.leg is not None:
            return {
                "kind": "trade_leg",
                "description": getattr(row_obj.leg, "symbol", ""),
                "association_id": row_obj.group.association_id,
                "strategy_name": row_obj.group.strategy_name,
                "legs": [row_obj.leg],
            }
        return None

    def strategy_records(self) -> list[dict[str, Any]]:
        return [group.to_record() for group in self._groups]

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(_HEADERS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row_obj = self._rows[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.BackgroundRole:
            if row_obj.is_header:
                return QBrush(_TRADE_BG)
            if row_obj.leg and getattr(row_obj.leg, "sec_type", "") in ("STK", "FUT"):
                return QBrush(_STOCK_BG)
            if row_obj.row_idx_in_group % 2 == 1:
                return QBrush(_LEG_ALT)
            return None

        if role == Qt.ItemDataRole.ForegroundRole:
            if row_obj.is_header:
                return QBrush(_TRADE_FG)
            if row_obj.leg and getattr(row_obj.leg, "sec_type", "") in ("STK", "FUT"):
                return QBrush(_STOCK_FG)
            return None

        if role == Qt.ItemDataRole.FontRole:
            font = QFont()
            if row_obj.is_header:
                font.setBold(True)
            return font

        if role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignCenter

        if role != Qt.ItemDataRole.DisplayRole:
            return None

        if row_obj.is_header:
            return self._header_display(row_obj.group, col)
        return self._leg_display(row_obj.leg, col)

    def _sort_groups(self, groups: list[StrategyGroup]) -> list[StrategyGroup]:
        metric = self._sort_metric
        if metric == "none":
            return list(groups)

        def _value(group: StrategyGroup) -> float:
            mapping = {
                "unrealized_pnl": group.net_upnl,
                "market_value": group.net_mkt_value,
                "delta": group.net_delta,
                "gamma": group.net_gamma,
                "theta": group.net_theta,
                "vega": group.net_vega,
                "spx_delta": group.net_spx_delta,
            }
            raw = mapping.get(metric)
            if raw is None:
                return float("-inf") if self._sort_desc else float("inf")
            numeric = float(raw)
            return abs(numeric) if self._sort_abs else numeric

        return sorted(groups, key=_value, reverse=self._sort_desc)

    @staticmethod
    def _header_display(group: StrategyGroup, col: int) -> str:
        mapping = {
            0: f"🔷 {group.strategy_name}",
            1: group.underlying,
            2: str(len(group.legs)),
            3: group.expiry_label,
            4: _fmt(group.net_delta),
            5: _fmt(group.net_theta),
            6: _fmt(group.net_vega),
            7: _fmt(group.net_spx_delta),
            8: _fmt(group.net_mkt_value, ",.2f"),
            9: _fmt(group.net_upnl, "+,.2f"),
        }
        return mapping.get(col, "")

    @staticmethod
    def _leg_display(leg: Any, col: int) -> str:
        if leg is None:
            return ""
        if col == 0:
            right_lbl = {"C": "Call", "P": "Put"}.get(getattr(leg, "right", "") or "", "")
            qty = getattr(leg, "quantity", 0.0) or 0.0
            qty_s = f"{float(qty):+.0f}"
            label = right_lbl or getattr(leg, "sec_type", "")
            return f"  {qty_s} {label}".rstrip()
        if col == 1:
            return f"  {getattr(leg, 'symbol', '')}"
        if col == 2:
            strike = getattr(leg, "strike", None)
            return f"  {float(strike):.0f}" if strike not in (None, "") else "—"
        if col == 3:
            expiry = getattr(leg, "expiry", None)
            return _fmt_exp(str(expiry)) if expiry else "—"
        if col == 4:
            return _fmt(getattr(leg, "delta", None))
        if col == 5:
            return _fmt(getattr(leg, "theta", None))
        if col == 6:
            return _fmt(getattr(leg, "vega", None))
        if col == 7:
            return _fmt(getattr(leg, "spx_delta", None))
        if col == 8:
            return _fmt(getattr(leg, "market_value", None), ",.2f")
        if col == 9:
            return _fmt(getattr(leg, "unrealized_pnl", None), "+,.2f")
        return ""


def _fmt(value: Any, fmt: str = ".2f") -> str:
    if value is None:
        return "—"
    try:
        return format(float(value), fmt)
    except (TypeError, ValueError):
        return str(value)


def _fmt_exp(expiry: str) -> str:
    import calendar

    digits = (expiry or "").replace("-", "")
    if len(digits) < 8 or not digits[:8].isdigit():
        return expiry
    year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
    return f"{calendar.month_abbr[month]}-{day:02d}-{str(year)[2:]}"
