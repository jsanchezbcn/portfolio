"""tests/test_order_builder.py — Unit tests for models/order.py dataclasses.

Covers T011 of 003-algo-execution-platform:
  - Order max-4-leg validation
  - OrderStatus FSM (valid + invalid transitions)
  - SimulationResult structure
  - PortfolioGreeks computed properties (delta_theta_ratio, sebastian_ratio)
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from models.order import (
    AITradeSuggestion,
    Order,
    OrderAction,
    OrderLeg,
    OrderStatus,
    OrderType,
    OptionRight,
    PortfolioGreeks,
    SimulationResult,
    TradeJournalEntry,
    validate_status_transition,
)


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _make_leg(**kw) -> OrderLeg:
    defaults = dict(
        symbol="SPX",
        action=OrderAction.BUY,
        quantity=1,
        option_right=OptionRight.CALL,
        strike=4500.0,
        expiration=date(2025, 12, 19),
        conid=None,
        fill_price=None,
    )
    defaults.update(kw)
    return OrderLeg(**defaults)


def _make_order(n_legs: int = 1, **kw) -> Order:
    legs = [_make_leg(symbol=f"SPX_{i}") for i in range(n_legs)]
    defaults = dict(
        legs=legs,
        order_type=OrderType.LIMIT,
        status=OrderStatus.DRAFT,
    )
    defaults.update(kw)
    return Order(**defaults)


# ------------------------------------------------------------------ #
# Order leg count validation                                          #
# ------------------------------------------------------------------ #

class TestOrderLegValidation:
    def test_order_max_4_legs_raises(self):
        with pytest.raises(ValueError, match="max.*4|4.*leg"):
            _make_order(n_legs=5)

    def test_order_4_legs_ok(self):
        order = _make_order(n_legs=4)
        assert len(order.legs) == 4

    def test_order_1_leg_ok(self):
        order = _make_order(n_legs=1)
        assert len(order.legs) == 1

    def test_order_zero_legs_raises(self):
        with pytest.raises((ValueError, TypeError)):
            _make_order(n_legs=0)


# ------------------------------------------------------------------ #
# OrderStatus FSM                                                     #
# ------------------------------------------------------------------ #

class TestOrderStatusFSM:
    def test_valid_draft_to_simulated(self):
        validate_status_transition(OrderStatus.DRAFT, OrderStatus.SIMULATED)  # no raise

    def test_valid_simulated_to_pending(self):
        validate_status_transition(OrderStatus.SIMULATED, OrderStatus.PENDING)

    def test_valid_pending_to_filled(self):
        validate_status_transition(OrderStatus.PENDING, OrderStatus.FILLED)

    def test_valid_pending_to_partial(self):
        validate_status_transition(OrderStatus.PENDING, OrderStatus.PARTIAL)

    def test_valid_pending_to_rejected(self):
        validate_status_transition(OrderStatus.PENDING, OrderStatus.REJECTED)

    def test_valid_pending_to_cancelled(self):
        validate_status_transition(OrderStatus.PENDING, OrderStatus.CANCELLED)

    def test_invalid_draft_to_filled_raises(self):
        with pytest.raises(ValueError):
            validate_status_transition(OrderStatus.DRAFT, OrderStatus.FILLED)

    def test_invalid_filled_to_cancelled_raises(self):
        with pytest.raises(ValueError):
            validate_status_transition(OrderStatus.FILLED, OrderStatus.CANCELLED)

    def test_transition_to_updates_status(self):
        order = _make_order()
        order.transition_to(OrderStatus.SIMULATED)
        assert order.status == OrderStatus.SIMULATED

    def test_transition_to_invalid_raises(self):
        order = _make_order()
        with pytest.raises(ValueError):
            order.transition_to(OrderStatus.FILLED)  # DRAFT → FILLED is invalid


# ------------------------------------------------------------------ #
# Order properties                                                    #
# ------------------------------------------------------------------ #

class TestOrderProperties:
    def test_is_multi_leg_false_for_one_leg(self):
        assert _make_order(n_legs=1).is_multi_leg is False

    def test_is_multi_leg_true_for_two_legs(self):
        assert _make_order(n_legs=2).is_multi_leg is True

    def test_has_option_legs_true_when_strike_present(self):
        order = _make_order(n_legs=1)
        assert order.has_option_legs is True

    def test_has_option_legs_false_when_no_strike(self):
        equity_leg = _make_leg(strike=None, option_right=None, expiration=None)
        order = Order(legs=[equity_leg], order_type=OrderType.MARKET, status=OrderStatus.DRAFT)
        assert order.has_option_legs is False


# ------------------------------------------------------------------ #
# SimulationResult                                                    #
# ------------------------------------------------------------------ #

class TestSimulationResult:
    def test_simulation_result_populated(self):
        greeks = PortfolioGreeks(
            spx_delta=-10.5,
            gamma=0.02,
            theta=-125.0,
            vega=320.0,
            timestamp=datetime.now(timezone.utc),
        )
        result = SimulationResult(
            margin_requirement=12_450.0,
            equity_before=120_000.0,
            equity_after=107_550.0,
            post_trade_greeks=greeks,
            delta_breach=False,
            error=None,
        )
        assert result.margin_requirement == 12_450.0
        assert result.post_trade_greeks.spx_delta == -10.5
        assert result.error is None

    def test_simulation_result_error_flag(self):
        result = SimulationResult(
            margin_requirement=None,
            equity_before=None,
            equity_after=None,
            post_trade_greeks=None,
            delta_breach=False,
            error="Insufficient margin",
        )
        assert result.error == "Insufficient margin"
        assert result.margin_requirement is None


# ------------------------------------------------------------------ #
# PortfolioGreeks computed properties                                 #
# ------------------------------------------------------------------ #

class TestPortfolioGreeksProperties:
    def test_delta_theta_ratio_normal(self):
        g = PortfolioGreeks(
            spx_delta=-10.0,
            gamma=0.0,
            theta=-100.0,
            vega=0.0,
            timestamp=datetime.now(timezone.utc),
        )
        # theta / spx_delta → -100 / -10 = 10.0
        assert g.delta_theta_ratio == pytest.approx(10.0)

    def test_delta_theta_ratio_none_when_zero_delta(self):
        g = PortfolioGreeks(
            spx_delta=0.0,
            gamma=0.0,
            theta=-80.0,
            vega=100.0,
            timestamp=datetime.now(timezone.utc),
        )
        assert g.delta_theta_ratio is None

    def test_sebastian_ratio_normal(self):
        """Sebastian ratio = |theta| / |vega|."""
        g = PortfolioGreeks(
            spx_delta=-5.0,
            gamma=0.0,
            theta=-90.0,
            vega=300.0,
            timestamp=datetime.now(timezone.utc),
        )
        assert g.sebastian_ratio == pytest.approx(0.30)

    def test_sebastian_ratio_none_when_zero_vega(self):
        g = PortfolioGreeks(
            spx_delta=-5.0,
            gamma=0.0,
            theta=-90.0,
            vega=0.0,
            timestamp=datetime.now(timezone.utc),
        )
        assert g.sebastian_ratio is None

    def test_sebastian_ratio_none_when_zero_theta(self):
        g = PortfolioGreeks(
            spx_delta=-5.0,
            gamma=0.0,
            theta=0.0,
            vega=100.0,
            timestamp=datetime.now(timezone.utc),
        )
        assert g.sebastian_ratio is None


# ------------------------------------------------------------------ #
# TradeJournalEntry (smoke)                                          #
# ------------------------------------------------------------------ #

class TestTradeJournalEntry:
    def test_entry_creation(self):
        entry = TradeJournalEntry(
            broker="IBKR",
            account_id="U1234567",
            underlying="SPX",
            legs_json="[]",
        )
        assert entry.status == "FILLED"
        assert len(entry.entry_id) == 36  # UUID format
        assert entry.broker == "IBKR"
