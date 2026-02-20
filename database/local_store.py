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

import csv
import io
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

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
            await self._init_trade_journal(db)
            await self._init_account_snapshots(db)
            await db.commit()
        self._initialised = True
        logger.info("LocalStore ready at %s", self._db_path)

    async def _init_trade_journal(self, db: aiosqlite.Connection) -> None:
        """Create trade_journal table and indexes (idempotent)."""
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_journal (
                id                TEXT PRIMARY KEY,
                created_at        TEXT NOT NULL,
                broker            TEXT NOT NULL DEFAULT '',
                account_id        TEXT NOT NULL DEFAULT '',
                broker_order_id   TEXT,
                underlying        TEXT NOT NULL DEFAULT '',
                strategy_tag      TEXT,
                status            TEXT NOT NULL DEFAULT 'FILLED',
                legs_json         TEXT NOT NULL DEFAULT '[]',
                net_debit_credit  REAL,
                vix_at_fill       REAL,
                spx_price_at_fill REAL,
                regime            TEXT,
                pre_greeks_json   TEXT NOT NULL DEFAULT '{}',
                post_greeks_json  TEXT NOT NULL DEFAULT '{}',
                user_rationale    TEXT,
                ai_rationale      TEXT,
                ai_suggestion_id  TEXT
            );
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_tj_created_at ON trade_journal(created_at DESC);"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_tj_account ON trade_journal(account_id, created_at DESC);"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_tj_underlying ON trade_journal(underlying, created_at DESC);"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_tj_regime ON trade_journal(regime, created_at DESC);"
        )

    async def _init_account_snapshots(self, db: aiosqlite.Connection) -> None:
        """Create account_snapshots table and indexes (idempotent)."""
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id                TEXT PRIMARY KEY,
                captured_at       TEXT NOT NULL,
                account_id        TEXT NOT NULL DEFAULT '',
                broker            TEXT NOT NULL DEFAULT '',
                net_liquidation   REAL,
                cash_balance      REAL,
                spx_delta         REAL,
                gamma             REAL,
                theta             REAL,
                vega              REAL,
                delta_theta_ratio REAL,
                vix               REAL,
                spx_price         REAL,
                regime            TEXT
            );
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_as_captured_at ON account_snapshots(captured_at DESC);"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS ix_as_account ON account_snapshots(account_id, captured_at DESC);"
        )

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
    # Trade Journal (003-algo-execution-platform)                        #
    # ------------------------------------------------------------------ #

    async def record_fill(self, entry: Any) -> str:
        """Insert a trade fill into trade_journal and return its UUID.

        Accepts a :class:`models.order.TradeJournalEntry` or any object with
        matching attributes.  Uses REPLACE to handle duplicate broker_order_id
        gracefully (idempotent re-delivery of the same fill).
        """
        await self._ensure_init()
        entry_id = getattr(entry, "entry_id", None) or str(uuid.uuid4())
        created_at = getattr(entry, "created_at", None) or datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO trade_journal (
                    id, created_at, broker, account_id, broker_order_id,
                    underlying, strategy_tag, status, legs_json,
                    net_debit_credit, vix_at_fill, spx_price_at_fill, regime,
                    pre_greeks_json, post_greeks_json,
                    user_rationale, ai_rationale, ai_suggestion_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                """,
                (
                    entry_id,
                    created_at,
                    getattr(entry, "broker", ""),
                    getattr(entry, "account_id", ""),
                    getattr(entry, "broker_order_id", None),
                    getattr(entry, "underlying", ""),
                    getattr(entry, "strategy_tag", None),
                    getattr(entry, "status", "FILLED"),
                    getattr(entry, "legs_json", "[]"),
                    getattr(entry, "net_debit_credit", None),
                    getattr(entry, "vix_at_fill", None),
                    getattr(entry, "spx_price_at_fill", None),
                    getattr(entry, "regime", None),
                    getattr(entry, "pre_greeks_json", "{}"),
                    getattr(entry, "post_greeks_json", "{}"),
                    getattr(entry, "user_rationale", None),
                    getattr(entry, "ai_rationale", None),
                    getattr(entry, "ai_suggestion_id", None),
                ),
            )
            await db.commit()
        logger.debug("Recorded fill %s for %s", entry_id, getattr(entry, "underlying", ""))
        return entry_id

    async def query_journal(
        self,
        *,
        start_dt: Optional[str] = None,
        end_dt: Optional[str] = None,
        instrument: Optional[str] = None,
        regime: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Query trade_journal with optional filters.  Returns rows newest-first."""
        await self._ensure_init()
        clauses: list[str] = []
        params: list[Any] = []
        if start_dt:
            clauses.append("created_at >= ?")
            params.append(start_dt)
        if end_dt:
            clauses.append("created_at <= ?")
            params.append(end_dt)
        if instrument:
            clauses.append("(underlying LIKE ? OR legs_json LIKE ?)")
            like = f"%{instrument}%"
            params.extend([like, like])
        if regime:
            clauses.append("regime = ?")
            params.append(regime)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""
                SELECT * FROM trade_journal
                {where}
                ORDER BY created_at DESC
                LIMIT ?;
                """,
                params,
            ) as cursor:
                rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    def export_csv(self, entries: list[dict[str, Any]]) -> str:
        """Serialise a list of trade_journal row dicts to a CSV string."""
        if not entries:
            return ""
        fieldnames = [
            "id", "created_at", "broker", "account_id", "broker_order_id",
            "underlying", "strategy_tag", "status", "legs_json",
            "net_debit_credit", "vix_at_fill", "spx_price_at_fill", "regime",
            "pre_greeks_json", "post_greeks_json",
            "user_rationale", "ai_rationale", "ai_suggestion_id",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(entries)
        return buf.getvalue()

    # ------------------------------------------------------------------ #
    # Account Snapshots (003-algo-execution-platform)                    #
    # ------------------------------------------------------------------ #

    async def capture_snapshot(self, snapshot: Any) -> str:
        """Insert one account snapshot row and return its UUID."""
        await self._ensure_init()
        snap_id = getattr(snapshot, "snapshot_id", None) or str(uuid.uuid4())
        captured_at = getattr(snapshot, "captured_at", None) or datetime.now(timezone.utc).isoformat()

        # Pre-compute delta_theta_ratio from the snapshot's own theta/delta if not set
        delta_theta_ratio = getattr(snapshot, "delta_theta_ratio", None)
        if delta_theta_ratio is None:
            spx_delta = getattr(snapshot, "spx_delta", None)
            theta = getattr(snapshot, "theta", None)
            if spx_delta and spx_delta != 0.0 and theta is not None:
                delta_theta_ratio = theta / spx_delta

        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO account_snapshots (
                    id, captured_at, account_id, broker,
                    net_liquidation, cash_balance,
                    spx_delta, gamma, theta, vega, delta_theta_ratio,
                    vix, spx_price, regime
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?);
                """,
                (
                    snap_id,
                    captured_at,
                    getattr(snapshot, "account_id", ""),
                    getattr(snapshot, "broker", ""),
                    getattr(snapshot, "net_liquidation", None),
                    getattr(snapshot, "cash_balance", None),
                    getattr(snapshot, "spx_delta", None),
                    getattr(snapshot, "gamma", None),
                    getattr(snapshot, "theta", None),
                    getattr(snapshot, "vega", None),
                    delta_theta_ratio,
                    getattr(snapshot, "vix", None),
                    getattr(snapshot, "spx_price", None),
                    getattr(snapshot, "regime", None),
                ),
            )
            await db.commit()
        logger.debug("Captured snapshot %s at %s", snap_id, captured_at)
        return snap_id

    async def query_snapshots(
        self,
        *,
        start_dt: Optional[str] = None,
        end_dt: Optional[str] = None,
        account_id: Optional[str] = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        """Query account_snapshots (ascending by captured_at for chart rendering)."""
        await self._ensure_init()
        clauses: list[str] = []
        params: list[Any] = []
        if start_dt:
            clauses.append("captured_at >= ?")
            params.append(start_dt)
        if end_dt:
            clauses.append("captured_at <= ?")
            params.append(end_dt)
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""
                SELECT * FROM account_snapshots
                {where}
                ORDER BY captured_at ASC
                LIMIT ?;
                """,
                params,
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
