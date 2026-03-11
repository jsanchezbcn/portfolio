"""desktop/tests/test_order_entry.py — Tests for the Order Entry panel.

Verifies:
  - Form field defaults
  - Limit price enable/disable based on order type
  - prefill_from_chain populates all fields correctly
  - Build leg dict structure
"""
from __future__ import annotations

from desktop.ui.order_entry import OrderEntryPanel, _net_prices
from desktop.engine.ib_engine import ChainRow


def _make_chain_row(**overrides) -> ChainRow:
    defaults = dict(
        underlying="ES",
        expiry="20260320",
        strike=5500.0,
        right="C",
        conid=55001,
        bid=10.0,
        ask=12.0,
        last=11.0,
        volume=100,
        open_interest=500,
        iv=0.18,
        delta=0.35,
        gamma=0.01,
        theta=-5.0,
        vega=12.0,
    )
    defaults.update(overrides)
    return ChainRow(**defaults)


class TestOrderEntryLayout:
    """Verify Order Entry panel structure."""

    def test_creates_without_crash(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

    def test_has_symbol_field(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        assert panel._txt_symbol is not None

    def test_has_quantity_spinner(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        assert panel._spn_qty.value() == 1
        assert panel._spn_qty.minimum() == 1
        assert panel._spn_qty.maximum() == 999

    def test_has_action_combo(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        items = [panel._cmb_action.itemText(i) for i in range(panel._cmb_action.count())]
        assert "BUY" in items
        assert "SELL" in items

    def test_has_order_type_combo(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        items = [panel._cmb_order_type.itemText(i) for i in range(panel._cmb_order_type.count())]
        assert "LIMIT" in items
        assert "MARKET" in items

    def test_has_sec_type_combo(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        items = [panel._cmb_sec_type.itemText(i) for i in range(panel._cmb_sec_type.count())]
        assert "FOP" in items
        assert "OPT" in items
        assert "STK" in items
        assert "FUT" in items

    def test_has_strike_spinner(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        assert panel._spn_strike is not None

    def test_has_right_combo(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        items = [panel._cmb_right.itemText(i) for i in range(panel._cmb_right.count())]
        assert "C" in items
        assert "P" in items

    def test_has_expiry_field(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        assert panel._txt_expiry is not None

    def test_has_whatif_button(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        assert "WhatIf" in panel._btn_whatif.text()

    def test_has_submit_button(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        assert "SUBMIT" in panel._btn_submit.text()

    def test_has_rationale_field(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        assert panel._txt_rationale is not None


class TestOrderEntryBehavior:
    """Test interactive behavior."""

    def test_limit_price_disabled_for_market(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        panel._cmb_order_type.setCurrentText("MARKET")
        assert not panel._spn_limit.isEnabled()

    def test_limit_price_enabled_for_limit(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        panel._cmb_order_type.setCurrentText("MARKET")
        panel._cmb_order_type.setCurrentText("LIMIT")
        assert panel._spn_limit.isEnabled()

    def test_build_leg_returns_correct_dict(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        panel._txt_symbol.setText("ES")
        panel._cmb_sec_type.setCurrentText("FOP")
        panel._cmb_exchange.setCurrentText("CME")
        panel._cmb_action.setCurrentText("BUY")
        panel._spn_qty.setValue(2)
        panel._spn_strike.setValue(5500)
        panel._cmb_right.setCurrentText("C")
        panel._txt_expiry.setText("20260320")

        leg = panel._build_leg()

        assert leg["symbol"] == "ES"
        assert leg["sec_type"] == "FOP"
        assert leg["exchange"] == "CME"
        assert leg["action"] == "BUY"
        assert leg["qty"] == 2
        assert leg["strike"] == 5500
        assert leg["right"] == "C"
        assert leg["expiry"] == "20260320"

    def test_net_price_not_scaled_by_order_quantity(self):
        """A 5-lot single option still uses per-contract price in ticket limit."""
        legs = [{"action": "BUY", "qty": 5, "sec_type": "OPT", "symbol": "SPY"}]
        bid_ask = [{"bid": 0.09, "ask": 0.11}]
        lo, hi, mid = _net_prices(legs, bid_ask)
        assert lo == 0.09
        assert hi == 0.11
        assert mid == 0.1

    def test_symbol_search_resolves_slash_alias(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        panel._txt_symbol.setText("/es")
        panel._on_symbol_search()

        assert panel._txt_symbol.text() == "ES"
        assert panel._cmb_sec_type.currentText() == "FOP"
        assert panel._cmb_exchange.currentText() == "CME"


class TestOrderEntryPrefill:
    """Test prefill_from_chain populates all fields."""

    def test_prefill_es_call(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        cr = _make_chain_row(underlying="ES", strike=5500.0, right="C", bid=10.0, ask=12.0)
        panel.prefill_from_chain(cr)

        # prefill_from_chain stages in _staged_legs; check staged leg content
        assert len(panel._staged_legs) == 1
        leg = panel._staged_legs[0]
        assert leg["symbol"] == "ES"
        assert leg["strike"] == 5500.0
        assert leg["right"] == "C"
        assert leg["expiry"] == "20260320"
        assert leg["sec_type"] == "FOP"
        assert leg["exchange"] == "CME"

    def test_prefill_sets_mid_price(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        cr = _make_chain_row(bid=10.0, ask=12.0)
        panel.prefill_from_chain(cr)

        assert panel._spn_limit.value() == 11.0  # mid of 10 and 12

    def test_prefill_spy_uses_opt_smart(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        cr = _make_chain_row(underlying="SPY")
        panel.prefill_from_chain(cr)

        leg = panel._staged_legs[0]
        assert leg["sec_type"] == "OPT"
        assert leg["exchange"] == "SMART"

    def test_prefill_mes_uses_fop_cme(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        cr = _make_chain_row(underlying="MES")
        panel.prefill_from_chain(cr)

        assert panel._cmb_sec_type.currentText() == "FOP"
        assert panel._cmb_exchange.currentText() == "CME"

    def test_prefill_put(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        cr = _make_chain_row(right="P", strike=5600.0)
        panel.prefill_from_chain(cr)

        leg = panel._staged_legs[0]
        assert leg["right"] == "P"
        assert leg["strike"] == 5600.0


class TestOrderEntryAutoRefresh:
    """Test 5-second bid/ask auto-refresh timer and guard logic."""

    def test_auto_refresh_timer_exists(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        assert panel._auto_refresh_timer is not None

    def test_auto_refresh_timer_interval_is_5s(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        assert panel._auto_refresh_timer.interval() == 5_000

    def test_on_auto_refresh_does_nothing_without_legs(self, qtbot, mock_engine):
        """Auto-refresh should be a no-op when no legs are staged."""
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        assert not panel._staged_legs
        # Should not raise and not set _refresh_running
        panel._on_auto_refresh()
        assert not panel._refresh_running

    def test_on_auto_refresh_does_nothing_when_already_running(self, qtbot, mock_engine):
        """Guard prevents concurrent refreshes."""
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        panel._staged_legs = [{"symbol": "ES", "sec_type": "FOP",
                               "exchange": "CME", "action": "BUY",
                               "qty": 1, "strike": 5500.0, "right": "C",
                               "expiry": "20260320", "conid": 0}]
        panel._refresh_running = True
        # Should be a no-op — no task created
        panel._on_auto_refresh()
        # Guard should still be True (unchanged)
        assert panel._refresh_running

    def test_refresh_running_starts_false(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)
        assert panel._refresh_running is False

    def test_selected_bid_price_survives_refresh(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        cr = _make_chain_row(bid=10.0, ask=12.0)
        panel.prefill_from_chain(cr)
        panel._spn_limit.setValue(10.0)

        panel._bid_ask = [{"bid": 9.5, "ask": 11.5, "mid": 10.5}]
        panel._update_price_controls()

        assert panel._spn_limit.value() == 9.5


class TestOrderEntryAddChainLeg:
    """Test add_chain_leg appends a leg without clearing existing ones."""

    def test_add_chain_leg_appends_to_staged(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        # Pre-stage one leg via prefill
        cr1 = _make_chain_row(underlying="ES", strike=5500.0, right="C")
        panel.prefill_from_chain(cr1)
        assert len(panel._staged_legs) == 1

        # Add a second leg via add_chain_leg
        cr2 = _make_chain_row(underlying="ES", strike=5400.0, right="P")
        panel.add_chain_leg(cr2, "SELL")
        assert len(panel._staged_legs) == 2

    def test_add_chain_leg_preserves_action(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        cr = _make_chain_row(underlying="ES", strike=5500.0, right="C")
        panel.add_chain_leg(cr, "SELL")

        assert panel._staged_legs[0]["action"] == "SELL"

    def test_add_chain_leg_sets_sec_type_fop_for_es(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        cr = _make_chain_row(underlying="ES")
        panel.add_chain_leg(cr, "BUY")

        assert panel._staged_legs[0]["sec_type"] == "FOP"

    def test_add_chain_leg_sets_sec_type_opt_for_spy(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        cr = _make_chain_row(underlying="SPY")
        panel.add_chain_leg(cr, "BUY")

        assert panel._staged_legs[0]["sec_type"] == "OPT"

    def test_add_chain_leg_updates_bid_ask(self, qtbot, mock_engine):
        panel = OrderEntryPanel(mock_engine)
        qtbot.addWidget(panel)

        cr = _make_chain_row(bid=5.0, ask=7.0)
        panel.add_chain_leg(cr, "BUY")

        ba = panel._bid_ask[0]
        assert ba["bid"] == 5.0
        assert ba["ask"] == 7.0
        assert ba["mid"] == 6.0
