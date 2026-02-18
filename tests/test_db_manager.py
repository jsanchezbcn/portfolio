from __future__ import annotations

from datetime import datetime, timezone

import pytest

from database.db_manager import DBManager, GreekSnapshotRecord


@pytest.mark.asyncio
async def test_enqueue_snapshot_flushes_on_batch_size() -> None:
    manager = DBManager(flush_interval_seconds=10, flush_batch_size=2)

    inserted: list[GreekSnapshotRecord] = []

    async def _fake_insert(data: list[GreekSnapshotRecord]) -> int:
        inserted.extend(data)
        return len(data)

    manager.batch_insert_snapshots = _fake_insert  # type: ignore[assignment]

    base = GreekSnapshotRecord(
        event_time=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        broker="ibkr",
        account_id="DU123",
        underlying="AAPL",
        contract_key="AAPL_20260220_200_call",
        option_type="call",
        delta=0.1,
    )

    await manager.enqueue_snapshot(base)
    assert len(inserted) == 0

    await manager.enqueue_snapshot(base)
    assert len(inserted) == 2


@pytest.mark.asyncio
async def test_flush_now_returns_zero_for_empty_buffer() -> None:
    manager = DBManager()
    inserted: list[GreekSnapshotRecord] = []

    async def _fake_insert(data: list[GreekSnapshotRecord]) -> int:
        inserted.extend(data)
        return len(data)

    manager.batch_insert_snapshots = _fake_insert  # type: ignore[assignment]

    count = await manager.flush_now()

    assert count == 0
    assert inserted == []
