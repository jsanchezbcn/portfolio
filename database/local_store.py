"""database/local_store.py — SQLite-backed market_intel store.

Drop-in replacement for the ``market_intel`` methods of :class:`DBManager`
when PostgreSQL is unavailable.  Uses :mod:`aiosqlite` (async SQLite).

The SQLite file is created automatically at ``./data/market_intel.db``
(relative to the project root).  Change ``DB_PATH`` or pass a custom path
to the constructor.

Thread-safety note: aiosqlite wraps a dedicated worker thread, so it is safe
to call from any event-loop coroutine as long as you don't share a single
:class:`LocalStore` instance across *multiple* event-loops simultaneously
(the normal usage pattern for this project).
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite  # type: ignore[import]

logger = logging.getLogger(__name__)

# Default path (relative to project root; created on first use)
_DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "market_intel.db"
)


class LocalStore:
    """Async SQLite store with the same ``market_intel`` interface as DBManager.

    Args:
        db_path: Absolute or relative path for the SQLite file.
                 Defaults to ``<project_root>/data/market_intel.db``.
    """

    def __init__(self, db_path: str = _DEFAULT_DB_PATH) -> None:
        self._db_path = db_path
        self._initialised = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def _ensure_init(self) -> None:
        """Create the table schema the first time we open the DB."""
        if self._initialised:
            return
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS market_intel (
                    id          TEXT PRIMARY KEY,
                    trade_id    TEXT,
                    symbol      TEXT NOT NULL,
                    source      TEXT NOT NULL,
                    content     TEXT NOT NULL DEFAULT '',
                    sentiment_score REAL,
                    created_at  TEXT NOT NULL
                );
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS ix_mi_symbol ON market_intel(symbol);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS ix_mi_source ON market_intel(source);"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS ix_mi_created ON market_intel(created_at);"
            )
            await db.commit()
        self._initialised = True
        logger.info("LocalStore ready at %s", self._db_path)

    # ------------------------------------------------------------------ #
    # Write methods (same signatures as DBManager)                        #
    # ------------------------------------------------------------------ #

    async def insert_market_intel(
        self,
        *,
        symbol: str,
        source: str,
        content: str,
        sentiment_score: float | None = None,
        trade_id: str | None = None,
    ) -> str:
        """Insert a news/sentiment record and return the new UUID."""
        await self._ensure_init()
        new_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO market_intel
                    (id, trade_id, symbol, source, content, sentiment_score, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (new_id, trade_id, symbol, source, content, sentiment_score, now),
            )
            await db.commit()
        logger.debug("Inserted market_intel %s for %s/%s", new_id, symbol, source)
        return new_id

    async def upsert_market_intel(
        self,
        *,
        symbol: str,
        source: str,
        sentiment_score: float | None = None,
        summary: str = "",
        raw_data: dict[str, Any] | None = None,
    ) -> str:
        """Insert or replace the single latest row for (symbol, source).

        Mirrors the PostgreSQL upsert: delete existing row, insert fresh one.
        This keeps one live result per (symbol, source) — used by LLM agents.
        """
        await self._ensure_init()
        content = summary
        if raw_data:
            try:
                content = json.dumps(raw_data)
            except (TypeError, ValueError):
                content = summary

        new_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "DELETE FROM market_intel WHERE symbol = ? AND source = ?;",
                (symbol, source),
            )
            await db.execute(
                """
                INSERT INTO market_intel
                    (id, symbol, source, content, sentiment_score, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (new_id, symbol, source, content, sentiment_score, now),
            )
            await db.commit()
        logger.debug("Upserted market_intel %s for %s/%s", new_id, symbol, source)
        return new_id

    # ------------------------------------------------------------------ #
    # Read methods (same signatures as DBManager)                         #
    # ------------------------------------------------------------------ #

    async def get_recent_market_intel(
        self, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return the most recent *limit* rows across all symbols."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, trade_id, symbol, source, content, sentiment_score, created_at
                FROM market_intel
                ORDER BY created_at DESC
                LIMIT ?;
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_market_intel_by_source(
        self,
        source: str,
        *,
        symbol: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Return recent rows for *source*, optionally filtered by *symbol*."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if symbol:
                async with db.execute(
                    """
                    SELECT id, symbol, source, content, sentiment_score, created_at
                    FROM market_intel
                    WHERE source = ? AND symbol = ?
                    ORDER BY created_at DESC
                    LIMIT ?;
                    """,
                    (source, symbol, limit),
                ) as cursor:
                    rows = await cursor.fetchall()
            else:
                async with db.execute(
                    """
                    SELECT id, symbol, source, content, sentiment_score, created_at
                    FROM market_intel
                    WHERE source = ?
                    ORDER BY created_at DESC
                    LIMIT ?;
                    """,
                    (source, limit),
                ) as cursor:
                    rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_market_intel_for_trade(
        self, trade_id: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return recent market_intel rows for *trade_id*."""
        await self._ensure_init()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, trade_id, symbol, source, content, sentiment_score, created_at
                FROM market_intel
                WHERE trade_id = ?
                ORDER BY created_at DESC
                LIMIT ?;
                """,
                (trade_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Convenience: stubs for non-market_intel methods the dashboard calls #
    # ------------------------------------------------------------------ #

    async def get_active_signals(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Stub — signals table not in LocalStore; returns empty list."""
        return []

    async def connect(self) -> None:
        """No-op; LocalStore lazily initialises on first query."""
        await self._ensure_init()

    async def close(self) -> None:
        """No-op; aiosqlite connections are opened/closed per-query."""
