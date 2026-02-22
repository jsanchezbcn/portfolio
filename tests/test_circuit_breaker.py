"""
tests/test_circuit_breaker.py
─────────────────────────────
Unit tests for database/circuit_breaker.py.

All tests are fully offline — no real asyncpg pool is used.
The pool is replaced by a simple mock that can be told to succeed or fail.
"""

from __future__ import annotations

import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from database.circuit_breaker import DBCircuitBreaker, _State

# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_buffer(tmp_path: Path) -> Path:
    return tmp_path / "test_buffer.jsonl"


def _make_breaker(tmp_buffer: Path, pool_execute=None) -> DBCircuitBreaker:
    """Return a breaker wired to a mocked pool and a temp buffer file."""
    pool = MagicMock()
    pool.execute = pool_execute or AsyncMock(return_value=None)
    return DBCircuitBreaker(
        pool,
        buffer_path=tmp_buffer,
        flush_interval=9999,          # disable auto-flush in tests
        failure_threshold=3,
    )


# ── happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_success_stays_closed(tmp_buffer):
    breaker = _make_breaker(tmp_buffer)
    await breaker.write("tbl", {"a": 1})
    assert breaker.state == "CLOSED"
    assert not tmp_buffer.exists()          # nothing buffered


@pytest.mark.asyncio
async def test_success_resets_failure_count(tmp_buffer):
    fail_pool = MagicMock()
    calls = 0

    async def flaky(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("boom")

    fail_pool.execute = flaky
    breaker = _make_breaker(tmp_buffer, pool_execute=flaky)
    # Two failures → failure_count=2 but still CLOSED (threshold=3)
    await breaker.write("tbl", {"x": 1})
    await breaker.write("tbl", {"x": 2})
    assert breaker._failure_count == 2
    assert breaker.state == "CLOSED"

    # Success resets counter
    await breaker.write("tbl", {"x": 3})
    assert breaker._failure_count == 0
    assert breaker.state == "CLOSED"


# ── circuit trips OPEN ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_circuit_opens_after_threshold(tmp_buffer):
    fail = AsyncMock(side_effect=ConnectionError("db down"))
    breaker = _make_breaker(tmp_buffer, pool_execute=fail)

    for i in range(3):
        await breaker.write("tbl", {"i": i})

    assert breaker.state == "OPEN"
    # All 3 rows buffered to file
    lines = tmp_buffer.read_text().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["row"]["i"] == 0


@pytest.mark.asyncio
async def test_writes_while_open_go_to_buffer(tmp_buffer):
    fail = AsyncMock(side_effect=ConnectionError())
    breaker = _make_breaker(tmp_buffer, pool_execute=fail)

    # Trip open
    for _ in range(3):
        await breaker.write("tbl", {"v": 0})

    # Extra writes — pool.execute must NOT be called
    fail.reset_mock()
    await breaker.write("tbl", {"v": 99})
    fail.assert_not_called()

    lines = tmp_buffer.read_text().splitlines()
    assert len(lines) == 4


# ── flush / drain ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flush_closes_circuit(tmp_buffer):
    fail = AsyncMock(side_effect=ConnectionError())
    breaker = _make_breaker(tmp_buffer, pool_execute=fail)

    for i in range(3):
        await breaker.write("tbl", {"i": i})

    assert breaker.state == "OPEN"

    # Now fix the pool
    breaker._pool.execute = AsyncMock(return_value=None)
    await breaker._try_flush()

    assert breaker.state == "CLOSED"
    assert len(breaker._buffer) == 0
    assert not tmp_buffer.exists()


@pytest.mark.asyncio
async def test_partial_flush_preserves_order(tmp_buffer):
    """First 2 rows succeed; row 3 fails — rows 3+ must stay at front of buffer."""
    fail = AsyncMock(side_effect=ConnectionError())
    breaker = _make_breaker(tmp_buffer, pool_execute=fail)

    for i in range(3):
        await breaker.write("tbl", {"i": i})
    assert breaker.state == "OPEN"

    call_count = 0

    async def succeed_twice(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count > 2:
            raise ConnectionError("still down")

    breaker._pool.execute = succeed_twice
    await breaker._try_flush()

    assert breaker.state == "OPEN"
    assert len(breaker._buffer) == 1
    assert breaker._buffer[0]["row"]["i"] == 2    # row 3 is still at front


@pytest.mark.asyncio
async def test_concurrent_writes_are_serialised(tmp_buffer):
    fail = AsyncMock(side_effect=ConnectionError())
    breaker = _make_breaker(tmp_buffer, pool_execute=fail)

    # Trip open first
    for _ in range(3):
        await breaker.write("tbl", {"v": -1})

    # Fire 10 concurrent writes while circuit is OPEN
    await asyncio.gather(*[breaker.write("tbl", {"v": i}) for i in range(10)])

    assert len(breaker._buffer) == 13    # 3 trip writes + 10 concurrent
    # File must have exactly 13 valid JSON lines
    lines = tmp_buffer.read_text().splitlines()
    assert len(lines) == 13
    for line in lines:
        json.loads(line)                  # no corruption


# ── crash recovery ────────────────────────────────────────────────────────────

def test_load_buffer_on_startup(tmp_buffer):
    """Rows persisted from a prior run are loaded; circuit starts OPEN."""
    entries = [{"table": "t", "row": {"n": i}} for i in range(5)]
    with tmp_buffer.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")

    pool = MagicMock()
    pool.execute = AsyncMock()
    breaker = DBCircuitBreaker(pool, buffer_path=tmp_buffer, flush_interval=9999)

    assert breaker.state == "OPEN"
    assert len(breaker._buffer) == 5
    assert breaker._buffer[2]["row"]["n"] == 2


def test_malformed_lines_skipped(tmp_buffer):
    tmp_buffer.write_text('{"table":"t","row":{"a":1}}\nNOT_JSON\n{"table":"t","row":{"a":2}}\n')
    pool = MagicMock()
    pool.execute = AsyncMock()
    breaker = DBCircuitBreaker(pool, buffer_path=tmp_buffer, flush_interval=9999)
    assert len(breaker._buffer) == 2


# ── atomic file rewrite (persist_buffer) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_persist_buffer_atomic_rename(tmp_buffer):
    fail = AsyncMock(side_effect=ConnectionError())
    breaker = _make_breaker(tmp_buffer, pool_execute=fail)

    for i in range(3):
        await breaker.write("tbl", {"i": i})

    # Manually persist and verify no .tmp file left behind
    tmp_side = tmp_buffer.with_suffix(".tmp")
    assert not tmp_side.exists()
    assert tmp_buffer.exists()


@pytest.mark.asyncio
async def test_persist_buffer_deletes_file_when_empty(tmp_buffer):
    fail = AsyncMock(side_effect=ConnectionError())
    breaker = _make_breaker(tmp_buffer, pool_execute=fail)

    for _ in range(3):
        await breaker.write("tbl", {"v": 1})
    assert tmp_buffer.exists()

    # Fix pool, flush everything
    breaker._pool.execute = AsyncMock()
    await breaker._try_flush()
    assert not tmp_buffer.exists()
