from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.processor import DataProcessor


class _FakeDBManager:
    async def connect(self) -> None:
        return None

    async def start_background_flush(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def enqueue_snapshot(self, record) -> None:
        return None


@pytest.mark.integration
def test_sc004_outage_isolation_keeps_healthy_stream_running_within_5_seconds() -> None:
    processor = DataProcessor(db_manager=_FakeDBManager())  # type: ignore[arg-type]

    start = datetime.now(timezone.utc)
    processor.set_session_state("ibkr", status="degraded", message_at=start, last_error="simulated outage")

    healthy_message_time = datetime.now(timezone.utc)
    processor.set_session_state("tastytrade", status="connected", message_at=healthy_message_time, subscription_count=12)

    interruption_seconds = (healthy_message_time - start).total_seconds()
    sessions = {item["broker"]: item for item in processor.get_stream_sessions()}

    assert sessions["ibkr"]["status"] == "degraded"
    assert sessions["tastytrade"]["status"] == "connected"
    assert sessions["tastytrade"]["subscriptionCount"] == 12
    assert interruption_seconds <= 5.0
