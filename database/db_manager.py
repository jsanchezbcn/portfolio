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

        async with self._pool.acquire() as conn:
            await conn.execute(create_trades)
            await conn.execute(create_snapshots_parent)
            await conn.execute(create_partition)
            await conn.execute(create_indexes)

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
