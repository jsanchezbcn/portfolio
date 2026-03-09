"""desktop/db/database.py — asyncpg-based PostgreSQL manager.

Provides connection pooling, typed helpers for CRUD on positions/orders/fills,
and is fully async so it integrates cleanly with the ib_async event loop via qasync.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


class Database:
    """Thin asyncpg wrapper with business-specific helpers."""

    def __init__(self, dsn: str = "postgresql://portfoliouser:portfoliopass@localhost:5432/portfoliodb"):
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Create the connection pool."""
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
            timeout=10,  # connection timeout per-connection
        )
        await self.ensure_schema()
        logger.info("Database pool created (%s)", self._dsn.split("@")[-1])

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("Database pool closed")

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database not connected — call await db.connect() first")
        return self._pool

    async def ensure_schema(self) -> None:
        """Create shared business-data tables required by the desktop runtime."""
        async with self.pool.acquire() as conn:
            await conn.execute('CREATE EXTENSION IF NOT EXISTS pgcrypto;')
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_intel (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    trade_id UUID,
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    sentiment_score DOUBLE PRECISION,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_market_intel_symbol_source ON market_intel(symbol, source, created_at DESC);"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS journal_notes (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    account_id TEXT,
                    order_id UUID REFERENCES orders(id) ON DELETE SET NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    tags TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_journal_notes_account_created ON journal_notes(account_id, created_at DESC);"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_journal_notes_created ON journal_notes(created_at DESC);"
            )

    # ── positions ─────────────────────────────────────────────────────────

    async def upsert_positions(self, account_id: str, rows: list[dict[str, Any]]) -> int:
        """Bulk upsert positions from IBKR.  Returns number of rows affected."""
        if not rows:
            return 0

        sql = """
            INSERT INTO positions (
                account_id, conid, symbol, sec_type, exchange, currency,
                underlying, strike, option_right, expiry, multiplier,
                quantity, avg_cost, market_price, market_value,
                unrealized_pnl, realized_pnl,
                delta, gamma, theta, vega, iv,
                spx_delta, beta, synced_at
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                $12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25
            )
            ON CONFLICT (account_id, conid) DO UPDATE SET
                symbol         = EXCLUDED.symbol,
                quantity       = EXCLUDED.quantity,
                avg_cost       = EXCLUDED.avg_cost,
                market_price   = EXCLUDED.market_price,
                market_value   = EXCLUDED.market_value,
                unrealized_pnl = EXCLUDED.unrealized_pnl,
                realized_pnl   = EXCLUDED.realized_pnl,
                delta          = EXCLUDED.delta,
                gamma          = EXCLUDED.gamma,
                theta          = EXCLUDED.theta,
                vega           = EXCLUDED.vega,
                iv             = EXCLUDED.iv,
                spx_delta      = EXCLUDED.spx_delta,
                beta           = EXCLUDED.beta,
                synced_at      = EXCLUDED.synced_at
        """
        now = datetime.now(timezone.utc)
        args = []
        for r in rows:
            args.append((
                account_id,
                int(r.get("conid", 0)),
                str(r.get("symbol", "")),
                str(r.get("sec_type", "STK")),
                r.get("exchange"),
                str(r.get("currency", "USD")),
                r.get("underlying"),
                r.get("strike"),
                r.get("option_right"),
                r.get("expiry"),
                float(r.get("multiplier", 1.0) or 1.0),
                float(r.get("quantity", 0)),
                r.get("avg_cost"),
                r.get("market_price"),
                r.get("market_value"),
                r.get("unrealized_pnl"),
                r.get("realized_pnl"),
                r.get("delta"),
                r.get("gamma"),
                r.get("theta"),
                r.get("vega"),
                r.get("iv"),
                r.get("spx_delta"),
                r.get("beta"),
                now,
            ))
        async with self.pool.acquire() as conn:
            await conn.executemany(sql, args)
        return len(args)

    async def get_positions(self, account_id: str) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM positions WHERE account_id = $1 ORDER BY symbol",
            account_id,
        )

    async def get_cached_greeks(self, account_id: str) -> dict[int, dict]:
        """Load last known Greeks for all positions from database.
        
        Returns dict mapping conid -> {delta, gamma, theta, vega, iv}.
        Used to populate _greeks_cache on engine startup for after-hours display.
        """
        rows = await self.pool.fetch(
            """
            SELECT conid, delta, gamma, theta, vega, iv 
            FROM positions 
            WHERE account_id = $1 AND (delta IS NOT NULL OR gamma IS NOT NULL OR theta IS NOT NULL OR vega IS NOT NULL)
            ORDER BY synced_at DESC
            """,
            account_id,
        )
        result: dict[int, dict] = {}
        for row in rows:
            if row['conid'] not in result:  # Keep first (most recent) entry per conid
                result[row['conid']] = {
                    'delta': row['delta'],
                    'gamma': row['gamma'],
                    'theta': row['theta'],
                    'vega': row['vega'],
                    'iv': row['iv'],
                }
        return result

    async def store_cached_greeks(
        self,
        underlying: str,
        expiry,
        strike: float,
        option_right: str,
        conid: int | None = None,
        bid: float | None = None,
        ask: float | None = None,
        last: float | None = None,
        volume: int | None = None,
        open_interest: int | None = None,
        iv: float | None = None,
        delta: float | None = None,
        gamma: float | None = None,
        theta: float | None = None,
        vega: float | None = None,
    ) -> None:
        """Store Greeks and chain data in the option_chain_cache table for offline fallback.
        
        Uses UPSERT to replace existing cache entries for the same option.
        """
        try:
            await self.pool.execute(
                """
                INSERT INTO option_chain_cache (
                    underlying, expiry, strike, option_right, conid,
                    bid, ask, last, volume, open_interest,
                    iv, delta, gamma, theta, vega, fetched_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, NOW())
                ON CONFLICT (underlying, expiry, strike, option_right)
                DO UPDATE SET
                    conid = EXCLUDED.conid,
                    bid = EXCLUDED.bid,
                    ask = EXCLUDED.ask,
                    last = EXCLUDED.last,
                    volume = EXCLUDED.volume,
                    open_interest = EXCLUDED.open_interest,
                    iv = EXCLUDED.iv,
                    delta = EXCLUDED.delta,
                    gamma = EXCLUDED.gamma,
                    theta = EXCLUDED.theta,
                    vega = EXCLUDED.vega,
                    fetched_at = NOW();
                """,
                underlying, expiry, strike, option_right, conid,
                bid, ask, last, volume, open_interest,
                iv, delta, gamma, theta, vega
            )
        except Exception as exc:
            logger.debug("Failed to store cached Greeks for %s %s %.0f %s: %s", 
                        underlying, expiry, strike, option_right, exc)

    async def clear_positions(self, account_id: str) -> None:
        await self.pool.execute("DELETE FROM positions WHERE account_id = $1", account_id)

    async def get_cached_expirations(
        self, 
        underlying: str, 
        sec_type: str = "FOP",
        exchange: str = "CME"
    ) -> list[str] | None:
        """Load cached available expirations for an underlying.
        
        Returns list of YYYYMMDD strings if cache exists, None otherwise.
        Used for offline fallback when market is closed or gateway is unavailable.
        """
        row = await self.pool.fetchrow(
            """
            SELECT expirations, fetched_at 
            FROM available_expirations 
            WHERE underlying = $1 AND sec_type = $2 AND exchange = $3
            """,
            underlying, sec_type, exchange,
        )
        if row:
            age_seconds = (datetime.now(timezone.utc) - row['fetched_at']).total_seconds()
            age_hours = age_seconds / 3600
            logger.debug(
                "Expiry cache hit (database) for %s/%s/%s - %d expirations, age: %.1fh",
                underlying, sec_type, exchange, len(row['expirations']), age_hours
            )
            return list(row['expirations'])
        return None

    async def store_cached_expirations(
        self,
        underlying: str,
        expirations: list[str],
        sec_type: str = "FOP",
        exchange: str = "CME"
    ) -> None:
        """Store available expirations in database for offline fallback.
        
        Uses UPSERT to replace existing cache entries for the same underlying/sec_type/exchange.
        """
        try:
            await self.pool.execute(
                """
                INSERT INTO available_expirations (underlying, sec_type, exchange, expirations, fetched_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (underlying, sec_type, exchange)
                DO UPDATE SET
                    expirations = EXCLUDED.expirations,
                    fetched_at = NOW();
                """,
                underlying, sec_type, exchange, expirations
            )
            logger.debug("Stored %d expirations for %s/%s/%s in database cache", 
                        len(expirations), underlying, sec_type, exchange)
        except Exception as exc:
            logger.debug("Failed to store cached expirations for %s/%s/%s: %s", 
                        underlying, sec_type, exchange, exc)

    # ── orders ────────────────────────────────────────────────────────────

    async def insert_order(self, order: dict[str, Any]) -> UUID:
        """Insert a new order, return its UUID."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO orders (
                account_id, status, order_type, side, limit_price,
                legs_json, source, rationale,
                pre_spx_delta, pre_vega
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            RETURNING id
            """,
            order.get("account_id", ""),
            order.get("status", "DRAFT"),
            order.get("order_type", "LIMIT"),
            order.get("side"),
            order.get("limit_price"),
            json.dumps(order.get("legs", [])),
            order.get("source"),
            order.get("rationale"),
            order.get("pre_spx_delta"),
            order.get("pre_vega"),
        )
        return row["id"]

    async def update_order_status(
        self,
        order_id: UUID,
        status: str,
        *,
        broker_order_id: Optional[str] = None,
        filled_price: Optional[float] = None,
        post_spx_delta: Optional[float] = None,
        post_vega: Optional[float] = None,
        margin_impact: Optional[float] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        ts_field = {
            "PENDING": "submitted_at",
            "SUBMITTED": "submitted_at",
            "FILLED": "filled_at",
            "CANCELLED": "cancelled_at",
            "CANCELED": "cancelled_at",
        }.get(status.upper())

        sets = ["status = $2", "updated_at = $3"]
        args: list[Any] = [order_id, status, now]
        idx = 4

        if broker_order_id is not None:
            sets.append(f"broker_order_id = ${idx}")
            args.append(broker_order_id)
            idx += 1
        if filled_price is not None:
            sets.append(f"filled_price = ${idx}")
            args.append(filled_price)
            idx += 1
        if post_spx_delta is not None:
            sets.append(f"post_spx_delta = ${idx}")
            args.append(post_spx_delta)
            idx += 1
        if post_vega is not None:
            sets.append(f"post_vega = ${idx}")
            args.append(post_vega)
            idx += 1
        if margin_impact is not None:
            sets.append(f"margin_impact = ${idx}")
            args.append(margin_impact)
            idx += 1
        if ts_field:
            sets.append(f"{ts_field} = ${idx}")
            args.append(now)
            idx += 1

        sql = f"UPDATE orders SET {', '.join(sets)} WHERE id = $1"
        await self.pool.execute(sql, *args)

    async def get_orders(
        self,
        account_id: str,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[asyncpg.Record]:
        if status:
            return await self.pool.fetch(
                "SELECT * FROM orders WHERE account_id = $1 AND status = $2 "
                "ORDER BY created_at DESC LIMIT $3",
                account_id, status, limit,
            )
        return await self.pool.fetch(
            "SELECT * FROM orders WHERE account_id = $1 ORDER BY created_at DESC LIMIT $2",
            account_id, limit,
        )

    async def create_journal_note(
        self,
        *,
        account_id: str | None,
        title: str,
        body: str,
        tags: list[str] | None = None,
        order_id: UUID | str | None = None,
    ) -> UUID:
        """Create a journal note and return its UUID."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO journal_notes (account_id, order_id, title, body, tags)
            VALUES ($1, $2::UUID, $3, $4, $5::TEXT[])
            RETURNING id
            """,
            account_id,
            str(order_id) if order_id else None,
            title,
            body,
            list(tags or []),
        )
        return row["id"]

    async def list_journal_notes(
        self,
        *,
        account_id: str | None = None,
        search: str | None = None,
        tag: str | None = None,
        limit: int = 200,
    ) -> list[asyncpg.Record]:
        """Return recent journal notes newest-first."""
        clauses: list[str] = []
        args: list[Any] = []

        if account_id:
            args.append(account_id)
            clauses.append(f"(account_id = ${len(args)} OR account_id IS NULL)")
        if search:
            args.append(f"%{search}%")
            clauses.append(
                f"(title ILIKE ${len(args)} OR body ILIKE ${len(args)})"
            )
        if tag:
            args.append(tag)
            clauses.append(f"${len(args)} = ANY(tags)")

        args.append(limit)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return await self.pool.fetch(
            f"""
            SELECT id, account_id, order_id, title, body, tags, created_at, updated_at
            FROM journal_notes
            {where}
            ORDER BY created_at DESC
            LIMIT ${len(args)}
            """,
            *args,
        )

    async def upsert_market_intel(
        self,
        *,
        symbol: str,
        source: str,
        sentiment_score: float | None = None,
        summary: str = "",
        raw_data: dict[str, Any] | None = None,
    ) -> str:
        """Insert or replace the latest market intelligence row for a symbol/source."""
        content = summary
        if raw_data:
            try:
                content = json.dumps(raw_data)
            except (TypeError, ValueError):
                content = summary

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM market_intel WHERE symbol = $1 AND source = $2",
                    symbol,
                    source,
                )
                row = await conn.fetchrow(
                    """
                    INSERT INTO market_intel (symbol, source, content, sentiment_score)
                    VALUES ($1, $2, $3, $4)
                    RETURNING id::TEXT
                    """,
                    symbol,
                    source,
                    content,
                    sentiment_score,
                )
        return row["id"]

    async def get_recent_market_intel(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id::TEXT, trade_id::TEXT, symbol, source, content, sentiment_score, created_at
            FROM market_intel
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
        return [dict(row) for row in rows]

    async def get_market_intel_by_source(
        self,
        source: str,
        *,
        symbol: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if symbol:
            rows = await self.pool.fetch(
                """
                SELECT id::TEXT, trade_id::TEXT, symbol, source, content, sentiment_score, created_at
                FROM market_intel
                WHERE source = $1 AND symbol = $2
                ORDER BY created_at DESC
                LIMIT $3
                """,
                source,
                symbol,
                limit,
            )
        else:
            rows = await self.pool.fetch(
                """
                SELECT id::TEXT, trade_id::TEXT, symbol, source, content, sentiment_score, created_at
                FROM market_intel
                WHERE source = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                source,
                limit,
            )
        return [dict(row) for row in rows]

    # ── fills ─────────────────────────────────────────────────────────────

    async def insert_fill(self, fill: dict[str, Any]) -> int:
        row = await self.pool.fetchrow(
            """
            INSERT INTO fills (
                order_id, account_id, conid, symbol, action,
                quantity, fill_price, commission, realized_pnl, execution_id
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            RETURNING id
            """,
            fill.get("order_id"),
            fill.get("account_id", ""),
            fill.get("conid"),
            fill.get("symbol", ""),
            fill.get("action", "BUY"),
            float(fill.get("quantity", 0)),
            float(fill.get("fill_price", 0)),
            float(fill.get("commission", 0)),
            fill.get("realized_pnl"),
            fill.get("execution_id"),
        )
        return row["id"]

    async def get_fills(self, account_id: str, limit: int = 200) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM fills WHERE account_id = $1 ORDER BY filled_at DESC LIMIT $2",
            account_id, limit,
        )

    # ── account snapshots ─────────────────────────────────────────────────

    async def insert_account_snapshot(self, snap: dict[str, Any]) -> int:
        row = await self.pool.fetchrow(
            """
            INSERT INTO account_snapshots (
                account_id, net_liquidation, total_cash, buying_power,
                init_margin, maint_margin, unrealized_pnl, realized_pnl
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            RETURNING id
            """,
            snap.get("account_id", ""),
            snap.get("net_liquidation"),
            snap.get("total_cash"),
            snap.get("buying_power"),
            snap.get("init_margin"),
            snap.get("maint_margin"),
            snap.get("unrealized_pnl"),
            snap.get("realized_pnl"),
        )
        return row["id"]

    async def get_latest_snapshot(self, account_id: str) -> Optional[asyncpg.Record]:
        return await self.pool.fetchrow(
            "SELECT * FROM account_snapshots WHERE account_id = $1 ORDER BY timestamp DESC LIMIT 1",
            account_id,
        )

    # ── risk snapshots ────────────────────────────────────────────────────

    async def insert_risk_snapshot(self, snap: dict[str, Any]) -> int:
        row = await self.pool.fetchrow(
            """
            INSERT INTO risk_snapshots (
                account_id, spx_delta, gamma, theta, vega,
                vix, regime, nlv, margin_used_pct
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            RETURNING id
            """,
            snap.get("account_id", ""),
            snap.get("spx_delta"),
            snap.get("gamma"),
            snap.get("theta"),
            snap.get("vega"),
            snap.get("vix"),
            snap.get("regime"),
            snap.get("nlv"),
            snap.get("margin_used_pct"),
        )
        return row["id"]

    async def capture_portfolio_snapshot(self, snap: dict[str, Any]) -> None:
        """Persist a dashboard-style portfolio snapshot across account and risk tables."""
        await self.insert_account_snapshot(
            {
                "account_id": snap.get("account_id", ""),
                "net_liquidation": snap.get("net_liquidation"),
                "total_cash": snap.get("cash_balance"),
                "buying_power": snap.get("buying_power"),
                "init_margin": snap.get("init_margin"),
                "maint_margin": snap.get("maint_margin"),
                "unrealized_pnl": snap.get("unrealized_pnl"),
                "realized_pnl": snap.get("realized_pnl"),
            }
        )
        await self.insert_risk_snapshot(
            {
                "account_id": snap.get("account_id", ""),
                "spx_delta": snap.get("spx_delta"),
                "gamma": snap.get("gamma"),
                "theta": snap.get("theta"),
                "vega": snap.get("vega"),
                "vix": snap.get("vix"),
                "regime": snap.get("regime"),
                "nlv": snap.get("net_liquidation"),
                "margin_used_pct": snap.get("margin_used_pct"),
            }
        )

    async def query_snapshots(
        self,
        *,
        start_dt: Optional[str] = None,
        end_dt: Optional[str] = None,
        account_id: Optional[str] = None,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        """Return chart-friendly portfolio snapshots oldest-first.

        Combines `risk_snapshots` with the latest prior `account_snapshots` row
        for each risk snapshot so dashboard history charts can use a single API.
        """
        clauses: list[str] = []
        args: list[Any] = []

        if start_dt:
            args.append(datetime.fromisoformat(start_dt.replace("Z", "+00:00")))
            clauses.append(f"rs.timestamp >= ${len(args)}")
        if end_dt:
            args.append(datetime.fromisoformat(end_dt.replace("Z", "+00:00")))
            clauses.append(f"rs.timestamp <= ${len(args)}")
        if account_id:
            args.append(account_id)
            clauses.append(f"rs.account_id = ${len(args)}")

        args.append(limit)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        rows = await self.pool.fetch(
            f"""
            SELECT
                rs.timestamp AS captured_at,
                rs.account_id,
                acct.net_liquidation,
                acct.total_cash AS cash_balance,
                rs.spx_delta,
                rs.gamma,
                rs.theta,
                rs.vega,
                CASE
                    WHEN rs.spx_delta IS NOT NULL AND rs.spx_delta <> 0 AND rs.theta IS NOT NULL
                    THEN rs.theta / rs.spx_delta
                    ELSE NULL
                END AS delta_theta_ratio,
                rs.vix,
                NULL::DOUBLE PRECISION AS spx_price,
                rs.regime
            FROM risk_snapshots rs
            LEFT JOIN LATERAL (
                SELECT a.net_liquidation, a.total_cash
                FROM account_snapshots a
                WHERE a.account_id = rs.account_id AND a.timestamp <= rs.timestamp
                ORDER BY a.timestamp DESC
                LIMIT 1
            ) acct ON TRUE
            {where}
            ORDER BY rs.timestamp ASC
            LIMIT ${len(args)}
            """,
            *args,
        )
        return [dict(row) for row in rows]
