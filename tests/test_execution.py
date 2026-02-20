"""tests/test_execution.py — Unit tests for core/execution.py ExecutionEngine.

Covers:
  - simulate() success path (T021)
  - simulate() error paths: timeout, HTTP 503, connection error (T021)
  - simulate() delta breach detection (T021 / T024)
  - submit() stub raises NotImplementedError — safety contract (T029 placeholder)

These tests are written FIRST (TDD). They FAIL until T022–T024 are implemented.

SAFETY NOTE: No live orders are transmitted in any test. submit() is intentionally
not tested here beyond its stub contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import requests

from models.order import (
    Order,
    OrderLeg,
    OrderAction,
    OrderStatus,
    OrderType,
    PortfolioGreeks,
    SimulationResult,
)
from core.execution import ExecutionEngine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_whatif_response.json"
WHATIF_RESPONSE: dict = json.loads(FIXTURE_PATH.read_text())


def _make_order(n_legs: int = 2) -> Order:
    """Build a minimal test order with 1–2 SPX legs."""
    legs = [
        OrderLeg(symbol="SPX", action=OrderAction.SELL, quantity=1, conid="416904"),
    ]
    if n_legs >= 2:
        legs.append(
            OrderLeg(symbol="SPX", action=OrderAction.BUY, quantity=1, conid="416905")
        )
    return Order(legs=legs[:n_legs], order_type=OrderType.LIMIT)


def _make_engine(http_response=None, http_exception=None) -> tuple[ExecutionEngine, MagicMock]:
    """
    Build ExecutionEngine with a mocked IBKRClient session.

    Pass http_response to mock a successful/error HTTP response, or
    http_exception to make the session.post raise an exception.
    """
    mock_client = MagicMock()
    mock_client.base_url = "https://localhost:5001"

    if http_exception is not None:
        mock_client.session.post.side_effect = http_exception
    elif http_response is not None:
        mock_client.session.post.return_value = http_response

    mock_store = MagicMock()
    mock_weighter = MagicMock()

    engine = ExecutionEngine(
        ibkr_gateway_client=mock_client,
        local_store=mock_store,
        beta_weighter=mock_weighter,
    )
    return engine, mock_client


def _success_response(data: dict | None = None) -> MagicMock:
    """Return a mock HTTP 200 response with the given JSON payload."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = data if data is not None else WHATIF_RESPONSE
    return resp


# ===========================================================================
# simulate() — SUCCESS PATH
# ===========================================================================


class TestSimulateSuccess:
    """simulate() parses the WhatIf response into a well-formed SimulationResult."""

    def test_returns_simulation_result_instance(self):
        """simulate() returns a SimulationResult object."""
        engine, _ = _make_engine(http_response=_success_response())
        result = engine.simulate("U12345", _make_order(), PortfolioGreeks())
        assert isinstance(result, SimulationResult)

    def test_margin_requirement_parsed_correctly(self):
        """margin_requirement matches fixture amount.amount (12450.00)."""
        engine, _ = _make_engine(http_response=_success_response())
        result = engine.simulate("U12345", _make_order(), PortfolioGreeks())
        assert result.margin_requirement == 12450.00

    def test_equity_before_parsed_correctly(self):
        """equity_before matches fixture equity.current (145320.50)."""
        engine, _ = _make_engine(http_response=_success_response())
        result = engine.simulate("U12345", _make_order(), PortfolioGreeks())
        assert result.equity_before == 145320.50

    def test_equity_after_parsed_correctly(self):
        """equity_after matches fixture equity.projected (132870.50)."""
        engine, _ = _make_engine(http_response=_success_response())
        result = engine.simulate("U12345", _make_order(), PortfolioGreeks())
        assert result.equity_after == 132870.50

    def test_error_is_none_on_success(self):
        """error field is None on a successful simulation."""
        engine, _ = _make_engine(http_response=_success_response())
        result = engine.simulate("U12345", _make_order(), PortfolioGreeks())
        assert result.error is None

    def test_post_trade_greeks_populated(self):
        """post_trade_greeks is a PortfolioGreeks instance on success."""
        engine, _ = _make_engine(http_response=_success_response())
        result = engine.simulate("U12345", _make_order(), PortfolioGreeks(spx_delta=100.0))
        assert result.post_trade_greeks is not None
        assert isinstance(result.post_trade_greeks, PortfolioGreeks)

    def test_order_transitions_to_simulated_on_success(self):
        """Order status advances to SIMULATED after a successful simulate() call."""
        engine, _ = _make_engine(http_response=_success_response())
        order = _make_order()
        engine.simulate("U12345", order, PortfolioGreeks())
        assert order.status == OrderStatus.SIMULATED

    def test_simulation_result_stored_on_order(self):
        """order.simulation_result is set after a successful simulate()."""
        engine, _ = _make_engine(http_response=_success_response())
        order = _make_order()
        result = engine.simulate("U12345", order, PortfolioGreeks())
        assert order.simulation_result is result

    def test_simulate_calls_whatif_endpoint_only(self):
        """simulate() ONLY calls the /orders/whatif endpoint — never /orders directly."""
        engine, mock_client = _make_engine(http_response=_success_response())
        engine.simulate("U12345", _make_order(), PortfolioGreeks())

        assert mock_client.session.post.call_count == 1
        called_url = mock_client.session.post.call_args.args[0]
        assert "whatif" in called_url, (
            f"simulate() must only hit the WhatIf endpoint. Got: {called_url}"
        )

    def test_simulate_passes_account_id_in_url(self):
        """Account ID is embedded in the WhatIf URL path."""
        engine, mock_client = _make_engine(http_response=_success_response())
        engine.simulate("UTEST99", _make_order(), PortfolioGreeks())
        called_url = mock_client.session.post.call_args.args[0]
        assert "UTEST99" in called_url

    def test_simulate_multi_leg_payload_has_all_legs(self):
        """WhatIf payload contains one entry per order leg."""
        engine, mock_client = _make_engine(http_response=_success_response())
        order = _make_order(n_legs=2)
        engine.simulate("U12345", order, PortfolioGreeks())

        payload = mock_client.session.post.call_args.kwargs.get("json") or \
                  mock_client.session.post.call_args[1].get("json")
        assert payload is not None
        assert len(payload["orders"]) == 2


# ===========================================================================
# simulate() — ERROR PATHS
# ===========================================================================


class TestSimulateErrors:
    """simulate() must return SimulationResult(error=...) for all failure modes.
    It must NEVER raise an exception and must NEVER submit a live order.
    """

    def test_timeout_returns_error_no_exception(self):
        """requests.Timeout → SimulationResult.error set, no exception raised."""
        engine, _ = _make_engine(http_exception=requests.exceptions.Timeout("timed out"))
        result = engine.simulate("U12345", _make_order(), PortfolioGreeks())

        assert isinstance(result, SimulationResult)
        assert result.error is not None
        assert "timeout" in result.error.lower() or "timed out" in result.error.lower()

    def test_timeout_margin_is_none(self):
        """On timeout, margin_requirement is None (no data received)."""
        engine, _ = _make_engine(http_exception=requests.exceptions.Timeout())
        result = engine.simulate("U12345", _make_order(), PortfolioGreeks())
        assert result.margin_requirement is None

    def test_broker_503_returns_error(self):
        """HTTP 503 → SimulationResult.error set, no exception raised."""
        resp = MagicMock()
        resp.status_code = 503
        resp.text = "Service Unavailable"
        engine, _ = _make_engine(http_response=resp)

        result = engine.simulate("U12345", _make_order(), PortfolioGreeks())

        assert isinstance(result, SimulationResult)
        assert result.error is not None
        assert result.margin_requirement is None

    def test_broker_503_includes_status_code_in_error(self):
        """Error message for non-200 includes the HTTP status code."""
        resp = MagicMock()
        resp.status_code = 503
        resp.text = "Service Unavailable"
        engine, _ = _make_engine(http_response=resp)

        result = engine.simulate("U12345", _make_order(), PortfolioGreeks())
        assert "503" in result.error

    def test_connection_error_returns_error(self):
        """requests.ConnectionError → SimulationResult.error set, no exception."""
        engine, _ = _make_engine(
            http_exception=requests.exceptions.ConnectionError("connection refused")
        )
        result = engine.simulate("U12345", _make_order(), PortfolioGreeks())

        assert isinstance(result, SimulationResult)
        assert result.error is not None
        assert result.margin_requirement is None

    def test_error_result_order_stays_draft(self):
        """When simulate() errors, order.status remains DRAFT (not SIMULATED)."""
        engine, _ = _make_engine(http_exception=requests.exceptions.Timeout())
        order = _make_order()
        engine.simulate("U12345", order, PortfolioGreeks())
        assert order.status == OrderStatus.DRAFT, (
            "Order must stay DRAFT when simulation fails — must not advance to SIMULATED"
        )

    def test_broker_error_field_propagates(self):
        """Broker-reported 'error' field in WhatIf response → SimulationResult.error."""
        data_with_error = dict(WHATIF_RESPONSE)
        data_with_error["error"] = "Insufficient margin"
        engine, _ = _make_engine(http_response=_success_response(data_with_error))

        result = engine.simulate("U12345", _make_order(), PortfolioGreeks())
        assert result.error is not None
        assert "Insufficient margin" in result.error

    def test_error_result_does_not_call_submit(self):
        """On error, simulate() does not advance to any order-submission endpoint."""
        engine, mock_client = _make_engine(http_exception=requests.exceptions.Timeout())
        engine.simulate("U12345", _make_order(), PortfolioGreeks())

        # Only one call was made (the whatif), and it raised Timeout
        assert mock_client.session.post.call_count == 1
        called_url = mock_client.session.post.call_args.args[0]
        assert "whatif" in called_url


# ===========================================================================
# simulate() — DELTA BREACH DETECTION (T024)
# ===========================================================================


class TestDeltaBreach:
    """delta_breach is True when abs(post_trade_greeks.spx_delta) > regime limit."""

    def test_breach_when_post_trade_delta_exceeds_neutral_limit(self):
        """delta_breach=True when post-trade |delta| > 300 (neutral_volatility default)."""
        engine, _ = _make_engine(http_response=_success_response())
        current_greeks = PortfolioGreeks(spx_delta=100.0)

        with patch.object(engine, "_compute_post_trade_greeks") as mock_greeks:
            mock_greeks.return_value = PortfolioGreeks(spx_delta=320.0)  # > 300 limit
            result = engine.simulate(
                "U12345", _make_order(), current_greeks, regime="neutral_volatility"
            )

        assert result.delta_breach is True

    def test_no_breach_within_neutral_limit(self):
        """delta_breach=False when post-trade |delta| <= 300."""
        engine, _ = _make_engine(http_response=_success_response())

        with patch.object(engine, "_compute_post_trade_greeks") as mock_greeks:
            mock_greeks.return_value = PortfolioGreeks(spx_delta=150.0)  # well within 300
            result = engine.simulate("U12345", _make_order(), PortfolioGreeks())

        assert result.delta_breach is False

    def test_breach_negative_direction(self):
        """delta_breach=True for short delta > 300 (abs check applies)."""
        engine, _ = _make_engine(http_response=_success_response())

        with patch.object(engine, "_compute_post_trade_greeks") as mock_greeks:
            mock_greeks.return_value = PortfolioGreeks(spx_delta=-350.0)  # abs > 300
            result = engine.simulate("U12345", _make_order(), PortfolioGreeks())

        assert result.delta_breach is True

    def test_breach_at_exactly_limit_is_not_breach(self):
        """delta_breach=False when post-trade delta exactly equals the limit (not strictly greater)."""
        engine, _ = _make_engine(http_response=_success_response())

        with patch.object(engine, "_compute_post_trade_greeks") as mock_greeks:
            mock_greeks.return_value = PortfolioGreeks(spx_delta=300.0)  # exactly at limit
            result = engine.simulate(
                "U12345", _make_order(), PortfolioGreeks(), regime="neutral_volatility"
            )

        assert result.delta_breach is False

    def test_breach_uses_high_vol_limit(self):
        """In high_volatility regime, limit is 75 — breach at 80."""
        engine, _ = _make_engine(http_response=_success_response())

        with patch.object(engine, "_compute_post_trade_greeks") as mock_greeks:
            mock_greeks.return_value = PortfolioGreeks(spx_delta=80.0)  # > 75 high_vol limit
            result = engine.simulate(
                "U12345", _make_order(), PortfolioGreeks(), regime="high_volatility"
            )

        assert result.delta_breach is True

    def test_no_breach_high_vol_within_limit(self):
        """In high_volatility regime, delta 60 is within the 75 limit."""
        engine, _ = _make_engine(http_response=_success_response())

        with patch.object(engine, "_compute_post_trade_greeks") as mock_greeks:
            mock_greeks.return_value = PortfolioGreeks(spx_delta=60.0)
            result = engine.simulate(
                "U12345", _make_order(), PortfolioGreeks(), regime="high_volatility"
            )

        assert result.delta_breach is False

    def test_breach_crisis_mode(self):
        """In crisis_mode, limit is 0 — any non-zero delta is a breach."""
        engine, _ = _make_engine(http_response=_success_response())

        with patch.object(engine, "_compute_post_trade_greeks") as mock_greeks:
            mock_greeks.return_value = PortfolioGreeks(spx_delta=5.0)  # > 0 crisis limit
            result = engine.simulate(
                "U12345", _make_order(), PortfolioGreeks(), regime="crisis_mode"
            )

        assert result.delta_breach is True

    def test_breach_result_has_post_trade_greeks(self):
        """Even when breaching, post_trade_greeks is populated in the result."""
        engine, _ = _make_engine(http_response=_success_response())

        with patch.object(engine, "_compute_post_trade_greeks") as mock_greeks:
            mock_greeks.return_value = PortfolioGreeks(spx_delta=400.0)
            result = engine.simulate("U12345", _make_order(), PortfolioGreeks())

        assert result.post_trade_greeks is not None
        assert result.post_trade_greeks.spx_delta == 400.0


# ===========================================================================
# submit() — Safety contract stub (T029 placeholder)
# ===========================================================================


class TestSubmitStub:
    """Safety contract: DRAFT order must be rejected; flatten_risk() still a stub."""

    def test_draft_order_raises_value_error(self):
        """submit() rejects DRAFT orders — order must be SIMULATED before submission."""
        engine, mock_client = _make_engine()
        order = _make_order()  # starts in DRAFT

        with pytest.raises(ValueError, match="SIMULATED"):
            engine.submit("U12345", order)

        # Broker endpoint MUST NOT be called for DRAFT orders
        assert not mock_client.session.post.called

    def test_flatten_risk_raises_not_implemented(self):
        """flatten_risk() now implemented (T067) — verify basic empty list return."""
        engine, _ = _make_engine()
        # No positions → empty list (T068: no short positions case)
        result = engine.flatten_risk([])
        assert result == []


# ===========================================================================
# submit() — Behaviour tests (T029)
# These tests describe the REQUIRED behaviour once T030 is implemented.
# They ALL FAIL until T030 is complete — that is intentional (TDD red phase).
# DO NOT remove the NotImplementedError guards; they flip to assertions at T030.
# ===========================================================================


class TestSubmitBehavior:
    """Acceptance tests for ExecutionEngine.submit() (T030).

    All broker calls are mocked — no live orders transmitted.
    _poll_order_status is patched to avoid 30-second polling loops.

    ⚠ SAFETY REMINDER: submit() must only ever be called after an explicit
    2-step user confirmation in the UI.
    """

    # ------------------------------------------------------------------
    # Helpers for submit() tests
    # ------------------------------------------------------------------

    @staticmethod
    def _make_simulated_order(n_legs: int = 2) -> Order:
        """Build an order already in SIMULATED state (ready to submit)."""
        order = _make_order(n_legs=n_legs)
        # Manually advance FSM to SIMULATED (bypassing simulate() call)
        order.status = OrderStatus.SIMULATED
        order.simulation_result = SimulationResult(
            margin_requirement=1000.0,
            equity_before=50000.0,
            equity_after=49000.0,
            post_trade_greeks=PortfolioGreeks(),
            delta_breach=False,
        )
        return order

    @staticmethod
    def _submit_response(order_id: str = "IBKR-001") -> MagicMock:
        """Mock HTTP 200 response from /orders endpoint."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{"order_id": order_id}]
        return resp

    # ------------------------------------------------------------------
    # Happy-path submit tests (T030)
    # ------------------------------------------------------------------

    def test_submit_transmits_order_to_ibkr(self):
        """Confirmed submit() calls POST /orders — not /orders/whatif."""
        resp = self._submit_response()
        engine, mock_client = _make_engine(http_response=resp)
        order = self._make_simulated_order()

        with patch.object(engine, "_poll_order_status", return_value="Filled"):
            engine.submit("U12345", order)

        assert mock_client.session.post.called
        called_url = mock_client.session.post.call_args.args[0]
        assert "/orders" in called_url
        assert "whatif" not in called_url

    def test_submit_advances_order_to_filled_after_poll(self):
        """After submit() + poll confirms fill, order.status is FILLED."""
        resp = self._submit_response()
        engine, _ = _make_engine(http_response=resp)
        order = self._make_simulated_order()

        with patch.object(engine, "_poll_order_status", return_value="Filled"):
            engine.submit("U12345", order)

        assert order.status == OrderStatus.FILLED

    def test_submit_stores_broker_order_id(self):
        """broker_order_id is populated after a successful submit()."""
        resp = self._submit_response(order_id="IBKR-TEST-42")
        engine, _ = _make_engine(http_response=resp)
        order = self._make_simulated_order()

        with patch.object(engine, "_poll_order_status", return_value="Filled"):
            engine.submit("U12345", order)

        assert order.broker_order_id == "IBKR-TEST-42"

    def test_submit_multi_leg_includes_all_legs(self):
        """Multi-leg combo order uses BAG format: 1 order entry with comboLegs covering all legs."""
        resp = self._submit_response()
        engine, mock_client = _make_engine(http_response=resp)
        order = self._make_simulated_order(n_legs=2)
        order.legs[0].conid = "265598"
        order.legs[1].conid = "265599"

        with patch.object(engine, "_poll_order_status", return_value="Filled"):
            engine.submit("U12345", order)

        payload = (
            mock_client.session.post.call_args.kwargs.get("json")
            or mock_client.session.post.call_args[1].get("json")
        )
        assert payload is not None
        orders_in_payload = payload.get("orders", [])
        # T032: multi-leg with all conids → single BAG entry with comboLegs
        assert len(orders_in_payload) == 1, "Multi-leg with conids must use single BAG order entry"
        bag_entry = orders_in_payload[0]
        assert bag_entry.get("secType") == "BAG", "BAG secType required for combo order"
        assert "comboLegs" in bag_entry, "comboLegs must be present in BAG order"
        assert len(bag_entry["comboLegs"]) == 2, "All 2 legs must appear in comboLegs"
        conids_in_combo = {str(leg["conid"]) for leg in bag_entry["comboLegs"]}
        assert "265598" in conids_in_combo
        assert "265599" in conids_in_combo

    # ------------------------------------------------------------------
    # Safety: DRAFT order must NOT reach broker
    # ------------------------------------------------------------------

    def test_draft_order_cannot_be_submitted(self):
        """submit() must raise ValueError when order is still DRAFT."""
        resp = self._submit_response()
        engine, mock_client = _make_engine(http_response=resp)
        order = _make_order()  # DRAFT status — not yet simulated

        with pytest.raises(ValueError):
            engine.submit("U12345", order)

        # Broker endpoint must NOT have been called
        assert not mock_client.session.post.called

    # ------------------------------------------------------------------
    # Error paths: rejection, connection drop
    # ------------------------------------------------------------------

    def test_broker_rejection_surfaces_reason(self):
        """Broker HTTP error returns Order with REJECTED status."""
        resp = MagicMock()
        resp.status_code = 400
        resp.json.return_value = {"error": "Insufficient buying power"}
        engine, _ = _make_engine(http_response=resp)
        order = self._make_simulated_order()

        result_order = engine.submit("U12345", order)

        assert result_order.status == OrderStatus.REJECTED

    def test_connection_drop_mid_order_returns_unknown(self):
        """Connection drop during order submission returns Order without raising.

        The order may or may not have reached the broker — user must verify
        in the IBKR platform directly.  Status is left as PENDING.
        """
        engine, mock_client = _make_engine(
            http_exception=requests.exceptions.ConnectionError("connection reset")
        )
        order = self._make_simulated_order()

        # Should not raise — must return an Order
        result_order = engine.submit("U12345", order)
        assert result_order is not None
        # PENDING because we couldn't confirm delivery
        assert result_order.status == OrderStatus.PENDING


# ===========================================================================
# T066: flatten_risk() — TDD tests (must FAIL before T067-T068 are implemented)
# ===========================================================================

from datetime import date

from models.unified_position import UnifiedPosition, InstrumentType


def _make_short_put(symbol: str = "SPX_put", qty: float = -2.0) -> UnifiedPosition:
    """A short put position (qty < 0, instrument_type == OPTION)."""
    return UnifiedPosition(
        symbol=symbol,
        instrument_type=InstrumentType.OPTION,
        broker="IBKR",
        quantity=qty,
        avg_price=5.0,
        market_value=qty * 500.0,
        unrealized_pnl=0.0,
        underlying="SPX",
        strike=4800.0,
        expiration=date(2025, 3, 21),
        option_type="put",
    )


def _make_long_call() -> UnifiedPosition:
    """A long call position (qty > 0, instrument_type == OPTION)."""
    return UnifiedPosition(
        symbol="SPX_call",
        instrument_type=InstrumentType.OPTION,
        broker="IBKR",
        quantity=1.0,
        avg_price=10.0,
        market_value=1000.0,
        unrealized_pnl=0.0,
        underlying="SPX",
        strike=5200.0,
        expiration=date(2025, 3, 21),
        option_type="call",
    )


def _make_futures_position() -> UnifiedPosition:
    """A short futures position (qty < 0, instrument_type == FUTURE)."""
    return UnifiedPosition(
        symbol="/ES",
        instrument_type=InstrumentType.FUTURE,
        broker="IBKR",
        quantity=-1.0,
        avg_price=5000.0,
        market_value=-5000.0,
        unrealized_pnl=0.0,
    )


def _make_equity_position() -> UnifiedPosition:
    """A short stock position (qty < 0, instrument_type == EQUITY)."""
    return UnifiedPosition(
        symbol="AAPL",
        instrument_type=InstrumentType.EQUITY,
        broker="IBKR",
        quantity=-10.0,
        avg_price=180.0,
        market_value=-1800.0,
        unrealized_pnl=0.0,
    )


class TestFlattenRisk:
    """T066 tests for ExecutionEngine.flatten_risk()."""

    def _make_engine(self) -> ExecutionEngine:
        mock_client = MagicMock()
        mock_client.base_url = "https://localhost:5001"
        mock_store = MagicMock()
        mock_weighter = MagicMock()
        return ExecutionEngine(
            ibkr_gateway_client=mock_client,
            local_store=mock_store,
            beta_weighter=mock_weighter,
        )

    def test_returns_buy_to_close_for_short_options(self):
        """flatten_risk() returns one BUY MARKET order per short option leg."""
        engine = self._make_engine()
        positions = [_make_short_put("SHORT_PUT_1"), _make_short_put("SHORT_PUT_2")]

        orders = engine.flatten_risk(positions)

        assert len(orders) == 2
        assert all(isinstance(o, Order) for o in orders)
        assert all(o.order_type == OrderType.MARKET for o in orders)
        assert all(o.legs[0].action == OrderAction.BUY for o in orders)

    def test_short_option_quantity_becomes_positive_buy(self):
        """flatten_risk() converts negative qty to positive for BUY orders."""
        engine = self._make_engine()
        positions = [_make_short_put(qty=-3.0)]

        orders = engine.flatten_risk(positions)

        assert len(orders) == 1
        assert orders[0].legs[0].quantity == 3  # positive int

    def test_long_options_excluded_from_flatten(self):
        """Long option positions must NOT be included in flatten."""
        engine = self._make_engine()
        positions = [_make_long_call()]

        orders = engine.flatten_risk(positions)

        assert orders == []

    def test_futures_excluded_from_flatten(self):
        """Short futures positions must NOT be included in flatten."""
        engine = self._make_engine()
        positions = [_make_futures_position()]

        orders = engine.flatten_risk(positions)

        assert orders == []

    def test_equity_excluded_from_flatten(self):
        """Short equity positions must NOT be included in flatten."""
        engine = self._make_engine()
        positions = [_make_equity_position()]

        orders = engine.flatten_risk(positions)

        assert orders == []

    def test_mixed_portfolio_only_short_options(self):
        """Only short option legs appear in flatten list; others excluded."""
        engine = self._make_engine()
        positions = [
            _make_short_put("SHORT_PUT"),
            _make_long_call(),
            _make_futures_position(),
            _make_equity_position(),
        ]

        orders = engine.flatten_risk(positions)

        assert len(orders) == 1
        assert orders[0].legs[0].symbol == "SHORT_PUT"

    def test_no_short_positions_returns_empty_list(self):
        """T068 — flatten_risk() returns [] when no short options exist."""
        engine = self._make_engine()
        positions = [_make_long_call(), _make_futures_position()]

        orders = engine.flatten_risk(positions)

        assert orders == []

    def test_empty_portfolio_returns_empty_list(self):
        """flatten_risk([]) returns empty list without raising."""
        engine = self._make_engine()

        orders = engine.flatten_risk([])

        assert orders == []

    def test_orders_not_transmitted(self):
        """flatten_risk() must NOT call the broker endpoint — returns dry orders."""
        engine = self._make_engine()
        positions = [_make_short_put()]

        engine.flatten_risk(positions)

        # No POST to broker should occur
        assert not engine._client.session.post.called

    def test_flatten_order_user_rationale(self):
        """Each flatten order has the standard rationale string."""
        engine = self._make_engine()
        positions = [_make_short_put()]

        orders = engine.flatten_risk(positions)

        assert "Flatten Risk" in orders[0].user_rationale

    def test_flatten_order_status_is_simulated(self):
        """Flattened orders are pre-approved (SIMULATED) status so submit() can be called directly."""
        engine = self._make_engine()
        positions = [_make_short_put()]

        orders = engine.flatten_risk(positions)

        assert orders[0].status == OrderStatus.SIMULATED
