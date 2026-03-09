"""desktop/tests/test_chain_tab.py — Tests for the Options Chain tab widget.

Verifies:
  - Underlying / SecType / Exchange combo defaults
  - Sec type ↔ exchange auto-sync
  - Chain table populates from signal
  - Double-click emits chain_row_selected signal
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QModelIndex
from PySide6.QtWidgets import QComboBox

from desktop.ui.chain_tab import ChainTab
from desktop.engine.ib_engine import ChainRow


def _sample_chain_rows() -> list[ChainRow]:
    """Tiny option chain (2 strikes × call/put)."""
    rows = []
    for strike in (5500.0, 5600.0):
        for right in ("C", "P"):
            rows.append(ChainRow(
                underlying="ES",
                expiry="20260320",
                strike=strike,
                right=right,
                conid=int(strike * 10 + (1 if right == "C" else 2)),
                bid=10.0 + (strike - 5500) / 100,
                ask=12.0 + (strike - 5500) / 100,
                last=11.0,
                volume=100,
                open_interest=500,
                iv=0.18,
                delta=0.35 if right == "C" else -0.35,
                gamma=0.01,
                theta=-5.0,
                vega=12.0,
            ))
    return rows


class TestChainTabLayout:
    """Verify Chain tab structure."""

    def test_creates_without_crash(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)

    def test_default_underlying_is_es(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._cmb_underlying.currentText() == "ES"

    def test_default_sec_type_is_fop(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._cmb_sec_type.currentText() == "FOP"

    def test_default_exchange_is_cme(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._cmb_exchange.currentText() == "CME"

    def test_has_fetch_button(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._btn_fetch is not None
        assert "Fetch" in tab._btn_fetch.text()

    def test_has_expiry_combo(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._cmb_expiry is not None

    def test_has_sd_range_combo(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._cmb_sd_range.currentText() in {"±1σ", "±2σ", "±3σ"}

    def test_stream_refresh_interval_is_5_seconds(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._stream_timer.interval() == 5_000

    def test_table_starts_empty(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        assert tab._model.rowCount() == 0


class TestChainTabAutoSync:
    """Test that underlying ↔ sec type ↔ exchange auto-sync correctly."""

    def test_spy_sets_opt_smart(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)

        tab._cmb_underlying.setCurrentText("SPY")

        assert tab._cmb_sec_type.currentText() == "OPT"
        assert tab._cmb_exchange.currentText() == "SMART"

    def test_es_sets_fop_cme(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)

        # Change away from ES, then back
        tab._cmb_underlying.setCurrentText("SPY")
        tab._cmb_underlying.setCurrentText("ES")

        assert tab._cmb_sec_type.currentText() == "FOP"
        assert tab._cmb_exchange.currentText() == "CME"

    def test_mes_sets_fop_cme(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)

        tab._cmb_underlying.setCurrentText("MES")

        assert tab._cmb_sec_type.currentText() == "FOP"
        assert tab._cmb_exchange.currentText() == "CME"

    def test_manual_sec_type_change_updates_exchange(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)

        tab._cmb_sec_type.setCurrentText("OPT")
        assert tab._cmb_exchange.currentText() == "SMART"

        tab._cmb_sec_type.setCurrentText("FOP")
        assert tab._cmb_exchange.currentText() == "CME"


class TestChainTabData:
    """Test chain table population from signals."""

    def test_chain_ready_signal_populates_table(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)

        rows = _sample_chain_rows()
        mock_engine.chain_ready.emit(rows)

        # 2 strikes → 2 rows in the matrix
        assert tab._model.rowCount() == 2

    def test_chain_table_shows_strike(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)

        rows = _sample_chain_rows()
        mock_engine.chain_ready.emit(rows)

        # Strike is in column 8 (after 8 call columns)
        idx = tab._model.index(0, 8)
        assert "5,500" in str(tab._model.data(idx))

    def test_chain_table_shows_bid(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)

        rows = _sample_chain_rows()
        mock_engine.chain_ready.emit(rows)

        # Call bid is column 0
        idx = tab._model.index(0, 0)
        assert "10.00" in str(tab._model.data(idx))

    def test_chain_row_selected_signal_emitted(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)

        rows = _sample_chain_rows()
        mock_engine.chain_ready.emit(rows)

        # Spy on the signal
        with qtbot.waitSignal(tab.chain_row_selected, timeout=1000) as blocker:
            # Simulate double-click on first row, column 3 (Vol — non bid/ask)
            idx = tab._model.index(0, 3)
            tab._on_double_click(idx)

        assert blocker.args[0].strike == 5500.0
        assert blocker.args[0].right == "C"

    def test_chain_row_put_side_click(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)

        rows = _sample_chain_rows()
        mock_engine.chain_ready.emit(rows)

        # Click on the put side (column 9+ = after strike)
        with qtbot.waitSignal(tab.chain_row_selected, timeout=1000) as blocker:
            idx = tab._model.index(0, 10)  # past strike column
            tab._on_double_click(idx)

        assert blocker.args[0].right == "P"

    def test_empty_chain_clears_table(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)

        rows = _sample_chain_rows()
        mock_engine.chain_ready.emit(rows)
        assert tab._model.rowCount() == 2

        mock_engine.chain_ready.emit([])
        assert tab._model.rowCount() == 0

    def test_expiry_change_clears_existing_rows_immediately(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)

        mock_engine.chain_ready.emit(_sample_chain_rows())
        assert tab._model.rowCount() == 2

        tab._on_expiry_changed("20260417")

        assert tab._model.rowCount() == 0

    def test_chain_ready_filters_rows_to_selected_expiry(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        tab._cmb_expiry.addItem("20260417")
        tab._cmb_expiry.setCurrentText("20260417")

        mock_engine.chain_ready.emit(_sample_chain_rows())

        assert tab._model.rowCount() == 0


class TestChainTabLegClicks:
    """Test single-click bid/ask direct leg staging signal."""

    def _load(self, tab, mock_engine):
        mock_engine.chain_ready.emit(_sample_chain_rows())

    # ── leg_clicked signal ─────────────────────────────────────────────

    def test_click_call_ask_emits_leg_clicked_buy_call(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        self._load(tab, mock_engine)

        with qtbot.waitSignal(tab.leg_clicked, timeout=1000) as blocker:
            idx = tab._model.index(0, 1)   # col 1 = Call Ask → BUY Call
            tab._on_click(idx)

        cr, action = blocker.args
        assert action == "BUY"
        assert cr.right == "C"
        assert cr.strike == 5500.0

    def test_click_call_bid_emits_leg_clicked_sell_call(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        self._load(tab, mock_engine)

        with qtbot.waitSignal(tab.leg_clicked, timeout=1000) as blocker:
            idx = tab._model.index(0, 0)   # col 0 = Call Bid → SELL Call
            tab._on_click(idx)

        cr, action = blocker.args
        assert action == "SELL"
        assert cr.right == "C"

    def test_click_put_ask_emits_leg_clicked_buy_put(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        self._load(tab, mock_engine)

        with qtbot.waitSignal(tab.leg_clicked, timeout=1000) as blocker:
            idx = tab._model.index(0, 15)  # col 15 = Put Ask → BUY Put
            tab._on_click(idx)

        cr, action = blocker.args
        assert action == "BUY"
        assert cr.right == "P"

    def test_click_put_bid_emits_leg_clicked_sell_put(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        self._load(tab, mock_engine)

        with qtbot.waitSignal(tab.leg_clicked, timeout=1000) as blocker:
            idx = tab._model.index(0, 16)  # col 16 = Put Bid → SELL Put
            tab._on_click(idx)

        cr, action = blocker.args
        assert action == "SELL"
        assert cr.right == "P"

    def test_click_non_price_column_does_not_emit_leg_clicked(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        self._load(tab, mock_engine)

        with qtbot.assertNotEmitted(tab.leg_clicked):
            idx = tab._model.index(0, 8)   # Strike column — no action
            tab._on_click(idx)

    def test_click_does_not_use_cart_state(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        self._load(tab, mock_engine)

        tab._on_click(tab._model.index(0, 1))  # BUY Call
        tab._on_click(tab._model.index(0, 16))  # SELL Put
        assert not hasattr(tab, "_cart")

    def test_status_label_updated_after_click(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        self._load(tab, mock_engine)

        tab._on_click(tab._model.index(0, 1))
        assert "Order Entry" in tab._lbl_status.text() or "5500" in tab._lbl_status.text()

    # ── double-click guard ────────────────────────────────────────────

    def test_double_click_on_bid_does_not_emit_chain_row_selected(self, qtbot, mock_engine):
        """Bid/ask columns must NOT trigger chain_row_selected on double-click."""
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        self._load(tab, mock_engine)

        with qtbot.assertNotEmitted(tab.chain_row_selected):
            idx = tab._model.index(0, 0)  # Call Bid — click-to-stage only
            tab._on_double_click(idx)

    def test_double_click_on_strike_col_emits_chain_row_selected(self, qtbot, mock_engine):
        """Double-click on non-bid/ask col still emits chain_row_selected."""
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        self._load(tab, mock_engine)

        with qtbot.waitSignal(tab.chain_row_selected, timeout=1000):
            idx = tab._model.index(0, 3)  # Last col — triggers normal order entry
            tab._on_double_click(idx)

class TestChainTabCallPutLabels:
    """Verify CALL / PUT visual labels are present."""

    def test_calls_label_exists(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        # The labels are created in _setup_ui; check table header area via children
        labels = tab.findChildren(type(tab._lbl_status))
        texts = [l.text() for l in labels]
        assert any("CALL" in t.upper() for t in texts)

    def test_puts_label_exists(self, qtbot, mock_engine):
        tab = ChainTab(mock_engine)
        qtbot.addWidget(tab)
        labels = tab.findChildren(type(tab._lbl_status))
        texts = [l.text() for l in labels]
        assert any("PUT" in t.upper() for t in texts)
