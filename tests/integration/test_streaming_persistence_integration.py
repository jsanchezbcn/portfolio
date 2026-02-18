from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.processor import DataProcessor
from database.db_manager import GreekSnapshotRecord


class _FakeDBManager:
    def __init__(self) -> None:
        self.records: list[GreekSnapshotRecord] = []

    async def connect(self) -> None:
        return None

    async def start_background_flush(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def enqueue_snapshot(self, record: GreekSnapshotRecord) -> None:
        self.records.append(record)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sc001_persistence_rate_is_at_least_99_percent_for_valid_ticks() -> None:
    db = _FakeDBManager()
    processor = DataProcessor(db_manager=db)  # type: ignore[arg-type]

    total = 200
    persisted = 0
    for index in range(total):
        payload = {
            "contract_key": f"AAPL_20260220_{200 + index}_call",
            "event_time": datetime.now(timezone.utc).isoformat(),
            "underlying": "AAPL",
            "option_type": "call",
            "delta": 0.12,
            "gamma": 0.03,
            "theta": -0.01,
            "vega": 0.09,
        }
        if await processor.process_ibkr_message(payload, account_id="DU123"):
            persisted += 1

    success_rate = persisted / total

    assert success_rate >= 0.99
    assert len(db.records) == persisted
