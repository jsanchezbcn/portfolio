"""
core/order_manager.py — User Story 1: Stage an order without transmitting.

The OrderManager submits orders to the IBKR Client Portal REST API with
``transmit: False`` so that orders are staged in TWS but never auto-executed.

Supported instrument types: STK (equities), FUT (futures including /MES, /ES).

Usage:
    manager = OrderManager(base_url="https://localhost:5001", db=db_manager)
    order_id = await manager.stage_order(request, account_id="DU123456")
"""
from __future__ import annotations

import logging
import ssl
from datetime import date
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal

from models.order import OrderStatus
from core.event_bus import get_event_bus

logger = logging.getLogger(__name__)

class OrderStateMachine:
    """
    Strict state machine for orders.
    Valid transitions:
    - DRAFT -> SIMULATED
    - SIMULATED -> STAGED
    - STAGED -> SUBMITTED
    - SUBMITTED -> PENDING
    - PENDING -> PARTIAL_FILL, FILLED, CANCELED, REJECTED
    - PARTIAL_FILL -> FILLED, CANCELED
    """
    VALID_TRANSITIONS = {
        OrderStatus.DRAFT: {OrderStatus.SIMULATED},
        OrderStatus.SIMULATED: {OrderStatus.STAGED},
        OrderStatus.STAGED: {OrderStatus.SUBMITTED, OrderStatus.CANCELED},
        OrderStatus.SUBMITTED: {OrderStatus.PENDING, OrderStatus.REJECTED},
        OrderStatus.PENDING: {OrderStatus.PARTIAL_FILL, OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED},
        OrderStatus.PARTIAL_FILL: {OrderStatus.FILLED, OrderStatus.CANCELED},
        OrderStatus.FILLED: set(),
        OrderStatus.CANCELED: set(),
        OrderStatus.REJECTED: set(),
    }

    @classmethod
    def can_transition(cls, current_state: OrderStatus, next_state: OrderStatus) -> bool:
        return next_state in cls.VALID_TRANSITIONS.get(current_state, set())

    @classmethod
    def transition(cls, current_state: OrderStatus, next_state: OrderStatus) -> OrderStatus:
        if not cls.can_transition(current_state, next_state):
            raise ValueError(f"Invalid state transition from {current_state} to {next_state}")
        return next_state

# ---------------------------------------------------------------------------
# T013 — OrderRequest Pydantic model
# ---------------------------------------------------------------------------

# Known conid mapping for common futures contracts.
# Extend as needed; conid lookup falls back to a symbol search on the gateway.
_FUTURES_CONID: dict[str, int] = {
    "/MES": 495512553,  # Micro E-mini S&P 500
    "/ES": 495512551,    # E-mini S&P 500
    "/MNQ": 551601728,  # Micro Nasdaq
    "/NQ": 551601726,    # E-mini Nasdaq
}


class OrderRequest(BaseModel):
    """Validated order request for staging via the IBKR Client Portal API.

    T013: fields defined here map 1-to-1 to the staged_orders DB schema.
    """

    # T013: instrument_type constrained to STK or FUT only
    instrument_type: Literal["STK", "FUT"]
    symbol: str
    quantity: float
    direction: Literal["BUY", "SELL"]
    limit_price: Optional[float] = None
    expiration: Optional[date] = None
    strike: Optional[float] = None

    @field_validator("quantity")
    @classmethod
    def quantity_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("quantity must be positive")
        return v


# ---------------------------------------------------------------------------
# T014/T015/T016/T017 — OrderManager
# ---------------------------------------------------------------------------


class OrderManager:
    """Stages orders in TWS with ``transmit=False`` via the Client Portal REST API.

    Args:
        base_url: Base URL for the IBKR Client Portal Gateway
                  (e.g. ``https://localhost:5001``).
        db:       An initialised DBManager instance for persisting staged orders.
        verify_ssl: Set to False to bypass self-signed cert verification
                    on the local gateway (default: False).
    """

    def __init__(
        self,
        *,
        base_url: str = "https://localhost:5001",
        db: Any,
        verify_ssl: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.db = db
        self.verify_ssl = verify_ssl

    # ------------------------------------------------------------------ #
    # T015 — IBKR order-mapping helpers                                   #
    # ------------------------------------------------------------------ #

    def _map_sec_type(self, instrument_type: str) -> str:
        """Translate OrderRequest.instrument_type to TWS secType."""
        mapping = {"STK": "STK", "FUT": "FUT"}
        return mapping[instrument_type]

    def _resolve_conid(self, request: OrderRequest) -> int | None:
        """Return a known conid for well-known futures; None for others.

        For unknown conids the caller must resolve via the gateway symbol search.
        """
        if request.instrument_type == "FUT":
            return _FUTURES_CONID.get(request.symbol.upper())
        return None

    def _build_order_body(self, request: OrderRequest) -> dict[str, Any]:
        """T015: Build the JSON body for POST /iserver/account/{id}/orders.

        The ``transmit`` field is ALWAYS False — this is the core safety invariant.
        """
        order: dict[str, Any] = {
            "secType": self._map_sec_type(request.instrument_type),
            "orderType": "LMT" if request.limit_price is not None else "MKT",
            "side": request.direction,
            "quantity": request.quantity,
            "transmit": False,  # CRITICAL: never auto-transmit
            "tif": "DAY",
        }

        conid = self._resolve_conid(request)
        if conid is not None:
            order["conid"] = conid
        else:
            order["symbol"] = request.symbol

        if request.limit_price is not None:
            order["price"] = request.limit_price

        if request.expiration is not None:
            order["lastTradingDayOrContractMonth"] = request.expiration.strftime(
                "%Y%m"
            )

        return order

    # ------------------------------------------------------------------ #
    # T014/T017 — stage_order                                             #
    # ------------------------------------------------------------------ #

    async def stage_order(
        self, request: OrderRequest, *, account_id: str
    ) -> str:
        """Submit an order with ``transmit=False`` and persist to staged_orders.

        T017: raises ValueError for unsupported instrument_type BEFORE any
        HTTP call is made.

        T012: cancels the TWS order if the DB write fails (try/finally).

        Returns:
            The TWS order ID string returned by the gateway.

        Raises:
            ValueError: if instrument_type is not STK or FUT.
            httpx.HTTPStatusError: if the gateway returns a non-2xx response.
        """
        # T017 — guard (Pydantic already blocks this, but defence-in-depth)
        if request.instrument_type not in ("STK", "FUT"):
            raise ValueError(
                f"Unsupported instrument_type: {request.instrument_type!r}. "
                "Only STK and FUT are supported."
            )

        order_body = self._build_order_body(request)
        url = f"{self.base_url}/v1/api/iserver/account/{account_id}/orders"
        payload = {"orders": [order_body]}

        logger.info(
            "Staging order: symbol=%s type=%s qty=%s dir=%s account=%s",
            request.symbol,
            request.instrument_type,
            request.quantity,
            request.direction,
            account_id,
        )

        # T014 — submit to Client Portal REST
        async with httpx.AsyncClient(verify=self.verify_ssl) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data: list[dict[str, Any]] = resp.json()

        if not data:
            raise RuntimeError("Gateway returned empty order response")

        tws_order_id: str = data[0].get("orderId", "")
        if not tws_order_id:
            raise RuntimeError(
                f"Gateway did not return an orderId: {data!r}"
            )

        logger.info("TWS order staged: orderId=%s", tws_order_id)

        # T016 — persist to staged_orders; cancel TWS order on DB failure
        try:
            await self.db.insert_staged_order(
                tws_order_id=tws_order_id,
                account_id=account_id,
                instrument_type=request.instrument_type,
                symbol=request.symbol,
                quantity=request.quantity,
                direction=request.direction,
                limit_price=request.limit_price,
                expiration=request.expiration,
                strike=request.strike,
                status=OrderStatus.STAGED.value,
            )
            
            event_bus = get_event_bus()
            if event_bus._running:
                await event_bus.publish("order_updates", {
                    "event": "ORDER_STAGED",
                    "tws_order_id": tws_order_id,
                    "account_id": account_id,
                    "symbol": request.symbol,
                    "status": OrderStatus.STAGED.value
                })
        except Exception:
            logger.exception(
                "DB write failed for order %s — attempting TWS cancellation",
                tws_order_id,
            )
            await self._cancel_order(account_id=account_id, order_id=tws_order_id)
            raise

        return tws_order_id

    async def _cancel_order(self, *, account_id: str, order_id: str) -> None:
        """Attempt to cancel a staged order in TWS. Errors are logged, not raised."""
        cancel_url = (
            f"{self.base_url}/v1/api/iserver/account/{account_id}/order/{order_id}"
        )
        try:
            async with httpx.AsyncClient(verify=self.verify_ssl) as client:
                resp = await client.delete(cancel_url)
                resp.raise_for_status()
                logger.info("TWS order %s cancelled after DB failure", order_id)
        except Exception:
            logger.exception(
                "Failed to cancel TWS order %s after DB failure — manual review required",
                order_id,
            )
