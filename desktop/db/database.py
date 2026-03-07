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

    async def clear_positions(self, account_id: str) -> None:
        await self.pool.execute("DELETE FROM positions WHERE account_id = $1", account_id)

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
