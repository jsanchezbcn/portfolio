"""desktop/models/trade_groups.py — Heuristic trade-grouping for portfolio positions.

Groups raw PositionRow objects into logical trade structures:
  Iron Condor · Strangle · Straddle · Vertical Spread · Calendar Spread ·
  Butterfly · Covered Call/Put · Diagonal · Naked Option · Futures · Stocks

The resulting TradeGroupsModel uses the same flat-row + header-row pattern as
PositionsTableModel so it can drop into any QTableView with no extra plumbing.

Usage
-----
    from desktop.models.trade_groups import TradeGroupsModel
    model = TradeGroupsModel()
    model.set_data(position_rows)
    table_view.setModel(model)
"""
from __future__ import annotations

import dataclasses
from collections import defaultdict
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor

# ── column definitions ─────────────────────────────────────────────────────────

_HEADERS = [
    "Strategy", "Symbol/Trade", "Legs", "Expiry",
    "Net Δ Delta", "Net Θ Theta", "Net Vega", "SPX Δ",
    "Mkt Value", "Unrealized PnL",
]

_TRADE_BG  = QColor(20, 60, 40)    # dark green for trade header rows
_TRADE_FG  = QColor(180, 240, 180)
_LEG_ALT   = QColor(38, 38, 38)     # slightly lighter for odd legs
_STOCK_BG  = QColor(45, 50, 20)    # olive for stock/futures rows
_STOCK_FG  = QColor(230, 220, 160)

# ── data structures ────────────────────────────────────────────────────────────

@dataclasses.dataclass
class TradeGroup:
    """A logical trade made of one or more position legs."""
    strategy: str           # e.g. "Iron Condor", "Short Strangle"
    underlying: str
    expiry_label: str       # "20250117" or "20250117–20250221" for calendars
    legs: list              # list[PositionRow]
    net_delta: float | None = None
    net_gamma: float | None = None
    net_theta: float | None = None
    net_vega:  float | None = None
    net_spx_delta: float | None = None
    net_mkt_value: float = 0.0
    net_upnl: float = 0.0
    is_header: bool = True  # True → summary row; legs produce is_header=False rows

    def __post_init__(self):
        self._aggregate()

    def _aggregate(self):
        for p in self.legs:
            def _s(v): return float(v) if v is not None else 0.0
            self.net_delta    = (self.net_delta    or 0) + _s(p.delta)
            self.net_gamma    = (self.net_gamma    or 0) + _s(p.gamma)
            self.net_theta    = (self.net_theta    or 0) + _s(p.theta)
            self.net_vega     = (self.net_vega     or 0) + _s(p.vega)
            self.net_spx_delta = (self.net_spx_delta or 0) + _s(p.spx_delta)
            self.net_mkt_value += _s(p.market_value)
            self.net_upnl     += _s(p.unrealized_pnl)


# ── grouping logic ─────────────────────────────────────────────────────────────

def _key(p) -> tuple:
    """Sort/grouping key: underlying → expiry → right → strike."""
    return (
        (p.underlying or p.symbol).upper(),
        p.expiry or "",
        (p.right or "").upper(),
        float(p.strike or 0),
    )


def _classify(legs: list) -> str:
    """Classify a list of option legs into a strategy name."""
    if len(legs) == 1:
        p = legs[0]
        if p.sec_type in ("STK",):
            return "Stock"
        if p.sec_type in ("FUT",):
            return "Future"
        side = "Short" if (p.quantity or 0) < 0 else "Long"
        right = {"C": "Call", "P": "Put"}.get(p.right or "", "Option")
        return f"{side} {right}"
    if len(legs) == 2:
        p1, p2 = legs
        # Both must be options
        if p1.sec_type not in ("OPT", "FOP") or p2.sec_type not in ("OPT", "FOP"):
            # Mixed stock + option
            if p1.sec_type == "STK" or p2.sec_type == "STK":
                short_leg = next((p for p in legs if (p.quantity or 0) < 0 and p.right), None)
                if short_leg:
                    return f"Covered {'Call' if short_leg.right == 'C' else 'Put'}"
            return "Combo"
        same_exp  = p1.expiry == p2.expiry
        same_right = (p1.right or "").upper() == (p2.right or "").upper()
        diff_right = not same_right
        same_strike = abs(float(p1.strike or 0) - float(p2.strike or 0)) < 0.01
        both_short = (p1.quantity or 0) < 0 and (p2.quantity or 0) < 0
        both_long  = (p1.quantity or 0) > 0 and (p2.quantity or 0) > 0
        # Straddle: same strike, different rights, same expiry
        if same_exp and diff_right and same_strike:
            return "Short Straddle" if both_short else ("Long Straddle" if both_long else "Straddle")
        # Strangle: different strikes, different rights, same expiry
        if same_exp and diff_right and not same_strike:
            return "Short Strangle" if both_short else ("Long Strangle" if both_long else "Strangle")
        # Vertical spread: same right, same expiry, different strikes
        if same_exp and same_right:
            right_name = {"C": "Call", "P": "Put"}.get((p1.right or "").upper(), "")
            side = "Short" if both_short else ("Long" if both_long else "Vertical")
            return f"{side} {right_name} Spread"
        # Calendar: same right, different expiry, same (or similar) strike
        if not same_exp and same_right:
            return "Calendar Spread"
        # Diagonal: different right, different expiry
        if not same_exp and diff_right:
            return "Diagonal Spread"
    if len(legs) == 3:
        rights = [(p.right or "").upper() for p in legs]
        exps   = [p.expiry for p in legs]
        all_same_exp   = len(set(exps)) == 1
        all_same_right = len(set(rights)) == 1
        if all_same_exp and all_same_right:
            return "Butterfly"
        return "3-Leg Combo"
    if len(legs) == 4:
        rights  = [(p.right or "").upper() for p in legs]
        calls   = [p for p in legs if (p.right or "").upper() == "C"]
        puts    = [p for p in legs if (p.right or "").upper() == "P"]
        exps    = [p.expiry for p in legs]
        if len(calls) == 2 and len(puts) == 2 and len(set(exps)) == 1:
            return "Iron Condor"
        if len(calls) == 2 and len(puts) == 2:
            return "Iron Condor (Calendar)"
    if len(legs) >= 4:
        return f"{len(legs)}-Leg Combo"
    return "Combo"


def _expiry_label(legs: list) -> str:
    exps = sorted(set(p.expiry for p in legs if p.expiry))
    if not exps:
        return "—"
    if len(exps) == 1:
        return _fmt_exp(exps[0])
    return f"{_fmt_exp(exps[0])}–{_fmt_exp(exps[-1])}"


def _fmt_exp(exp: str) -> str:
    """Format YYYYMMDD as MMM-DD (e.g. 20250117 → Jan-17)."""
    import calendar
    try:
        y, m, d = int(exp[:4]), int(exp[4:6]), int(exp[6:8])
        return f"{calendar.month_abbr[m]}-{d:02d}-{str(y)[2:]}"
    except Exception:
        return exp


def group_positions(rows: list) -> list[TradeGroup]:
    """Group PositionRow objects into TradeGroup objects.

    Algorithm:
    1. Separate stocks/futures from options.
    2. Group options by underlying + same-expiry buckets.
    3. Within each bucket apply greedy pattern matching:
       - Try to match 4-leg iron condors first.
       - Then 3-leg butterflies.
       - Then 2-leg structures (strangles, spreads, calendars).
       - Remainder → naked single-leg trades.
    4. Add stocks and futures as their own single-asset groups last.
    """
    options: list = []
    stocks_futs: list = []
    for p in rows:
        if p.sec_type in ("OPT", "FOP"):
            options.append(p)
        else:
            stocks_futs.append(p)

    # Group by (underlying, expiry) first
    by_und_exp: dict[tuple, list] = defaultdict(list)
    for p in options:
        und = (p.underlying or p.symbol).upper()
        by_und_exp[(und, p.expiry or "")].append(p)

    groups: list[TradeGroup] = []

    # Process per-underlying (collect all expiries together for cross-expiry patterns)
    by_und: dict[str, list] = defaultdict(list)
    for p in options:
        und = (p.underlying or p.symbol).upper()
        by_und[und].append(p)

    for und, und_legs in sorted(by_und.items()):
        remaining = list(und_legs)
        used: set[int] = set()

        def take(candidates):
            idxs = [i for i, p in enumerate(remaining) if id(p) not in used and p in candidates]
            for i in idxs:
                used.add(id(remaining[i]))
            return [remaining[i] for i in idxs]

        def unused():
            return [p for p in remaining if id(p) not in used]

        # Pass 1: iron condors (4-leg, same expiry)
        exp_map: dict[str, list] = defaultdict(list)
        for p in und_legs:
            exp_map[p.expiry or ""].append(p)

        for exp, exp_legs in sorted(exp_map.items()):
            avail = [p for p in exp_legs if id(p) not in used]
            calls = sorted([p for p in avail if (p.right or "").upper() == "C"],
                           key=lambda p: float(p.strike or 0))
            puts  = sorted([p for p in avail if (p.right or "").upper() == "P"],
                           key=lambda p: float(p.strike or 0))
            # Try to form IC: need 2 calls + 2 puts at the same expiry
            while len(calls) >= 2 and len(puts) >= 2:
                ic_legs = [puts[0], puts[-1], calls[0], calls[-1]]
                # Validate: inner strikes closer to ATM than outer (for a proper condor)
                if abs(float(puts[-1].strike or 0)) < abs(float(puts[0].strike or 0)):
                    break
                for p in ic_legs:
                    used.add(id(p))
                groups.append(TradeGroup(
                    strategy=_classify(ic_legs),
                    underlying=und,
                    expiry_label=_expiry_label(ic_legs),
                    legs=ic_legs,
                ))
                # Remove used
                calls = [p for p in calls if id(p) not in used]
                puts  = [p for p in puts  if id(p) not in used]

        # Pass 2: 2-leg structures (strangles, spreads, calendars, straddles)
        avail_now = [p for p in und_legs if id(p) not in used]
        # Group by expiry for same-expiry 2-leg structures
        by_exp: dict[str, list] = defaultdict(list)
        for p in avail_now:
            by_exp[p.expiry or ""].append(p)

        for exp, ep in sorted(by_exp.items()):
            while len(ep) >= 2:
                calls = [p for p in ep if (p.right or "").upper() == "C" and id(p) not in used]
                puts  = [p for p in ep  if (p.right or "").upper() == "P" and id(p) not in used]
                if calls and puts:
                    pair = [calls[0], puts[0]]
                    for p in pair:
                        used.add(id(p))
                    groups.append(TradeGroup(
                        strategy=_classify(pair),
                        underlying=und,
                        expiry_label=_expiry_label(pair),
                        legs=pair,
                    ))
                elif len(calls) >= 2:
                    pair = calls[:2]
                    for p in pair:
                        used.add(id(p))
                    groups.append(TradeGroup(
                        strategy=_classify(pair),
                        underlying=und,
                        expiry_label=_expiry_label(pair),
                        legs=pair,
                    ))
                elif len(puts) >= 2:
                    pair = puts[:2]
                    for p in pair:
                        used.add(id(p))
                    groups.append(TradeGroup(
                        strategy=_classify(pair),
                        underlying=und,
                        expiry_label=_expiry_label(pair),
                        legs=pair,
                    ))
                else:
                    break
                ep = [p for p in ep if id(p) not in used]

        # Pass 3: Calendar/diagonal pairs (different expiries, same right+strike)
        avail3 = [p for p in und_legs if id(p) not in used]
        for i in range(len(avail3)):
            if id(avail3[i]) in used:
                continue
            pi = avail3[i]
            for j in range(i + 1, len(avail3)):
                if id(avail3[j]) in used:
                    continue
                pj = avail3[j]
                if (
                    (pi.right or "").upper() == (pj.right or "").upper()
                    and abs(float(pi.strike or 0) - float(pj.strike or 0)) < 0.01
                    and pi.expiry != pj.expiry
                ):
                    pair = [pi, pj]
                    used.add(id(pi))
                    used.add(id(pj))
                    groups.append(TradeGroup(
                        strategy=_classify(pair),
                        underlying=und,
                        expiry_label=_expiry_label(pair),
                        legs=pair,
                    ))
                    break

        # Pass 4: remaining singles
        for p in und_legs:
            if id(p) not in used:
                groups.append(TradeGroup(
                    strategy=_classify([p]),
                    underlying=und,
                    expiry_label=_expiry_label([p]),
                    legs=[p],
                ))

    # Stocks and futures
    for p in stocks_futs:
        groups.append(TradeGroup(
            strategy="Stock" if p.sec_type == "STK" else "Future",
            underlying=p.symbol,
            expiry_label=_fmt_exp(p.expiry) if p.expiry else "—",
            legs=[p],
        ))

    return groups


# ── Qt model ───────────────────────────────────────────────────────────────────

class _TradeRow:
    """Either a trade-header row or a leg row."""
    __slots__ = ("is_header", "group", "leg", "row_idx_in_group")

    def __init__(self, *, is_header: bool, group: TradeGroup, leg=None, row_idx: int = 0):
        self.is_header      = is_header
        self.group          = group
        self.leg            = leg          # None for header rows
        self.row_idx_in_group = row_idx


class TradeGroupsModel(QAbstractTableModel):
    """Flat model that shows trade-header rows followed by individual leg rows.

    Header rows are dark-green; leg rows are indented with the position details.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[_TradeRow] = []

    # ── public API ─────────────────────────────────────────────────────────────

    def set_data(self, positions: list) -> None:
        """Re-group positions and refresh the model."""
        self.beginResetModel()
        self._rows = []
        if positions:
            groups = group_positions(positions)
            for grp in groups:
                self._rows.append(_TradeRow(is_header=True, group=grp))
                for i, leg in enumerate(grp.legs):
                    self._rows.append(_TradeRow(is_header=False, group=grp, leg=leg, row_idx=i))
        self.endResetModel()

    def payload_at(self, logical_row: int) -> dict[str, Any] | None:
        """Return a payload describing the selected trade or leg row."""
        if not (0 <= logical_row < len(self._rows)):
            return None
        row_obj = self._rows[logical_row]
        if row_obj.is_header:
            return {
                "kind": "trade_group",
                "description": row_obj.group.strategy,
                "legs": list(row_obj.group.legs),
            }
        if row_obj.leg is not None:
            return {
                "kind": "trade_leg",
                "description": row_obj.leg.symbol,
                "legs": [row_obj.leg],
            }
        return None

    # ── QAbstractTableModel interface ──────────────────────────────────────────

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
        col     = index.column()

        # ── background colour ──────────────────────────────────────────────────
        if role == Qt.ItemDataRole.BackgroundRole:
            if row_obj.is_header:
                return QBrush(_TRADE_BG)
            if row_obj.leg and row_obj.leg.sec_type in ("STK", "FUT"):
                return QBrush(_STOCK_BG)
            if row_obj.row_idx_in_group % 2 == 1:
                return QBrush(_LEG_ALT)
            return None

        if role == Qt.ItemDataRole.ForegroundRole:
            if row_obj.is_header:
                return QBrush(_TRADE_FG)
            if row_obj.leg and row_obj.leg.sec_type in ("STK", "FUT"):
                return QBrush(_STOCK_FG)
            return None

        if role == Qt.ItemDataRole.FontRole:
            from PySide6.QtGui import QFont
            f = QFont()
            if row_obj.is_header:
                f.setBold(True)
            return f

        if role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignCenter

        if role != Qt.ItemDataRole.DisplayRole:
            return None

        grp = row_obj.group
        leg = row_obj.leg

        def _fmt(v, fmt=".2f"):
            if v is None:
                return "—"
            try:
                return format(float(v), fmt)
            except Exception:
                return str(v)

        def _color_pnl(v):
            return _fmt(v, "+,.2f") if v is not None else "—"

        if row_obj.is_header:
            # ── strategy header row ────────────────────────────────────────────
            mapping = {
                0: f"🔷 {grp.strategy}",
                1: grp.underlying,
                2: str(len(grp.legs)),
                3: grp.expiry_label,
                4: _fmt(grp.net_delta),
                5: _fmt(grp.net_theta),
                6: _fmt(grp.net_vega),
                7: _fmt(grp.net_spx_delta),
                8: _fmt(grp.net_mkt_value, ",.2f"),
                9: _color_pnl(grp.net_upnl),
            }
            return mapping.get(col, "")
        else:
            # ── individual leg row (indented) ──────────────────────────────────
            if leg is None:
                return ""
            if col == 0:
                right_lbl = {"C": "Call", "P": "Put"}.get(leg.right or "", "")
                qty_s = f"{leg.quantity:+.0f}" if leg.quantity else "0"
                return f"  {qty_s} {right_lbl or leg.sec_type}"
            if col == 1:
                return f"  {leg.symbol}"
            if col == 2:
                return f"  {leg.strike:.0f}" if leg.strike else "—"
            if col == 3:
                return _fmt_exp(leg.expiry) if leg.expiry else "—"
            if col == 4:
                return _fmt(leg.delta)
            if col == 5:
                return _fmt(leg.theta)
            if col == 6:
                return _fmt(leg.vega)
            if col == 7:
                return _fmt(leg.spx_delta)
            if col == 8:
                return _fmt(leg.market_value, ",.2f")
            if col == 9:
                return _color_pnl(leg.unrealized_pnl)
        return ""
