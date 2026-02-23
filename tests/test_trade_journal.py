"""tests/test_trade_journal.py — T036: Trade Journal unit tests.

Tests for LocalStore record_fill / query_journal / export_csv using in-memory SQLite.
All tests must PASS after T037–T039 are implemented (they test the completed implementation).
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database.local_store import LocalStore
from models.order import TradeJournalEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_entry(**kwargs) -> TradeJournalEntry:
    """Build a minimal TradeJournalEntry with sensible defaults."""
    defaults = dict(
        broker="IBKR",
        account_id="U12345",
        broker_order_id=str(uuid.uuid4()),
        underlying="SPX",
        strategy_tag="short_strangle",
        status="FILLED",
        legs_json=json.dumps([{"symbol": "SPX", "action": "SELL", "quantity": 1}]),
        net_debit_credit=-500.0,
        vix_at_fill=18.5,
        spx_price_at_fill=5000.0,
        regime="neutral_volatility",
        pre_greeks_json=json.dumps({"spx_delta": 10.0, "theta": -150.0, "vega": 3000.0, "gamma": 0.05}),
        post_greeks_json=json.dumps({"spx_delta": 5.0, "theta": -200.0, "vega": 2500.0, "gamma": 0.04}),
        user_rationale="Selling premium on weekly expiry",
    )
    defaults.update(kwargs)
    return TradeJournalEntry(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    """Fresh LocalStore backed by a temp SQLite file."""
    db_path = str(tmp_path / "test_journal.db")
    s = LocalStore(db_path=db_path)
    _run(s._ensure_init())
    return s


# ---------------------------------------------------------------------------
# T037: record_fill
# ---------------------------------------------------------------------------

class TestRecordFill:
    def test_record_fill_returns_uuid(self, store):
        """record_fill returns the entry UUID."""
        entry = _make_entry()
        returned_id = _run(store.record_fill(entry))
        assert returned_id == entry.entry_id

    def test_record_fill_stores_all_required_fields(self, store):
        """All FR-014 fields are persisted and retrievable."""
        entry = _make_entry(
            underlying="AAPL",
            vix_at_fill=22.0,
            spx_price_at_fill=5100.0,
            regime="high_volatility",
            user_rationale="Test rationale",
            ai_suggestion_id="ai-uuid-123",
        )
        _run(store.record_fill(entry))

        rows = _run(store.query_journal())
        assert len(rows) == 1
        row = rows[0]

        assert row["underlying"] == "AAPL"
        assert row["broker"] == "IBKR"
        assert row["account_id"] == "U12345"
        assert row["broker_order_id"] == entry.broker_order_id
        assert row["strategy_tag"] == "short_strangle"
        assert row["status"] == "FILLED"
        assert abs(row["vix_at_fill"] - 22.0) < 0.001
        assert abs(row["spx_price_at_fill"] - 5100.0) < 0.001
        assert row["regime"] == "high_volatility"
        assert row["user_rationale"] == "Test rationale"
        assert row["ai_suggestion_id"] == "ai-uuid-123"
        assert row["pre_greeks_json"] != "{}" or row["pre_greeks_json"] == entry.pre_greeks_json
        assert row["post_greeks_json"] != "{}" or row["post_greeks_json"] == entry.post_greeks_json

    def test_record_fill_duplicate_broker_order_id_upserts(self, store):
        """Duplicate broker_order_id is handled gracefully (upsert, not error)."""
        broker_oid = str(uuid.uuid4())
        entry1 = _make_entry(broker_order_id=broker_oid, user_rationale="first")
        entry2 = TradeJournalEntry(
            entry_id=entry1.entry_id,  # same DB primary key → replace
            broker_order_id=broker_oid,
            underlying="SPX",
            user_rationale="updated",
        )
        _run(store.record_fill(entry1))
        _run(store.record_fill(entry2))  # Should not raise

        rows = _run(store.query_journal())
        assert len(rows) == 1
        assert rows[0]["user_rationale"] == "updated"

    def test_record_fill_persists_across_reconnect(self, tmp_path):
        """Fills survive LocalStore re-instantiation (new connection to same DB)."""
        db_path = str(tmp_path / "persist_test.db")
        store1 = LocalStore(db_path=db_path)
        entry = _make_entry(underlying="ES", broker_order_id="order-persist-1")
        _run(store1.record_fill(entry))

        # Re-open with a fresh LocalStore instance
        store2 = LocalStore(db_path=db_path)
        rows = _run(store2.query_journal())
        assert len(rows) == 1
        assert rows[0]["underlying"] == "ES"


# ---------------------------------------------------------------------------
# T038: query_journal
# ---------------------------------------------------------------------------

class TestQueryJournal:
    def _insert_three(self, store) -> list[TradeJournalEntry]:
        """Helper: insert 3 entries with different dates and attributes."""
        base = datetime(2025, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
        entries = [
            _make_entry(
                underlying="SPX",
                regime="low_volatility",
                created_at=(base + timedelta(days=0)).isoformat(),
            ),
            _make_entry(
                underlying="AAPL",
                regime="neutral_volatility",
                created_at=(base + timedelta(days=1)).isoformat(),
            ),
            _make_entry(
                underlying="SPX",
                regime="high_volatility",
                created_at=(base + timedelta(days=2)).isoformat(),
            ),
        ]
        for e in entries:
            _run(store.record_fill(e))
        return entries

    def test_query_all_returns_newest_first(self, store):
        entries = self._insert_three(store)
        rows = _run(store.query_journal())
        assert len(rows) == 3
        # Newest first
        assert rows[0]["created_at"] >= rows[1]["created_at"]
        assert rows[1]["created_at"] >= rows[2]["created_at"]

    def test_filter_by_date_range(self, store):
        entries = self._insert_three(store)
        # Only the middle entry falls between day 0.5 and day 1.5
        start = "2025-03-02T00:00:00+00:00"
        end   = "2025-03-02T23:59:59+00:00"
        rows = _run(store.query_journal(start_dt=start, end_dt=end))
        assert len(rows) == 1
        assert rows[0]["underlying"] == "AAPL"

    def test_filter_by_instrument(self, store):
        self._insert_three(store)
        rows = _run(store.query_journal(instrument="AAPL"))
        assert len(rows) == 1
        assert rows[0]["underlying"] == "AAPL"

    def test_filter_by_regime(self, store):
        self._insert_three(store)
        rows = _run(store.query_journal(regime="low_volatility"))
        assert len(rows) == 1
        assert rows[0]["regime"] == "low_volatility"

    def test_limit_controls_max_rows(self, store):
        self._insert_three(store)
        rows = _run(store.query_journal(limit=2))
        assert len(rows) == 2

    def test_empty_journal_returns_empty_list(self, store):
        rows = _run(store.query_journal())
        assert rows == []


# ---------------------------------------------------------------------------
# T039: export_csv
# ---------------------------------------------------------------------------

class TestExportCsv:
    def test_export_csv_produces_valid_csv(self, store):
        """export_csv produces parseable CSV with the correct headers."""
        entry = _make_entry(underlying="QQQ")
        _run(store.record_fill(entry))
        rows = _run(store.query_journal())

        csv_str = store.export_csv(rows)
        assert csv_str, "export_csv should return non-empty string"

        reader = csv.DictReader(io.StringIO(csv_str))
        parsed = list(reader)
        assert len(parsed) == 1
        assert parsed[0]["underlying"] == "QQQ"

    def test_export_csv_includes_all_fr014_columns(self, store):
        """All required FR-014 columns are present in the CSV header."""
        entry = _make_entry()
        _run(store.record_fill(entry))
        rows = _run(store.query_journal())
        csv_str = store.export_csv(rows)

        reader = csv.DictReader(io.StringIO(csv_str))
        headers = reader.fieldnames or []

        required = [
            "id", "created_at", "broker", "account_id", "broker_order_id",
            "underlying", "strategy_tag", "status", "legs_json",
            "net_debit_credit", "vix_at_fill", "spx_price_at_fill", "regime",
            "pre_greeks_json", "post_greeks_json",
            "user_rationale", "ai_rationale", "ai_suggestion_id",
        ]
        for col in required:
            assert col in headers, f"Missing column: {col}"

    def test_export_csv_empty_list_returns_empty_string(self, store):
        csv_str = store.export_csv([])
        assert csv_str == ""

    def test_export_csv_multiple_rows(self, store):
        for symbol in ["SPX", "AAPL", "QQQ"]:
            _run(store.record_fill(_make_entry(underlying=symbol)))
        rows = _run(store.query_journal())
        csv_str = store.export_csv(rows)
        reader = csv.DictReader(io.StringIO(csv_str))
        parsed = list(reader)
        assert len(parsed) == 3


# ---------------------------------------------------------------------------
# T057 (partial): Account Snapshots
# ---------------------------------------------------------------------------

class TestAccountSnapshots:
    def test_capture_snapshot_saves_fields(self, store):
        from models.order import AccountSnapshot
        snap = AccountSnapshot(
            account_id="U12345",
            broker="IBKR",
            net_liquidation=250_000.0,
            spx_delta=18.5,
            gamma=0.05,
            theta=-150.0,
            vega=2000.0,
            vix=18.5,
            spx_price=5000.0,
            regime="neutral_volatility",
        )
        snap_id = _run(store.capture_snapshot(snap))
        assert snap_id == snap.snapshot_id

        rows = _run(store.query_snapshots(account_id="U12345"))
        assert len(rows) == 1
        row = rows[0]
        assert abs(row["net_liquidation"] - 250_000.0) < 1.0
        assert abs(row["spx_delta"] - 18.5) < 0.01

    def test_delta_theta_ratio_computed(self, store):
        """delta_theta_ratio = theta/delta computed when not supplied."""
        from models.order import AccountSnapshot
        snap = AccountSnapshot(
            account_id="U12345",
            broker="IBKR",
            spx_delta=10.0,
            theta=-200.0,
        )
        _run(store.capture_snapshot(snap))
        rows = _run(store.query_snapshots())
        assert rows[0]["delta_theta_ratio"] == pytest.approx(-200.0 / 10.0)

    def test_query_snapshots_asc_order(self, store):
        """query_snapshots returns rows oldest-first (for chart rendering)."""
        from models.order import AccountSnapshot
        base = datetime(2025, 3, 1, tzinfo=timezone.utc)
        for i in range(3):
            snap = AccountSnapshot(
                account_id="U12345",
                broker="IBKR",
                captured_at=(base + timedelta(hours=i)).isoformat(),
            )
            _run(store.capture_snapshot(snap))
        rows = _run(store.query_snapshots())
        assert rows[0]["captured_at"] <= rows[1]["captured_at"]
        assert rows[1]["captured_at"] <= rows[2]["captured_at"]
