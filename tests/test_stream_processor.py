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


@pytest.mark.asyncio
async def test_processor_dedupes_identical_messages() -> None:
    db = _FakeDBManager()
    processor = DataProcessor(db_manager=db)  # type: ignore[arg-type]

    payload = {
        "contract_key": "AAPL_20260220_200_call",
        "event_time": datetime.now(timezone.utc).isoformat(),
        "underlying": "AAPL",
        "option_type": "call",
        "delta": 0.11,
        "gamma": 0.02,
        "theta": -0.01,
        "vega": 0.08,
        "rho": 0.03,
    }

    first = await processor.process_ibkr_message(payload, account_id="DU123")
    second = await processor.process_ibkr_message(payload, account_id="DU123")

    assert first is True
    assert second is False
    assert len(db.records) == 1


def test_processor_exposes_session_state_shape() -> None:
    db = _FakeDBManager()
    processor = DataProcessor(db_manager=db)  # type: ignore[arg-type]

    processor.set_session_state("ibkr", status="connected", subscription_count=4)

    sessions = processor.get_stream_sessions()

    assert any(item["broker"] == "ibkr" for item in sessions)
    assert any(item["status"] == "connected" for item in sessions)
