"""tests/test_historical_charts.py — unit tests for dashboard historical chart data loading."""
from __future__ import annotations

from dashboard.components.historical_charts import _load_snapshots


class _FakeSnapshotStore:
    def __init__(self) -> None:
        self.calls = []

    async def query_snapshots(self, **kwargs):
        self.calls.append(kwargs)
        return [{"captured_at": "2026-03-06T00:00:00+00:00", "spx_delta": 5.0}]


def test_load_snapshots_uses_store_query():
    store = _FakeSnapshotStore()

    rows = _load_snapshots(store, "1W")

    assert len(rows) == 1
    assert rows[0]["spx_delta"] == 5.0
    assert store.calls
    assert "start_dt" in store.calls[0]
