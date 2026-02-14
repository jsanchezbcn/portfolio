from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest

from adapters.ibkr_adapter import IBKRAdapter
from models.unified_position import InstrumentType


@pytest.mark.asyncio
async def test_fetch_positions_transforms_equity_and_option() -> None:
    client = Mock()
    client.get_positions.return_value = [
        {
            "contractDesc": "AAPL",
            "assetClass": "STK",
            "position": 100,
            "avgCost": 175.0,
            "mktValue": 18000.0,
            "unrealizedPnl": 500.0,
            "ticker": "AAPL",
            "mktPrice": 180.0,
        },
        {
            "contractDesc": "AAPL 20MAR26 180 C",
            "assetClass": "OPT",
            "position": -2,
            "avgCost": 5.2,
            "mktValue": -1040.0,
            "unrealizedPnl": 80.0,
            "ticker": "AAPL",
            "undSym": "AAPL",
            "strike": 180,
            "expiry": "20260320",
            "right": "C",
        },
    ]
    client.is_option_contract.side_effect = lambda p: p.get("assetClass") == "OPT"
    client._extract_option_details.return_value = ("AAPL", "20260320", 180.0, "call", "C")
    client.calculate_spx_weighted_delta.return_value = 12.5

    adapter = IBKRAdapter(client=client)
    positions = await adapter.fetch_positions("U000000")

    assert len(positions) == 2
    assert positions[0].instrument_type == InstrumentType.EQUITY
    assert positions[1].instrument_type == InstrumentType.OPTION
    assert positions[1].expiration == date(2026, 3, 20)


@pytest.mark.asyncio
async def test_fetch_greeks_maps_values() -> None:
    client = Mock()
    client.get_tastytrade_option_greeks = AsyncMock(
        return_value={"delta": 0.4, "gamma": 0.02, "theta": -0.1, "vega": 0.12, "impliedVol": 0.25}
    )
    client.calculate_spx_weighted_delta.return_value = 9.5
    adapter = IBKRAdapter(client=client)

    option = Mock()
    option.instrument_type = InstrumentType.OPTION
    option.underlying = "AAPL"
    option.expiration = date(2026, 3, 20)
    option.strike = 180.0
    option.option_type = "call"
    option.quantity = -2
    option.market_value = -1040.0
    option.delta = option.gamma = option.theta = option.vega = 0.0
    option.iv = None
    option.spx_delta = 0.0

    updated = await adapter.fetch_greeks([option])

    assert updated[0].delta == -0.8
    assert updated[0].gamma == -0.04
    assert updated[0].theta == 0.2
    assert updated[0].vega == -0.24
    assert updated[0].iv == 0.25
