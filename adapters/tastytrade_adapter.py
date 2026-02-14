from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
import logging

from adapters.base_adapter import BrokerAdapter
from models.unified_position import InstrumentType, UnifiedPosition


LOGGER = logging.getLogger(__name__)


class TastytradeAdapter(BrokerAdapter):
    """Adapter that converts Tastytrade payloads into normalized positions."""

    def __init__(self, client: Any) -> None:
        """Store the broker client dependency."""

        self.client = client

    async def fetch_positions(self, account_id: str) -> list[UnifiedPosition]:
        """Fetch account positions from Tastytrade and normalize schema."""

        try:
            raw_positions = await asyncio.to_thread(self.client.get_positions, account_id)
        except Exception as exc:
            raise ConnectionError(f"Unable to fetch Tastytrade positions for account {account_id}.") from exc

        transformed: list[UnifiedPosition] = []
        for position in raw_positions:
            try:
                transformed.append(self._to_unified_position(position))
            except Exception as exc:
                LOGGER.warning("Skipping unparseable Tastytrade position payload: %s", exc)
                continue
        return transformed

    async def fetch_greeks(self, positions: list[UnifiedPosition]) -> list[UnifiedPosition]:
        """Enrich option positions with Greeks when not already present."""

        for position in positions:
            if position.instrument_type != InstrumentType.OPTION:
                continue

            has_existing = any(
                abs(float(getattr(position, greek, 0.0))) > 0.0
                for greek in ("delta", "gamma", "theta", "vega")
            )
            if has_existing:
                position.greeks_source = "tastytrade"
                continue

            if hasattr(self.client, "get_option_greeks"):
                try:
                    greeks = await asyncio.to_thread(self.client.get_option_greeks, position.symbol)
                except Exception as exc:
                    LOGGER.warning("Unable to fetch Greeks for %s: %s", position.symbol, exc)
                    greeks = None
            else:
                greeks = None

            if not isinstance(greeks, dict):
                continue

            qty = float(position.quantity)
            position.delta = float(greeks.get("delta") or 0.0) * qty
            position.gamma = float(greeks.get("gamma") or 0.0) * qty
            position.theta = float(greeks.get("theta") or 0.0) * qty
            position.vega = float(greeks.get("vega") or 0.0) * qty
            iv_value = greeks.get("iv")
            if iv_value is not None:
                position.iv = float(iv_value)
            position.greeks_source = "tastytrade"

        return positions

    def _to_unified_position(self, position: dict[str, Any]) -> UnifiedPosition:
        """Transform raw Tastytrade position payload into UnifiedPosition."""

        instrument_text = str(position.get("instrument-type") or position.get("instrument_type") or "").lower()
        if "option" in instrument_text:
            instrument_type = InstrumentType.OPTION
        elif "future" in instrument_text:
            instrument_type = InstrumentType.FUTURE
        else:
            instrument_type = InstrumentType.EQUITY

        quantity = float(position.get("quantity") or 0.0)
        avg_open = float(position.get("average-open-price") or position.get("average_open_price") or 0.0)
        mark = float(position.get("mark") or position.get("mark-price") or 0.0)
        multiplier = float(position.get("multiplier") or position.get("contract_multiplier") or 1.0)
        market_value = mark * quantity * multiplier

        symbol = str(position.get("symbol") or "")
        underlying = str(position.get("underlying-symbol") or position.get("underlying_symbol") or "").upper() or None

        strike = position.get("strike-price") or position.get("strike_price")
        strike_float = float(strike) if strike not in (None, "") else None

        expiry_raw = position.get("expiration-date") or position.get("expiration_date")
        expiration = None
        if expiry_raw:
            expiration = datetime.strptime(str(expiry_raw), "%Y-%m-%d").date()

        option_type_raw = str(position.get("option-type") or position.get("option_type") or "").upper()
        option_type = None
        if option_type_raw.startswith("C"):
            option_type = "call"
        elif option_type_raw.startswith("P"):
            option_type = "put"

        delta = float(position.get("delta") or 0.0) * quantity
        gamma = float(position.get("gamma") or 0.0) * quantity
        theta = float(position.get("theta") or 0.0) * quantity
        vega = float(position.get("vega") or 0.0) * quantity

        iv_raw = position.get("iv")
        iv = float(iv_raw) if iv_raw not in (None, "") else None

        return UnifiedPosition(
            symbol=symbol,
            instrument_type=instrument_type,
            broker="tastytrade",
            quantity=quantity,
            contract_multiplier=multiplier,
            avg_price=avg_open,
            market_value=market_value,
            unrealized_pnl=float(position.get("realized-day-gain") or position.get("unrealized_pnl") or 0.0),
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            iv=iv,
            underlying=underlying,
            strike=strike_float,
            expiration=expiration,
            option_type=option_type,
            greeks_source="tastytrade" if instrument_type == InstrumentType.OPTION else "none",
        )
