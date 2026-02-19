"""
Tests for core/order_manager.py — User Story 1: Stage an order without transmitting.

TDD: these tests are written BEFORE the implementation and are expected to FAIL
until core/order_manager.py is created.

Test IDs:
- T008: OrderRequest Pydantic validation
- T009: stage_order() payload contains transmit=false + returns order ID
- T010: DB persistence — staged_orders record exists after stage_order()
- T011: Error path — unsupported instrument type raises ValueError before TWS call
- T012: Rollback path — no partial DB record when DB write fails
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from core.order_manager import OrderManager, OrderRequest


# ---------------------------------------------------------------------------
# T008 — OrderRequest Pydantic validation
# ---------------------------------------------------------------------------


class TestOrderRequest:
    """T008: Validate the OrderRequest Pydantic model."""

    def test_valid_stk(self) -> None:
        req = OrderRequest(
            instrument_type="STK",
            symbol="AAPL",
            quantity=10,
            direction="BUY",
            limit_price=175.50,
        )
        assert req.instrument_type == "STK"
        assert req.symbol == "AAPL"
        assert req.quantity == 10
        assert req.direction == "BUY"
        assert req.limit_price == 175.50

    def test_valid_fut_mes(self) -> None:
        req = OrderRequest(
            instrument_type="FUT",
            symbol="/MES",
            quantity=2,
            direction="SELL",
            limit_price=5100.00,
            expiration=date(2025, 12, 19),
        )
        assert req.instrument_type == "FUT"
        assert req.symbol == "/MES"
        assert req.expiration == date(2025, 12, 19)

    def test_invalid_instrument_type_raises(self) -> None:
        with pytest.raises(ValidationError):
            OrderRequest(
                instrument_type="OPT",   # not in Literal["STK","FUT"]
                symbol="AAPL",
                quantity=1,
                direction="BUY",
                limit_price=10.0,
            )

    def test_invalid_direction_raises(self) -> None:
        with pytest.raises(ValidationError):
            OrderRequest(
                instrument_type="STK",
                symbol="AAPL",
                quantity=1,
                direction="HOLD",  # not in Literal["BUY","SELL"]
                limit_price=10.0,
            )

    def test_quantity_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            OrderRequest(
                instrument_type="STK",
                symbol="AAPL",
                quantity=0,
                direction="BUY",
                limit_price=10.0,
            )

    def test_optional_fields_default_none(self) -> None:
        req = OrderRequest(
            instrument_type="STK",
            symbol="TSLA",
            quantity=5,
            direction="BUY",
            limit_price=None,
        )
        assert req.expiration is None
        assert req.strike is None
        assert req.limit_price is None


# ---------------------------------------------------------------------------
# T009 — stage_order() payload contains transmit=false and returns order ID
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db() -> MagicMock:
    db = MagicMock()
    db.insert_staged_order = AsyncMock(return_value="fake-uuid-1234")
    db.insert_trade_journal_entry = AsyncMock()
    return db


@pytest.fixture
def mock_httpx_post_success() -> dict[str, Any]:
    return [{"orderId": "IB-987654", "order_status": "PreSubmitted"}]


@pytest.mark.asyncio
async def test_stage_order_payload_has_transmit_false(mock_db: MagicMock) -> None:
    """T009: The REST payload sent to IBKR must contain transmit=False."""
    request = OrderRequest(
        instrument_type="FUT",
        symbol="/MES",
        quantity=1,
        direction="BUY",
        limit_price=5100.00,
        expiration=date(2025, 12, 19),
    )

    captured_payloads: list[Any] = []

    async def fake_post(url: str, **kwargs: Any) -> MagicMock:
        captured_payloads.append(kwargs.get("json", kwargs.get("data", {})))
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(
            return_value=[{"orderId": "IB-123", "order_status": "PreSubmitted"}]
        )
        return resp

    with patch("core.order_manager.httpx.AsyncClient") as MockClient:
        ctx = AsyncMock()
        ctx.post = fake_post
        MockClient.return_value.__aenter__ = AsyncMock(return_value=ctx)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        manager = OrderManager(
            base_url="https://localhost:5001",
            db=mock_db,
        )
        order_id = await manager.stage_order(request, account_id="DU123456")

    assert order_id == "IB-123"
    # Verify the REST payload contained transmit: false
    assert len(captured_payloads) >= 1
    payload = captured_payloads[0]
    orders = payload.get("orders", [payload])
    order_body = orders[0] if isinstance(orders, list) else payload
    assert order_body.get("transmit") is False or order_body.get("transmit") == False  # noqa: E712


@pytest.mark.asyncio
async def test_stage_order_returns_order_id(mock_db: MagicMock) -> None:
    """T009: stage_order() must return a non-null, non-empty string order ID."""
    request = OrderRequest(
        instrument_type="STK",
        symbol="AAPL",
        quantity=10,
        direction="BUY",
        limit_price=175.0,
    )

    with patch("core.order_manager.httpx.AsyncClient") as MockClient:
        ctx = AsyncMock()
        ctx.post = AsyncMock(return_value=MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value=[{"orderId": "IB-AAPL-001"}]),
        ))
        MockClient.return_value.__aenter__ = AsyncMock(return_value=ctx)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        manager = OrderManager(base_url="https://localhost:5001", db=mock_db)
        order_id = await manager.stage_order(request, account_id="DU123456")

    assert order_id
    assert isinstance(order_id, str)


# ---------------------------------------------------------------------------
# T010 — DB persistence after stage_order()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_order_persists_to_db(mock_db: MagicMock) -> None:
    """T010: After stage_order() returns, a record must be written to staged_orders."""
    request = OrderRequest(
        instrument_type="FUT",
        symbol="/ES",
        quantity=3,
        direction="SELL",
        limit_price=5300.0,
        expiration=date(2025, 12, 19),
    )

    with patch("core.order_manager.httpx.AsyncClient") as MockClient:
        ctx = AsyncMock()
        ctx.post = AsyncMock(return_value=MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value=[{"orderId": "IB-ES-999"}]),
        ))
        MockClient.return_value.__aenter__ = AsyncMock(return_value=ctx)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        manager = OrderManager(base_url="https://localhost:5001", db=mock_db)
        await manager.stage_order(request, account_id="DU999")

    mock_db.insert_staged_order.assert_awaited_once()
    call_kwargs = mock_db.insert_staged_order.call_args.kwargs
    assert call_kwargs["symbol"] == "/ES"
    assert call_kwargs["direction"] == "SELL"
    assert call_kwargs["status"] == "STAGED"


# ---------------------------------------------------------------------------
# T011 — ValueError for unsupported instrument type BEFORE any TWS call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_order_rejects_unsupported_instrument_type(
    mock_db: MagicMock,
) -> None:
    """T011: ValueError must be raised before any TWS HTTP call for bad instrument_type.

    Note: OrderRequest validation itself blocks 'OPT' via Pydantic, so we test
    the runtime guard by constructing a patched request object directly.
    """
    # Bypass Pydantic to simulate a request reaching stage_order() with bad type.
    request = MagicMock(spec=OrderRequest)
    request.instrument_type = "CRYPTO"
    request.symbol = "BTC"
    request.quantity = 1
    request.direction = "BUY"
    request.limit_price = 50000.0
    request.expiration = None
    request.strike = None

    with patch("core.order_manager.httpx.AsyncClient") as MockClient:
        manager = OrderManager(base_url="https://localhost:5001", db=mock_db)
        with pytest.raises(ValueError, match="Unsupported instrument_type"):
            await manager.stage_order(request, account_id="DU123456")

        # Confirm the HTTP client was never touched
        MockClient.assert_not_called()


# ---------------------------------------------------------------------------
# T012 — Rollback path: no partial DB record when DB write fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_order_rolls_back_on_db_failure(mock_db: MagicMock) -> None:
    """T012: If the DB write fails after the TWS call, no partial record is left."""
    mock_db.insert_staged_order = AsyncMock(
        side_effect=RuntimeError("DB unavailable")
    )

    request = OrderRequest(
        instrument_type="STK",
        symbol="MSFT",
        quantity=5,
        direction="BUY",
        limit_price=420.0,
    )

    cancel_calls: list[Any] = []

    async def fake_delete(url: str, **kwargs: Any) -> MagicMock:
        cancel_calls.append(url)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp

    with patch("core.order_manager.httpx.AsyncClient") as MockClient:
        ctx = AsyncMock()
        ctx.post = AsyncMock(return_value=MagicMock(
            raise_for_status=MagicMock(),
            json=MagicMock(return_value=[{"orderId": "IB-MSFT-777"}]),
        ))
        ctx.delete = fake_delete
        MockClient.return_value.__aenter__ = AsyncMock(return_value=ctx)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        manager = OrderManager(base_url="https://localhost:5001", db=mock_db)
        with pytest.raises(RuntimeError, match="DB unavailable"):
            await manager.stage_order(request, account_id="DU123456")

    # The order cancellation endpoint must have been called
    assert len(cancel_calls) >= 1, (
        "Expected order cancellation call after DB failure, got none"
    )
