"""tests/test_business_store.py — unit tests for the shared Postgres business store."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from database.business_store import PostgresBusinessStore


class _FakeDatabase:
    def __init__(self) -> None:
        self.connect = AsyncMock()
        self.close = AsyncMock()
        self.upsert_market_intel = AsyncMock(return_value="intel-1")
        self.get_recent_market_intel = AsyncMock(return_value=[{"symbol": "SPY"}])
        self.get_market_intel_by_source = AsyncMock(return_value=[{"source": "llm_risk_audit"}])
        self.create_journal_note = AsyncMock(return_value="note-1")
        self.list_journal_notes = AsyncMock(return_value=[{"title": "Desk note"}])
        self.capture_portfolio_snapshot = AsyncMock()
        self.query_snapshots = AsyncMock(return_value=[{"captured_at": "2026-03-06T00:00:00+00:00"}])


@pytest.mark.asyncio
async def test_business_store_connects_before_read_methods():
    db = _FakeDatabase()
    store = PostgresBusinessStore(database=cast(Any, db))

    rows = await store.get_recent_market_intel(limit=10)

    db.connect.assert_awaited()
    db.get_recent_market_intel.assert_awaited_once_with(limit=10)
    assert rows == [{"symbol": "SPY"}]


@pytest.mark.asyncio
async def test_business_store_delegates_market_intel_by_source():
    db = _FakeDatabase()
    store = PostgresBusinessStore(database=cast(Any, db))

    rows = await store.get_market_intel_by_source("llm_risk_audit", symbol="PORTFOLIO", limit=1)

    db.get_market_intel_by_source.assert_awaited_once_with("llm_risk_audit", symbol="PORTFOLIO", limit=1)
    assert rows[0]["source"] == "llm_risk_audit"


@pytest.mark.asyncio
async def test_business_store_maps_snapshot_object_to_payload():
    db = _FakeDatabase()
    store = PostgresBusinessStore(database=cast(Any, db))
    snapshot = SimpleNamespace(
        account_id="U123",
        net_liquidation=250000.0,
        cash_balance=50000.0,
        spx_delta=12.5,
        gamma=1.2,
        theta=-44.0,
        vega=300.0,
        vix=18.0,
        regime="neutral",
    )

    await store.capture_snapshot(snapshot)

    db.capture_portfolio_snapshot.assert_awaited_once()
    assert db.capture_portfolio_snapshot.await_args is not None
    payload = db.capture_portfolio_snapshot.await_args.args[0]
    assert payload["account_id"] == "U123"
    assert payload["net_liquidation"] == 250000.0
    assert payload["spx_delta"] == 12.5


@pytest.mark.asyncio
async def test_business_store_passes_through_query_snapshots():
    db = _FakeDatabase()
    store = PostgresBusinessStore(database=cast(Any, db))

    rows = await store.query_snapshots(start_dt="2026-03-01T00:00:00+00:00")

    db.query_snapshots.assert_awaited_once_with(start_dt="2026-03-01T00:00:00+00:00")
    assert rows[0]["captured_at"].startswith("2026-03-06")
