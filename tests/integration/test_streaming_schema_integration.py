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
@pytest.mark.asyncio
async def test_sc005_required_unified_fields_present_for_ibkr_and_tastytrade() -> None:
    processor = DataProcessor(db_manager=_FakeDBManager())  # type: ignore[arg-type]

    ibkr_payload = {
        "contract_key": "AAPL_20260220_200_call",
        "event_time": datetime.now(timezone.utc).isoformat(),
        "underlying": "AAPL",
        "option_type": "call",
        "delta": 0.11,
        "gamma": 0.02,
        "theta": -0.01,
        "vega": 0.08,
    }
    tasty_payload = {
        "eventSymbol": ".AAPL260220C200",
        "event_time": datetime.now(timezone.utc).isoformat(),
        "underlying": "AAPL",
        "option_type": "call",
        "delta": 0.10,
        "gamma": 0.02,
        "theta": -0.01,
        "vega": 0.07,
    }

    ibkr_record = processor._normalize_ibkr_payload(payload=ibkr_payload, account_id="DU123")
    tasty_record = processor._normalize_tasty_payload(payload=tasty_payload, account_id="TASTY-1")

    assert ibkr_record is not None
    assert tasty_record is not None

    for record in (ibkr_record, tasty_record):
        assert record.event_time is not None
        assert record.broker in {"ibkr", "tastytrade"}
        assert record.account_id
        assert record.underlying
        assert record.contract_key
