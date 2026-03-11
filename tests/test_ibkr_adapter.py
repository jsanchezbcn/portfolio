from datetime import date
from types import SimpleNamespace
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


@pytest.mark.asyncio
async def test_fetch_positions_portal_falls_back_to_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IB_API_MODE", "PORTAL")

    client = Mock()
    client.get_positions.side_effect = RuntimeError("portal unauthorized")

    adapter = IBKRAdapter(client=client)
    socket_positions = [Mock()]
    adapter._fetch_positions_via_tws_socket = AsyncMock(return_value=socket_positions)

    result = await adapter.fetch_positions("U000000")

    assert result is socket_positions
    adapter._fetch_positions_via_tws_socket.assert_awaited_once_with("U000000")


@pytest.mark.asyncio
async def test_fetch_greeks_portal_falls_back_to_tws_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IB_API_MODE", "PORTAL")

    client = Mock()
    client.get_market_greeks_batch.return_value = {}
    client.get_spx_price.return_value = 5000.0

    adapter = IBKRAdapter(client=client)
    adapter.disable_tasty_cache = True
    adapter.force_refresh_on_miss = False
    adapter._beta_weighter.compute_spx_equivalent_delta = AsyncMock(
        return_value=SimpleNamespace(spx_equivalent_delta=1.23, beta_unavailable=False)
    )
    adapter._fetch_greeks_via_tws_socket = AsyncMock(
        return_value={
            123: {
                "delta": 0.5,
                "gamma": 0.1,
                "theta": -0.2,
                "vega": 0.3,
                "iv": 0.25,
                "source": "tws_socket",
            }
        }
    )

    option = Mock()
    option.instrument_type = InstrumentType.OPTION
    option.underlying = "ES"
    option.expiration = date(2026, 3, 20)
    option.strike = 6800.0
    option.option_type = "put"
    option.quantity = 1.0
    option.contract_multiplier = 50.0
    option.market_value = 1000.0
    option.delta = option.gamma = option.theta = option.vega = 0.0
    option.iv = None
    option.spx_delta = 0.0
    option.greeks_source = "none"
    option.broker_id = "123"
    option.underlying_price = None
    option.beta_unavailable = False

    updated = await adapter.fetch_greeks([option])

    assert updated[0].greeks_source == "tws_socket"
    assert updated[0].delta == pytest.approx(25.0)
    assert updated[0].theta == pytest.approx(-10.0)
    assert updated[0].vega == pytest.approx(15.0)
    adapter._fetch_greeks_via_tws_socket.assert_awaited_once()
