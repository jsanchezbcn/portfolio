"""desktop/tests/test_table_models.py — Tests for Qt table models.

These are pure data-model tests (no widgets, no qtbot needed).
They verify that PositionsTableModel, ChainTableModel, OrdersTableModel
correctly expose data via the Qt model/view interface.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from desktop.models.table_models import (
    PositionsTableModel,
    ChainTableModel,
    OrdersTableModel,
    _POS_HEADERS,
    CHAIN_HEADERS,
)
from desktop.engine.ib_engine import PositionRow, ChainRow


# ── helpers ───────────────────────────────────────────────────────────────


def _pos_row(**kw) -> PositionRow:
    defaults = dict(
        conid=1, symbol="ES", sec_type="FOP", underlying="ES",
        strike=5500.0, right="C", expiry="20260320",
        quantity=-1.0, avg_cost=50.0,
        market_price=45.0, market_value=-4500.0,
        unrealized_pnl=500.0, realized_pnl=0.0,
        delta=-0.35, gamma=0.01, theta=-5.0, vega=12.0,
        iv=0.18, spx_delta=-17.5,
    )
    defaults.update(kw)
    return PositionRow(**defaults)


def _chain_row(strike: float, right: str, **kw) -> ChainRow:
    defaults = dict(
        underlying="ES", expiry="20260320", strike=strike, right=right,
        conid=int(strike * 10), bid=10.0, ask=12.0, last=11.0,
        volume=100, open_interest=500, iv=0.18,
        delta=0.35 if right == "C" else -0.35,
        gamma=0.01, theta=-5.0, vega=12.0,
    )
    defaults.update(kw)
    return ChainRow(**defaults)


# ── PositionsTableModel ──────────────────────────────────────────────────


class TestPositionsTableModel:

    def test_empty_model(self, qapp):
        m = PositionsTableModel()
        assert m.rowCount() == 0
        assert m.columnCount() == len(_POS_HEADERS)

    # ── basic interface ────────────────────────────────────────────────────

    def test_set_data_two_same_expiry(self, qapp):
        """2 FOP rows with same expiry → 1 group header + 2 position rows = 3."""
        m = PositionsTableModel()
        m.set_data([_pos_row(), _pos_row(symbol="SPY")])
        assert m.rowCount() == 3  # group header + 2 rows

    def test_header_data(self, qapp):
        m = PositionsTableModel()
        assert m.headerData(0, Qt.Orientation.Horizontal) == "Symbol"
        assert m.headerData(3, Qt.Orientation.Horizontal) == "Qty"

    def test_data_symbol(self, qapp):
        """Row 0 is the group header; row 1 is the actual position row."""
        m = PositionsTableModel()
        m.set_data([_pos_row(symbol="MES")])
        idx = m.index(1, 0)  # row 1 = first position row
        assert m.data(idx) == "MES"

    def test_data_quantity_formatted(self, qapp):
        m = PositionsTableModel()
        m.set_data([_pos_row(quantity=-3.0)])
        idx = m.index(1, 3)  # row 1 = position row
        assert "-3" in m.data(idx)

    def test_data_pnl_formatted(self, qapp):
        m = PositionsTableModel()
        m.set_data([_pos_row(unrealized_pnl=1234.56)])
        idx = m.index(1, 7)  # row 1 = position row
        val = m.data(idx)
        assert "1,234.56" in val

    def test_data_delta(self, qapp):
        m = PositionsTableModel()
        m.set_data([_pos_row(delta=-0.35)])
        idx = m.index(1, 11)  # row 1 = position row
        assert "-0.35" in m.data(idx)

    def test_data_iv_percentage(self, qapp):
        m = PositionsTableModel()
        m.set_data([_pos_row(iv=0.18)])
        idx = m.index(1, 15)  # row 1 = position row
        assert "18.0%" in m.data(idx)

    def test_data_none_values(self, qapp):
        m = PositionsTableModel()
        m.set_data([_pos_row(iv=None, delta=None, strike=None)])
        assert m.data(m.index(1, 15)) == ""  # IV — row 1 is position
        assert m.data(m.index(1, 11)) == ""  # Delta
        assert m.data(m.index(1, 8)) == ""   # Strike

    def test_estimated_greeks_rows_use_italic_font(self, qapp):
        from PySide6.QtGui import QFont

        m = PositionsTableModel()
        m.set_data([_pos_row(greeks_source="estimated_bsm")])
        font = m.data(m.index(1, 11), Qt.ItemDataRole.FontRole)
        assert isinstance(font, QFont)
        assert font.italic()

    def test_invalid_index_returns_none(self, qapp):
        m = PositionsTableModel()
        from PySide6.QtCore import QModelIndex
        assert m.data(QModelIndex()) is None

    def test_set_data_resets(self, qapp):
        m = PositionsTableModel()
        m.set_data([_pos_row()])
        m.set_data([])
        assert m.rowCount() == 0

    # ── expiry grouping ────────────────────────────────────────────────────

    def test_group_headers_inserted(self, qapp):
        """Each distinct expiry gets exactly one group-header row."""
        m = PositionsTableModel()
        r1 = _pos_row(expiry="20260320", conid=1)
        r2 = _pos_row(expiry="20260320", conid=2)
        r3 = _pos_row(expiry="20260418", conid=3)
        m.set_data([r1, r2, r3])
        # 2 groups → 2 headers, 3 position rows = 5 total
        assert m.rowCount() == 5
        assert m.is_group_row(0)   # first expiry header
        assert not m.is_group_row(1)  # position
        assert not m.is_group_row(2)  # position
        assert m.is_group_row(3)   # second expiry header
        assert not m.is_group_row(4)  # position

    def test_groups_sorted_by_expiry(self, qapp):
        """Earlier expiry appears first."""
        m = PositionsTableModel()
        m.set_data([
            _pos_row(expiry="20260418", conid=1),
            _pos_row(expiry="20260320", conid=2),
        ])
        # row 0 is group header for 20260320
        header_text = m.data(m.index(0, 0))
        assert "20260320" in header_text

    def test_group_header_label(self, qapp):
        """Group header col-0 shows expiry and contract count."""
        m = PositionsTableModel()
        m.set_data([_pos_row(expiry="20260320", conid=1),
                    _pos_row(expiry="20260320", conid=2)])
        label = m.data(m.index(0, 0))
        assert "20260320" in label
        assert "2" in label  # count

    def test_group_header_aggregates_greeks(self, qapp):
        """Group header shows summed delta/theta/vega/spx_delta."""
        m = PositionsTableModel()
        m.set_data([
            _pos_row(conid=1, delta=-10.0, theta=-5.0, vega=20.0, spx_delta=-500.0, expiry="20260320"),
            _pos_row(conid=2, delta=+3.0,  theta=-2.0, vega= 8.0, spx_delta=+150.0, expiry="20260320"),
        ])
        header = m.index(0, 0)  # group row
        assert m.is_group_row(0)
        # col 11 = delta, col 13 = theta, col 14 = vega, col 16 = spx_delta
        assert "-7.00" in m.data(m.index(0, 11))   # delta sum = -7
        assert "-7.00" in m.data(m.index(0, 13))   # theta sum = -7
        assert "+28.00" in m.data(m.index(0, 14))  # vega sum = 28
        assert "-350.00" in m.data(m.index(0, 16)) # spx_delta sum = -350

    def test_group_header_background_role(self, qapp):
        """Group rows return a QBrush for BackgroundRole."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QBrush
        m = PositionsTableModel()
        m.set_data([_pos_row()])
        brush = m.data(m.index(0, 0), Qt.ItemDataRole.BackgroundRole)
        assert isinstance(brush, QBrush)

    def test_group_header_font_bold(self, qapp):
        """Group rows return a bold QFont for FontRole."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFont
        m = PositionsTableModel()
        m.set_data([_pos_row()])
        font = m.data(m.index(0, 0), Qt.ItemDataRole.FontRole)
        assert isinstance(font, QFont)
        assert font.bold()

    # ── stocks / futures at end ────────────────────────────────────────────

    def test_non_options_appended_at_end(self, qapp):
        """STK and FUT rows appear after all option groups."""
        stk = _pos_row(sec_type="STK", expiry=None, strike=None, right=None,
                       underlying="", conid=99, symbol="SPY")
        opt = _pos_row(conid=1, expiry="20260320")
        m = PositionsTableModel()
        m.set_data([stk, opt])  # stk comes first in input
        # display: group_header(opt) | opt_row | stk_row
        assert m.rowCount() == 3
        # Last row should be the stock
        last_symbol = m.data(m.index(2, 0))
        assert last_symbol == "SPY"

    def test_stk_background_role(self, qapp):
        """STK rows get a distinct background for visual separation."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QBrush
        stk = _pos_row(sec_type="STK", expiry=None, strike=None, right=None,
                       underlying="", conid=99, symbol="SPY")
        m = PositionsTableModel()
        m.set_data([stk])
        # Row 0 is the STK row (no options → no group headers)
        brush = m.data(m.index(0, 0), Qt.ItemDataRole.BackgroundRole)
        assert isinstance(brush, QBrush)


# ── ChainTableModel ──────────────────────────────────────────────────────


class TestChainTableModel:

    def test_empty_model(self, qapp):
        m = ChainTableModel()
        assert m.rowCount() == 0
        assert m.columnCount() == len(CHAIN_HEADERS)

    def test_set_data_groups_by_strike(self, qapp):
        m = ChainTableModel()
        rows = [
            _chain_row(5500.0, "C"),
            _chain_row(5500.0, "P"),
            _chain_row(5600.0, "C"),
            _chain_row(5600.0, "P"),
        ]
        m.set_data(rows)
        assert m.rowCount() == 2  # 2 strikes

    def test_strike_column(self, qapp):
        m = ChainTableModel()
        m.set_data([_chain_row(5500.0, "C"), _chain_row(5500.0, "P")])
        idx = m.index(0, 8)  # strike column
        assert "5,500" in m.data(idx)

    def test_call_bid(self, qapp):
        m = ChainTableModel()
        m.set_data([_chain_row(5500.0, "C", bid=15.5)])
        idx = m.index(0, 0)  # call bid
        assert "15.50" in m.data(idx)

    def test_put_bid(self, qapp):
        m = ChainTableModel()
        m.set_data([_chain_row(5500.0, "P", bid=8.25)])
        idx = m.index(0, 16)  # put bid is last column
        assert "8.25" in m.data(idx)

    def test_get_chain_row_at_call(self, qapp):
        m = ChainTableModel()
        m.set_data([_chain_row(5500.0, "C"), _chain_row(5500.0, "P")])
        cr = m.get_chain_row_at(0, "C")
        assert cr is not None
        assert cr.right == "C"
        assert cr.strike == 5500.0

    def test_get_chain_row_at_put(self, qapp):
        m = ChainTableModel()
        m.set_data([_chain_row(5500.0, "C"), _chain_row(5500.0, "P")])
        cr = m.get_chain_row_at(0, "P")
        assert cr is not None
        assert cr.right == "P"

    def test_get_chain_row_at_invalid(self, qapp):
        m = ChainTableModel()
        assert m.get_chain_row_at(0, "C") is None
        assert m.get_chain_row_at(-1, "C") is None

    def test_sorted_strikes(self, qapp):
        m = ChainTableModel()
        m.set_data([
            _chain_row(5600.0, "C"),
            _chain_row(5500.0, "C"),
            _chain_row(5600.0, "P"),
            _chain_row(5500.0, "P"),
        ])
        # First row should be lowest strike
        assert "5,500" in m.data(m.index(0, 8))
        assert "5,600" in m.data(m.index(1, 8))

    def test_missing_put_shows_empty(self, qapp):
        m = ChainTableModel()
        m.set_data([_chain_row(5500.0, "C")])  # no put
        # Put side should be empty strings
        assert m.data(m.index(0, 16)) == ""  # put bid

    def test_header_data(self, qapp):
        m = ChainTableModel()
        assert m.headerData(0, Qt.Orientation.Horizontal) == "Bid"
        assert m.headerData(8, Qt.Orientation.Horizontal) == "Strike"
