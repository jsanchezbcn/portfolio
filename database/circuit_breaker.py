"""
database/circuit_breaker.py
──────────────────────────
Async circuit-breaker for asyncpg writes.

States
  CLOSED    – DB healthy; writes go directly to Postgres.
  OPEN      – DB unreachable; writes buffered to ~/.portfolio_bridge_buffer.jsonl.
  HALF_OPEN – Probe in progress; transitions to CLOSED on success, OPEN on failure.

Usage
  breaker = DBCircuitBreaker(pool)
  asyncio.create_task(breaker.flush_loop())   # start background drain task

  await breaker.write("portfolio_snapshots", {"ts": ..., "delta": ...})
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from enum import Enum, auto
from pathlib import Path
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

# ── tunables ────────────────────────────────────────────────────────────────
_BUFFER_PATH      = Path.home() / ".portfolio_bridge_buffer.jsonl"
_FLUSH_INTERVAL   = 60        # seconds between drain attempts
_FAILURE_THRESHOLD = 3        # consecutive write failures → OPEN
_DB_TIMEOUT       = 5.0       # seconds per individual insert
_FLUSH_BATCH_SIZE = 200       # max rows per flush attempt (avoids giant transactions)


class _State(Enum):
    CLOSED    = auto()
    OPEN      = auto()
    HALF_OPEN = auto()


class DBCircuitBreaker:
    """
    Async-safe circuit breaker that buffers failed Postgres writes to a local
    JSON Lines file and retries them on a background loop.

    Parameters
    ----------
    pool : asyncpg.Pool
        Live asyncpg connection pool.
    buffer_path : Path | None
        Override the default buffer file location (useful in tests).
    flush_interval : float
        Seconds between drain attempts when circuit is OPEN.
    failure_threshold : int
        Consecutive failures before the circuit trips OPEN.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        buffer_path: Path | None = None,
        flush_interval: float = _FLUSH_INTERVAL,
        failure_threshold: int = _FAILURE_THRESHOLD,
    ) -> None:
        self._pool             = pool
        self._buffer_path      = buffer_path or _BUFFER_PATH
        self._flush_interval   = flush_interval
        self._failure_threshold = failure_threshold

        self._state       : _State              = _State.CLOSED
        self._lock        : asyncio.Lock        = asyncio.Lock()
        self._buffer      : deque[dict[str, Any]] = deque()
        self._failure_count: int                = 0
        self._last_failure : float              = 0.0

        self._load_buffer()

    # ── public API ───────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state.name

    async def write(self, table: str, row: dict[str, Any]) -> None:
        """
        Write *row* to *table*.

        • If CLOSED: attempt direct DB insert; on failure increment failure
          counter, buffer the row, and open the circuit once the threshold
          is reached.
        • If OPEN/HALF_OPEN: buffer immediately.
        """
        async with self._lock:
            if self._state is _State.CLOSED:
                try:
                    await self._db_insert(table, row)
                    self._failure_count = 0
                    return
                except Exception as exc:
                    self._failure_count += 1
                    self._last_failure = time.monotonic()
                    logger.warning(
                        "DB write failed (%d/%d): %s",
                        self._failure_count, self._failure_threshold, exc,
                    )
                    if self._failure_count >= self._failure_threshold:
                        self._state = _State.OPEN
                        logger.error(
                            "Circuit OPEN after %d failures — buffering to %s",
                            self._failure_count, self._buffer_path,
                        )

            # Buffer the row (OPEN, HALF_OPEN, or CLOSED after first failure)
            entry: dict[str, Any] = {"table": table, "row": row}
            self._buffer.append(entry)
            self._append_to_file(entry)

    async def flush_loop(self) -> None:
        """
        Background coroutine — schedule with ``asyncio.create_task()``.

        Sleeps for *flush_interval* seconds then attempts to drain the buffer
        when the circuit is OPEN.  Runs forever until task is cancelled.
        """
        logger.info("DBCircuitBreaker flush loop started (interval=%ss)", self._flush_interval)
        while True:
            await asyncio.sleep(self._flush_interval)
            if self._state is _State.OPEN and self._buffer:
                await self._try_flush()

    # ── internal helpers ─────────────────────────────────────────────────────

    async def _try_flush(self) -> None:
        """
        Attempt to drain the in-memory buffer to Postgres.

        Algorithm
        ---------
        1. Under lock: snapshot up to _FLUSH_BATCH_SIZE rows and clear them
           from the working buffer so new writes can proceed concurrently.
        2. Outside lock: write each row in order; stop on first error.
        3. Under lock: prepend any un-flushed rows back to the *front* of the
           buffer (preserving global order) and persist the buffer to disk.
        """
        async with self._lock:
            if not self._buffer:
                self._state = _State.CLOSED
                return
            self._state = _State.HALF_OPEN
            # Snapshot a bounded batch; leave the rest in the deque.
            pending: list[dict[str, Any]] = []
            for _ in range(min(_FLUSH_BATCH_SIZE, len(self._buffer))):
                pending.append(self._buffer.popleft())

        logger.info("Circuit HALF_OPEN — attempting to flush %d buffered rows", len(pending))

        failed_from: int = len(pending)  # sentinel: all succeeded
        for i, entry in enumerate(pending):
            try:
                await self._db_insert(entry["table"], entry["row"])
            except Exception as exc:
                logger.warning("Flush failed at row %d: %s", i, exc)
                failed_from = i
                break

        succeeded = pending[:failed_from]
        remaining = pending[failed_from:]          # rows that weren't flushed

        async with self._lock:
            if remaining:
                # Prepend un-flushed rows back to the front, maintaining order.
                # deque.appendleft inserts each item at position 0, so we loop
                # in *reverse* to end up with original order at the front.
                for entry in reversed(remaining):
                    self._buffer.appendleft(entry)
                self._state = _State.OPEN
                self._failure_count += 1
                logger.warning(
                    "Partial flush: %d succeeded, %d re-buffered. Circuit OPEN.",
                    len(succeeded), len(remaining),
                )
            else:
                if not self._buffer:
                    self._state = _State.CLOSED
                    self._failure_count = 0
                    logger.info(
                        "Circuit CLOSED — all %d buffered rows flushed.",
                        len(succeeded),
                    )
                else:
                    # More rows remain from concurrent writes; stay OPEN, next loop.
                    self._state = _State.OPEN
                    logger.info(
                        "Batch flushed (%d rows); %d remain — will retry.",
                        len(succeeded), len(self._buffer),
                    )
            self._persist_buffer()

    async def _db_insert(self, table: str, row: dict[str, Any]) -> None:
        """Single-row INSERT via asyncpg with a per-query timeout."""
        cols         = ", ".join(row.keys())
        placeholders = ", ".join(f"${i + 1}" for i in range(len(row)))
        sql          = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        async with asyncio.timeout(_DB_TIMEOUT):
            await self._pool.execute(sql, *row.values())

    # ── file I/O (called under lock) ─────────────────────────────────────────

    def _append_to_file(self, entry: dict[str, Any]) -> None:
        """
        Append a single JSON line to the buffer file.

        A single write() call for a small JSON object is atomic on POSIX
        (< PIPE_BUF ≈ 4 KB).  fsync ensures the kernel flushes to disk.
        """
        try:
            with self._buffer_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            logger.error("Failed to append to buffer file %s: %s", self._buffer_path, exc)

    def _persist_buffer(self) -> None:
        """
        Atomically rewrite the buffer file from the current in-memory deque.

        Uses write-to-temp + os.rename() (atomic on POSIX) to avoid a
        half-written file if the process is killed mid-write.
        """
        if not self._buffer:
            self._buffer_path.unlink(missing_ok=True)
            return
        tmp = self._buffer_path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                for entry in self._buffer:
                    fh.write(json.dumps(entry, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            tmp.rename(self._buffer_path)
        except OSError as exc:
            logger.error("Failed to persist buffer file: %s", exc)

    def _load_buffer(self) -> None:
        """
        Load persisted rows from disk into memory on startup.  Called from
        __init__ (synchronous — no pool required).

        If the file exists with entries, the circuit starts in OPEN state so
        flush_loop() will attempt to drain it immediately.
        """
        if not self._buffer_path.exists():
            return
        loaded = 0
        try:
            with self._buffer_path.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._buffer.append(json.loads(line))
                        loaded += 1
                    except json.JSONDecodeError:
                        logger.warning(
                            "Skipping malformed line %d in %s",
                            lineno, self._buffer_path,
                        )
        except OSError as exc:
            logger.warning("Could not read buffer file %s: %s", self._buffer_path, exc)
            return

        if loaded:
            self._state = _State.OPEN
            logger.info(
                "Recovered %d buffered rows from %s — circuit starts OPEN",
                loaded, self._buffer_path,
            )
