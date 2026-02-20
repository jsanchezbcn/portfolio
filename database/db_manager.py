from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from typing import Any, Optional

import asyncpg


@dataclass(slots=True)
class GreekSnapshotRecord:
    event_time: datetime
    received_at: datetime
    broker: str
    account_id: str
    underlying: str
    contract_key: str
    expiration: Optional[date] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    quantity: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    rho: Optional[float] = None
    implied_volatility: Optional[float] = None
    underlying_price: Optional[float] = None
    source_payload: Optional[dict[str, Any]] = None


@dataclass(slots=True)
class TradeEntry:
    broker: str
    account_id: str
    symbol: str
    action: str
    quantity: float
    contract_key: Optional[str] = None
    price: Optional[float] = None
    strategy_tag: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class DBManager:
    _instance: Optional["DBManager"] = None
    _instance_lock = asyncio.Lock()

    def __init__(self, *, flush_interval_seconds: float = 1.0, flush_batch_size: int = 50) -> None:
        self.flush_interval_seconds = flush_interval_seconds
        self.flush_batch_size = flush_batch_size
        self._pool: Optional[asyncpg.Pool] = None
        self._buffer: list[GreekSnapshotRecord] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._db_lock = asyncio.Lock()

    @classmethod
    async def get_instance(cls) -> "DBManager":
        async with cls._instance_lock:
            if cls._instance is None:
                cls._instance = DBManager()
            return cls._instance

    @property
    def dsn(self) -> str:
        host = os.getenv("DB_HOST", "localhost")
        port = os.getenv("DB_PORT", "5432")
        name = os.getenv("DB_NAME", "portfolio_engine")
        user = os.getenv("DB_USER", "portfolio")
        password = os.getenv("DB_PASS", "")
        return f"postgresql://{user}:{password}@{host}:{port}/{name}"

    @property
    def _redacted_dsn(self) -> str:
        """Issue 12: Return a log-safe DSN with the password replaced by '***'."""
        return re.sub(r":(.[^:@]*)@", ":***@", self.dsn, count=1)

    async def connect(self) -> None:
        if self._pool is not None:
            return
        async with self._db_lock:
            if self._pool is not None:
                return
            self._pool = await asyncpg.create_pool(
                dsn=self.dsn,
                min_size=int(os.getenv("DB_POOL_MIN", "1")),
                max_size=int(os.getenv("DB_POOL_MAX", "10")),
                command_timeout=float(os.getenv("DB_COMMAND_TIMEOUT", "10")),
            )
            await self.ensure_schema()

    async def close(self) -> None:
        self._stopping = True
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush_now()
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ensure_schema(self) -> None:
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")

        create_trades = """
        CREATE EXTENSION IF NOT EXISTS pgcrypto;

        CREATE TABLE IF NOT EXISTS trades (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            broker TEXT NOT NULL,
            account_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            contract_key TEXT,
            action TEXT NOT NULL,
            quantity NUMERIC NOT NULL,
            price NUMERIC,
            strategy_tag TEXT,
            metadata JSONB
        );
        """

        create_snapshots_parent = """
        CREATE TABLE IF NOT EXISTS greek_snapshots (
            id BIGSERIAL,
            event_time TIMESTAMPTZ NOT NULL,
            received_at TIMESTAMPTZ NOT NULL,
            persisted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            broker TEXT NOT NULL,
            account_id TEXT NOT NULL,
            underlying TEXT NOT NULL,
            contract_key TEXT NOT NULL,
            expiration DATE,
            strike NUMERIC,
            option_type TEXT,
            quantity NUMERIC,
            delta DOUBLE PRECISION,
            gamma DOUBLE PRECISION,
            theta DOUBLE PRECISION,
            vega DOUBLE PRECISION,
            rho DOUBLE PRECISION,
            implied_volatility DOUBLE PRECISION,
            underlying_price DOUBLE PRECISION,
            source_payload JSONB NOT NULL DEFAULT '{}'::JSONB,
            PRIMARY KEY (id, event_time)
        ) PARTITION BY RANGE (event_time);
        """

        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        if now.month == 12:
            next_month = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
            partition_name = f"greek_snapshots_{now.year}_12"
        else:
            next_month = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
            partition_name = f"greek_snapshots_{now.year}_{now.month:02d}"

        create_partition = f"""
        CREATE TABLE IF NOT EXISTS {partition_name}
        PARTITION OF greek_snapshots
        FOR VALUES FROM ('{month_start.isoformat()}') TO ('{next_month.isoformat()}');
        """

        create_indexes = """
        CREATE INDEX IF NOT EXISTS idx_greek_snapshots_event_time ON greek_snapshots (event_time DESC);
        CREATE INDEX IF NOT EXISTS idx_greek_snapshots_lookup ON greek_snapshots (broker, account_id, contract_key, event_time DESC);
        """

        # T004: staged_orders — orders staged in TWS with transmit=False
        create_staged_orders = """
        CREATE TABLE IF NOT EXISTS staged_orders (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tws_order_id TEXT,
            account_id TEXT NOT NULL,
            instrument_type TEXT NOT NULL,
            symbol TEXT NOT NULL,
            expiration DATE,
            strike NUMERIC,
            quantity NUMERIC NOT NULL,
            direction TEXT NOT NULL,
            limit_price NUMERIC,
            status TEXT NOT NULL DEFAULT 'STAGED',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """

        # T005: market_intel — sentiment records written by NewsSentry
        create_market_intel = """
        CREATE TABLE IF NOT EXISTS market_intel (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            trade_id UUID,
            symbol TEXT NOT NULL,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            sentiment_score DOUBLE PRECISION,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_market_intel_symbol ON market_intel (symbol, created_at DESC);
        """

        # T006: signals — arbitrage opportunity signals from ArbHunter
        create_signals = """
        CREATE TABLE IF NOT EXISTS signals (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            signal_type TEXT NOT NULL,
            legs_json JSONB NOT NULL DEFAULT '{}'::JSONB,
            net_value NUMERIC,
            confidence DOUBLE PRECISION,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_signals_status ON signals (status, detected_at DESC);
        """

        # T007: trade_journal — entry context for ExplainPerformanceSkill
        create_trade_journal = """
        CREATE TABLE IF NOT EXISTS trade_journal (
            trade_id UUID PRIMARY KEY,
            symbol TEXT NOT NULL,
            entry_greeks_json JSONB NOT NULL DEFAULT '{}'::JSONB,
            thesis TEXT,
            entry_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """

        # T008: worker_jobs — async job queue for background workers
        create_worker_jobs = """
        CREATE TABLE IF NOT EXISTS worker_jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            job_type TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::JSONB,
            status TEXT NOT NULL DEFAULT 'pending',
            result JSONB,
            error TEXT,
            worker_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_worker_jobs_status ON worker_jobs (job_type, status, created_at DESC);
        """

        async with self._pool.acquire() as conn:
            await conn.execute(create_trades)
            await conn.execute(create_snapshots_parent)
            await conn.execute(create_partition)
            await conn.execute(create_indexes)
            await conn.execute(create_staged_orders)
            await conn.execute(create_market_intel)
            await conn.execute(create_signals)
            await conn.execute(create_trade_journal)
            await conn.execute(create_worker_jobs)

    async def start_background_flush(self) -> None:
        if self._flush_task and not self._flush_task.done():
            return
        self._stopping = False
        self._flush_task = asyncio.create_task(self._flush_loop(), name="db-snapshot-flush-loop")

    async def _flush_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.flush_interval_seconds)
            await self.flush_now()

    async def enqueue_snapshot(self, record: GreekSnapshotRecord) -> None:
        async with self._buffer_lock:
            self._buffer.append(record)
            should_flush = len(self._buffer) >= self.flush_batch_size
        if should_flush:
            await self.flush_now()

    async def batch_insert_snapshots(self, data: list[GreekSnapshotRecord]) -> int:
        if not data:
            return 0
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")

        query = """
        INSERT INTO greek_snapshots (
            event_time, received_at, broker, account_id, underlying, contract_key,
            expiration, strike, option_type, quantity,
            delta, gamma, theta, vega, rho,
            implied_volatility, underlying_price, source_payload
        ) VALUES (
            $1, $2, $3, $4, $5, $6,
            $7, $8, $9, $10,
            $11, $12, $13, $14, $15,
            $16, $17, $18
        );
        """

        rows = [
            (
                item.event_time,
                item.received_at,
                item.broker,
                item.account_id,
                item.underlying,
                item.contract_key,
                item.expiration,
                item.strike,
                item.option_type,
                item.quantity,
                item.delta,
                item.gamma,
                item.theta,
                item.vega,
                item.rho,
                item.implied_volatility,
                item.underlying_price,
                item.source_payload or {},
            )
            for item in data
        ]

        async with self._pool.acquire() as conn:
            await conn.executemany(query, rows)
        return len(rows)

    async def flush_now(self) -> int:
        async with self._buffer_lock:
            if not self._buffer:
                return 0
            batch = self._buffer[:]
            self._buffer.clear()
        return await self.batch_insert_snapshots(batch)

    async def insert_trade(self, trade: TradeEntry) -> None:
        if trade.quantity == 0:
            raise ValueError("Trade quantity cannot be zero")
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")

        query = """
        INSERT INTO trades (broker, account_id, symbol, contract_key, action, quantity, price, strategy_tag, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9);
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                query,
                trade.broker,
                trade.account_id,
                trade.symbol,
                trade.contract_key,
                trade.action,
                trade.quantity,
                trade.price,
                trade.strategy_tag,
                trade.metadata or {},
            )

    async def fetch_snapshots(
        self,
        *,
        broker: str | None = None,
        account_id: str | None = None,
        contract_key: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")

        conditions = []
        args: list[Any] = []

        def add_condition(field: str, value: Any) -> None:
            args.append(value)
            conditions.append(f"{field} = ${len(args)}")

        if broker:
            add_condition("broker", broker)
        if account_id:
            add_condition("account_id", account_id)
        if contract_key:
            add_condition("contract_key", contract_key)
        if from_time:
            args.append(from_time)
            conditions.append(f"event_time >= ${len(args)}")
        if to_time:
            args.append(to_time)
            conditions.append(f"event_time <= ${len(args)}")

        args.append(limit)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"""
        SELECT event_time, received_at, persisted_at, broker, account_id, underlying, contract_key,
               expiration, strike, option_type, quantity, delta, gamma, theta, vega, rho,
               implied_volatility, underlying_price, source_payload
        FROM greek_snapshots
        {where_clause}
        ORDER BY event_time DESC
        LIMIT ${len(args)};
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
        return [dict(row) for row in rows]

    @staticmethod
    def snapshot_from_mapping(mapping: dict[str, Any]) -> GreekSnapshotRecord:
        _known_fields = {
            "event_time", "received_at", "broker", "account_id", "underlying",
            "contract_key", "expiration", "strike", "option_type", "quantity",
            "delta", "gamma", "theta", "vega", "rho", "implied_volatility",
            "underlying_price", "source_payload",
        }
        payload = {k: v for k, v in mapping.items() if k in _known_fields}
        payload.setdefault("received_at", datetime.now(timezone.utc))
        return GreekSnapshotRecord(**payload)

    @staticmethod
    def trade_from_mapping(mapping: dict[str, Any]) -> TradeEntry:
        return TradeEntry(**mapping)

    @staticmethod
    def as_dict(record: GreekSnapshotRecord) -> dict[str, Any]:
        return asdict(record)

    # ------------------------------------------------------------------ #
    # T016 — staged_orders                                                 #
    # ------------------------------------------------------------------ #

    async def insert_staged_order(
        self,
        *,
        tws_order_id: str | None,
        account_id: str,
        instrument_type: str,
        symbol: str,
        quantity: float,
        direction: str,
        limit_price: float | None = None,
        expiration: date | None = None,
        strike: float | None = None,
        status: str = "STAGED",
    ) -> str:
        """Insert a staged order record and return the new UUID as a string."""
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        INSERT INTO staged_orders
            (tws_order_id, account_id, instrument_type, symbol, expiration,
             strike, quantity, direction, limit_price, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING id::TEXT;
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                tws_order_id,
                account_id,
                instrument_type,
                symbol,
                expiration,
                strike,
                quantity,
                direction,
                limit_price,
                status,
            )
        return row["id"]  # type: ignore[index]

    # ------------------------------------------------------------------ #
    # T025 — market_intel                                                  #
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
        """Insert a sentiment/news record and return the new UUID as a string."""
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        INSERT INTO market_intel (trade_id, symbol, source, content, sentiment_score)
        VALUES ($1::UUID, $2, $3, $4, $5)
        RETURNING id::TEXT;
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                trade_id,
                symbol,
                source,
                content,
                sentiment_score,
            )
        return row["id"]  # type: ignore[index]

    async def get_market_intel_for_trade(
        self, trade_id: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return recent market_intel rows associated with *trade_id*."""
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        SELECT id::TEXT, trade_id::TEXT, symbol, source, content, sentiment_score, created_at
        FROM market_intel
        WHERE trade_id = $1::UUID
        ORDER BY created_at DESC
        LIMIT $2;
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, trade_id, limit)
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # T035 — signals                                                       #
    # ------------------------------------------------------------------ #

    async def insert_signal(
        self,
        *,
        signal_type: str,
        legs_json: dict[str, Any],
        net_value: float | None = None,
        confidence: float | None = None,
        status: str = "ACTIVE",
    ) -> str:
        """Insert an arbitrage signal record and return the new UUID as a string."""
        import json as _json

        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        INSERT INTO signals (signal_type, legs_json, net_value, confidence, status)
        VALUES ($1, $2::JSONB, $3, $4, $5)
        RETURNING id::TEXT;
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                signal_type,
                _json.dumps(legs_json),
                net_value,
                confidence,
                status,
            )
        return row["id"]  # type: ignore[index]

    async def expire_stale_signals(self, *, active_ids: list[str]) -> int:
        """Mark all ACTIVE signals whose IDs are NOT in *active_ids* as 'EXPIRED'.

        Returns the number of rows updated.
        """
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        UPDATE signals
        SET status = 'EXPIRED', updated_at = NOW()
        WHERE status = 'ACTIVE'
          AND ($1::UUID[] IS NULL OR id <> ALL($1::UUID[]))
        """
        # Pass None when no active IDs to expire everything active.
        ids_param: list[str] | None = active_ids if active_ids else None
        async with self._pool.acquire() as conn:
            result = await conn.execute(query, ids_param)
        # asyncpg returns e.g. "UPDATE 3" — parse the count.
        try:
            return int(result.split()[-1])
        except (AttributeError, IndexError, ValueError):
            return 0

    # ------------------------------------------------------------------ #
    # T007b / T046 — trade_journal                                        #
    # ------------------------------------------------------------------ #

    async def insert_trade_journal_entry(
        self,
        *,
        trade_id: str,
        symbol: str,
        entry_greeks_json: dict[str, Any],
        thesis: str | None = None,
    ) -> None:
        """Insert (or upsert) a trade_journal row for *trade_id*."""
        import json as _json

        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        INSERT INTO trade_journal (trade_id, symbol, entry_greeks_json, thesis)
        VALUES ($1::UUID, $2, $3::JSONB, $4)
        ON CONFLICT (trade_id) DO UPDATE
            SET entry_greeks_json = EXCLUDED.entry_greeks_json,
                thesis = EXCLUDED.thesis;
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                query,
                trade_id,
                symbol,
                _json.dumps(entry_greeks_json),
                thesis,
            )

    async def get_trade_journal_entry(self, trade_id: str) -> dict[str, Any] | None:
        """Return the trade_journal row for *trade_id*, or None if not found."""
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        SELECT trade_id::TEXT, symbol, entry_greeks_json, thesis, entry_at
        FROM trade_journal
        WHERE trade_id = $1::UUID;
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, trade_id)
        return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # Dashboard read helpers                                               #
    # ------------------------------------------------------------------ #

    async def get_recent_market_intel(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return the most recent *limit* market_intel rows across all symbols."""
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        SELECT id::TEXT, trade_id::TEXT, symbol, source, content, sentiment_score, created_at
        FROM market_intel
        ORDER BY created_at DESC
        LIMIT $1;
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, limit)
        return [dict(r) for r in rows]

    async def upsert_market_intel(
        self,
        *,
        symbol: str,
        source: str,
        sentiment_score: float | None = None,
        summary: str = "",
        raw_data: dict[str, Any] | None = None,
    ) -> str:
        """Insert or replace the single latest record for (symbol, source).

        Keeps only the most recent row per (symbol, source) by deleting
        any existing row first, then inserting a fresh one.  Used by
        LLM agents (llm_risk_audit, llm_brief) that maintain one live
        result per source rather than an append-only log.

        Returns the new UUID as a string.
        """
        import json as _json

        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        delete_query = """
        DELETE FROM market_intel
        WHERE symbol = $1 AND source = $2;
        """
        insert_query = """
        INSERT INTO market_intel (symbol, source, content, sentiment_score)
        VALUES ($1, $2, $3, $4)
        RETURNING id::TEXT;
        """
        content = summary
        if raw_data:
            # Encode raw_data as JSON and append to content for easy recovery
            try:
                content = _json.dumps(raw_data)
            except (TypeError, ValueError):
                content = summary
        async with self._pool.acquire() as conn:
            await conn.execute(delete_query, symbol, source)
            row = await conn.fetchrow(insert_query, symbol, source, content, sentiment_score)
        return row["id"]  # type: ignore[index]

    async def get_market_intel_by_source(
        self, source: str, *, symbol: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Return the most recent rows for a given *source*, optionally filtered by *symbol*."""
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        if symbol:
            query = """
            SELECT id::TEXT, symbol, source, content, sentiment_score, created_at
            FROM market_intel
            WHERE source = $1 AND symbol = $2
            ORDER BY created_at DESC
            LIMIT $3;
            """
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(query, source, symbol, limit)
        else:
            query = """
            SELECT id::TEXT, symbol, source, content, sentiment_score, created_at
            FROM market_intel
            WHERE source = $1
            ORDER BY created_at DESC
            LIMIT $2;
            """
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(query, source, limit)
        return [dict(r) for r in rows]

    async def get_active_signals(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return active (non-expired) arbitrage signals, newest first."""
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        SELECT id::TEXT, signal_type, legs_json, net_value, confidence, status,
               detected_at, updated_at
        FROM signals
        WHERE status = 'ACTIVE'
        ORDER BY detected_at DESC
        LIMIT $1;
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, limit)
        return [dict(r) for r in rows]

    # ── Worker job queue ──────────────────────────────────────────────────────

    async def enqueue_job(
        self,
        job_type: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Insert a new pending job; return the UUID as a string."""
        import json as _json

        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        payload_str = _json.dumps(payload or {})
        query = """
        INSERT INTO worker_jobs (job_type, payload, status, created_at, updated_at)
        VALUES ($1, $2::JSONB, 'pending', NOW(), NOW())
        RETURNING id::TEXT;
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, job_type, payload_str)
        return row["id"]  # type: ignore[index]

    async def claim_next_job(self, worker_id: str) -> dict[str, Any] | None:
        """Atomically claim the oldest pending job; return the row or None."""
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        UPDATE worker_jobs
        SET status = 'running', worker_id = $1, updated_at = NOW()
        WHERE id = (
            SELECT id FROM worker_jobs
            WHERE status = 'pending'
            ORDER BY created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id::TEXT, job_type, payload, status, worker_id, created_at, updated_at;
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, worker_id)
        if row is None:
            return None
        d = dict(row)
        # asyncpg returns JSONB as a dict already; normalize
        if isinstance(d.get("payload"), str):
            import json as _json
            d["payload"] = _json.loads(d["payload"])
        return d

    async def complete_job(self, job_id: str, result: dict[str, Any]) -> None:
        """Mark a running job as done and store the result."""
        import json as _json

        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        result_str = _json.dumps(result)
        query = """
        UPDATE worker_jobs
        SET status = 'done', result = $2::JSONB, updated_at = NOW()
        WHERE id = $1::UUID;
        """
        async with self._pool.acquire() as conn:
            await conn.execute(query, job_id, result_str)

    async def fail_job(self, job_id: str, error: str) -> None:
        """Mark a running job as error."""
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        UPDATE worker_jobs
        SET status = 'error', error = $2, updated_at = NOW()
        WHERE id = $1::UUID;
        """
        async with self._pool.acquire() as conn:
            await conn.execute(query, job_id, error)

    async def get_latest_job_result(
        self, job_type: str, *, max_age_seconds: float = 60
    ) -> dict[str, Any] | None:
        """Return the result JSON of the most recently completed job of the given type,
        or None if no such job exists within max_age_seconds."""
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        SELECT result
        FROM worker_jobs
        WHERE job_type = $1
          AND status = 'done'
          AND updated_at >= NOW() - ($2 || ' seconds')::INTERVAL
        ORDER BY updated_at DESC
        LIMIT 1;
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, job_type, str(int(max_age_seconds)))
        if row is None or row["result"] is None:
            return None
        result = row["result"]
        if isinstance(result, str):
            import json as _json
            result = _json.loads(result)
        return result

    async def get_job_status(self, job_id: str) -> dict[str, Any] | None:
        """Return status/result/error for a specific job id."""
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        SELECT id::TEXT, job_type, status, result, error, worker_id, created_at, updated_at
        FROM worker_jobs
        WHERE id = $1::UUID;
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, job_id)
        return dict(row) if row else None

    async def cleanup_old_jobs(self, *, max_age_hours: int = 24) -> int:
        """Delete done/error jobs older than max_age_hours. Returns count deleted."""
        await self.connect()
        if self._pool is None:
            raise RuntimeError("DB pool is not initialized")
        query = """
        DELETE FROM worker_jobs
        WHERE status IN ('done', 'error')
          AND updated_at < NOW() - ($1 || ' hours')::INTERVAL;
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(query, str(max_age_hours))
        # asyncpg returns 'DELETE N'
        try:
            return int(result.split()[-1])
        except (IndexError, ValueError):
            return 0
