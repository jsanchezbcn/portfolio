from __future__ import annotations

from datetime import date
from unittest.mock import Mock

import pytest

from adapters.tastytrade_adapter import TastytradeAdapter
from models.unified_position import InstrumentType


@pytest.mark.asyncio
async def test_tastytrade_adapter_transforms_and_maps_option() -> None:
    client = Mock()
    client.get_positions.return_value = [
        {
            "symbol": "AAPL  260320C00180000",
            "instrument-type": "Equity Option",
            "quantity": -2,
            "average-open-price": "5.20",
            "mark": "5.40",
            "realized-day-gain": "140",
            "delta": "0.42",
            "gamma": "0.02",
            "theta": "-0.11",
            "vega": "0.16",
            "iv": "0.28",
            "underlying-symbol": "AAPL",
            "strike-price": "180",
            "expiration-date": "2026-03-20",
            "option-type": "C",
        }
    ]

    adapter = TastytradeAdapter(client=client)
    positions = await adapter.fetch_positions("TEST")

    assert len(positions) == 1
    assert positions[0].instrument_type == InstrumentType.OPTION
    assert positions[0].expiration == date(2026, 3, 20)
    assert positions[0].option_type == "call"
    assert positions[0].greeks_source == "tastytrade"


@pytest.mark.asyncio
async def test_tastytrade_adapter_fetch_greeks_uses_client_when_missing() -> None:
    client = Mock()
    client.get_positions.return_value = []
    client.get_option_greeks.return_value = {
        "delta": 0.35,
        "gamma": 0.01,
        "theta": -0.12,
        "vega": 0.2,
        "iv": 0.31,
    }

    adapter = TastytradeAdapter(client=client)
    positions = await adapter.fetch_positions("TEST")

    from models.unified_position import UnifiedPosition

    position = UnifiedPosition(
        symbol="MSFT  260320P00400000",
        instrument_type=InstrumentType.OPTION,
        broker="tastytrade",
        quantity=2,
        avg_price=1.0,
        market_value=2.0,
        unrealized_pnl=0.0,
        underlying="MSFT",
        strike=400.0,
        expiration=date(2026, 3, 20),
        option_type="put",
    )
    positions.append(position)

    updated = await adapter.fetch_greeks(positions)

    assert updated[0].delta == pytest.approx(0.7)
    assert updated[0].theta == pytest.approx(-0.24)
    assert updated[0].vega == pytest.approx(0.4)
    assert updated[0].iv == pytest.approx(0.31)
