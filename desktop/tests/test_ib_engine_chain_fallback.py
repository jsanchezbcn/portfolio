from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from desktop.engine.ib_engine import IBEngine


@pytest.mark.asyncio
async def test_get_available_expiries_uses_secdef_chain():
    engine = IBEngine()
    engine._ib = MagicMock()

    engine._qualify_underlying = AsyncMock(
        return_value=SimpleNamespace(symbol="ES", secType="FUT", conId=123)
    )

    chain = SimpleNamespace(exchange="CME", expirations=["20270116", "20270320"])
    engine._ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])

    expiries = await engine.get_available_expiries("ES", sec_type="FOP", exchange="CME")

    assert expiries == ["20270116", "20270320"]


@pytest.mark.asyncio
async def test_get_available_expiries_falls_back_to_contract_details_on_timeout():
    engine = IBEngine()
    engine._ib = MagicMock()

    engine._qualify_underlying = AsyncMock(
        return_value=SimpleNamespace(symbol="ES", secType="FUT", conId=123)
    )

    engine._ib.reqSecDefOptParamsAsync = AsyncMock(side_effect=asyncio.TimeoutError())
    engine._ib.reqContractDetailsAsync = AsyncMock(
        return_value=[
            SimpleNamespace(contract=SimpleNamespace(lastTradeDateOrContractMonth="20270219")),
            SimpleNamespace(contract=SimpleNamespace(lastTradeDateOrContractMonth="20270116")),
        ]
    )

    expiries = await engine.get_available_expiries("ES", sec_type="FOP", exchange="CME")

    assert expiries == ["20270116", "20270219"]


@pytest.mark.asyncio
async def test_fallback_filters_invalid_and_past_expiries():
    engine = IBEngine()
    engine._ib = MagicMock()

    # Freeze "today" filter behavior by patching helper date source indirectly.
    # Use clearly old/present/future values to keep test stable over time.
    old_expiry = "20200117"
    future_expiry = "20990119"

    engine._ib.reqContractDetailsAsync = AsyncMock(
        return_value=[
            SimpleNamespace(contract=SimpleNamespace(lastTradeDateOrContractMonth=old_expiry)),
            SimpleNamespace(contract=SimpleNamespace(lastTradeDateOrContractMonth=future_expiry)),
            SimpleNamespace(contract=SimpleNamespace(lastTradeDateOrContractMonth="BADDATE")),
            SimpleNamespace(contract=SimpleNamespace(lastTradeDateOrContractMonth="202704")),
        ]
    )

    expiries = await engine._fallback_fop_expiries_from_contract_details("ES", "CME")

    assert future_expiry in expiries
    assert old_expiry not in expiries
    assert all(len(e) == 8 and e.isdigit() for e in expiries)


@pytest.mark.asyncio
async def test_fallback_fop_contracts_for_expiry_returns_contracts():
    engine = IBEngine()
    engine._ib = MagicMock()

    c1 = SimpleNamespace(conId=1001, right="C", strike=5500.0)
    c2 = SimpleNamespace(conId=1002, right="P", strike=5500.0)
    c3 = SimpleNamespace(conId=1003, right="C", strike=5600.0)

    engine._ib.reqContractDetailsAsync = AsyncMock(
        return_value=[
            SimpleNamespace(contract=c1),
            SimpleNamespace(contract=c2),
            SimpleNamespace(contract=c3),
        ]
    )

    contracts = await engine._fallback_fop_contracts_for_expiry(
        underlying="ES",
        exchange="CME",
        expiry_str="20260320",
        max_strikes=10,
        und_contract=SimpleNamespace(conId=0),
    )

    assert len(contracts) == 3
    assert {c.conId for c in contracts} == {1001, 1002, 1003}
