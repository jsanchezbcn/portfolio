from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from desktop.engine.ib_engine import IBEngine


@pytest.mark.asyncio
async def test_get_available_expiries_uses_secdef_chain():
    engine = IBEngine()
    engine._ib = MagicMock()

    engine._get_active_fop_underlyings = AsyncMock(
        return_value=[SimpleNamespace(symbol="ES", secType="FUT", conId=123, lastTradeDateOrContractMonth="20260320")]
    )

    chain = SimpleNamespace(exchange="CME", expirations=["20270116", "20270320"])
    engine._ib.reqSecDefOptParamsAsync = AsyncMock(return_value=[chain])

    expiries = await engine.get_available_expiries("ES", sec_type="FOP", exchange="CME")

    assert expiries == ["20270116", "20270320"]


@pytest.mark.asyncio
async def test_get_available_expiries_falls_back_to_contract_details_on_timeout():
    engine = IBEngine()
    engine._ib = MagicMock()

    engine._get_active_fop_underlyings = AsyncMock(
        return_value=[SimpleNamespace(symbol="ES", secType="FUT", conId=123, lastTradeDateOrContractMonth="20260320")]
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
async def test_get_available_expiries_combines_next_two_futures_months():
    engine = IBEngine()
    engine._ib = MagicMock()

    front = SimpleNamespace(symbol="ES", secType="FUT", conId=101, lastTradeDateOrContractMonth="20260320")
    next_month = SimpleNamespace(symbol="ES", secType="FUT", conId=202, lastTradeDateOrContractMonth="20260619")
    engine._get_active_fop_underlyings = AsyncMock(return_value=[front, next_month])

    async def _secdef(symbol, fut_fop_exchange, sec_type, conid):
        if conid == 101:
            return [SimpleNamespace(exchange="CME", expirations=["20260311", "20260320"])]
        if conid == 202:
            return [SimpleNamespace(exchange="CME", expirations=["20260430", "20260529", "20260619"])]
        return []

    engine._ib.reqSecDefOptParamsAsync = AsyncMock(side_effect=_secdef)

    expiries = await engine.get_available_expiries("ES", sec_type="FOP", exchange="CME")

    assert expiries == ["20260311", "20260320", "20260430", "20260529", "20260619"]


@pytest.mark.asyncio
async def test_get_chain_uses_future_month_that_contains_selected_expiry():
    engine = IBEngine()
    engine._ib = MagicMock()
    engine.cancel_chain_streaming = MagicMock()

    front = SimpleNamespace(symbol="ES", secType="FUT", conId=101, lastTradeDateOrContractMonth="20260320")
    next_month = SimpleNamespace(symbol="ES", secType="FUT", conId=202, lastTradeDateOrContractMonth="20260619")
    engine._get_active_fop_underlyings = AsyncMock(return_value=[front, next_month])

    async def _secdef(symbol, fut_fop_exchange, sec_type, conid):
        if conid == 101:
            return [SimpleNamespace(exchange="CME", expirations=["20260320"], strikes=[6700.0])]
        if conid == 202:
            return [SimpleNamespace(exchange="CME", expirations=["20260430"], strikes=[6800.0])]
        return []

    engine._ib.reqSecDefOptParamsAsync = AsyncMock(side_effect=_secdef)
    engine._ib.qualifyContractsAsync = AsyncMock(
        side_effect=lambda *contracts: [
            SimpleNamespace(
                conId=index + 1,
                symbol=c.symbol,
                lastTradeDateOrContractMonth=c.lastTradeDateOrContractMonth,
                strike=c.strike,
                right=c.right,
                exchange=c.exchange,
            )
            for index, c in enumerate(contracts)
        ]
    )
    engine._ib.reqMktData = MagicMock(
        return_value=SimpleNamespace(
            last=6793.75,
            close=6793.75,
            bid=10.0,
            ask=11.0,
            volume=1,
            openInterest=2,
        )
    )

    rows = await engine.get_chain(
        "ES",
        expiry=date(2026, 4, 30),
        sec_type="FOP",
        exchange="CME",
        max_strikes=1,
    )

    assert rows
    assert all(row.expiry == "20260430" for row in rows)


@pytest.mark.asyncio
async def test_get_chain_uses_database_cache_before_refetching_live_chain():
    engine = IBEngine()
    engine._ib = MagicMock()
    engine._db_ok = True
    engine._db = SimpleNamespace(
        get_cached_chain=AsyncMock(
            side_effect=[
                [
                    {
                        "underlying": "ES",
                        "expiry": date(2026, 3, 11),
                        "strike": 6800.0,
                        "option_right": "C",
                        "conid": 101,
                        "bid": 12.5,
                        "ask": 13.0,
                        "last": 12.75,
                        "volume": 10,
                        "open_interest": 20,
                        "iv": 0.22,
                        "delta": 0.31,
                        "gamma": 0.01,
                        "theta": -0.15,
                        "vega": 0.85,
                    }
                ],
                [],
            ]
        )
    )
    engine.cancel_chain_streaming = MagicMock()

    rows = await engine.get_chain(
        "ES",
        expiry=date(2026, 3, 11),
        sec_type="FOP",
        exchange="CME",
        max_strikes=1,
    )

    assert len(rows) == 1
    assert rows[0].conid == 101
    assert rows[0].delta == 0.31
    assert not engine._ib.reqSecDefOptParamsAsync.called


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
        und_contract=cast(Any, SimpleNamespace(conId=0)),
    )

    assert len(contracts) == 3
    assert {c.conId for c in contracts} == {1001, 1002, 1003}
