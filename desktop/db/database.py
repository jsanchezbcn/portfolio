"""desktop/db/database.py — asyncpg-based PostgreSQL manager.

Provides connection pooling, typed helpers for CRUD on positions/orders/fills,
and is fully async so it integrates cleanly with the ib_async event loop via qasync.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
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
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_chain_cache (
                    id SERIAL PRIMARY KEY,
                    underlying TEXT NOT NULL,
                    expiry DATE NOT NULL,
                    strike DOUBLE PRECISION NOT NULL,
                    option_right CHAR(1) NOT NULL,
                    conid BIGINT,
                    bid DOUBLE PRECISION,
                    ask DOUBLE PRECISION,
                    last DOUBLE PRECISION,
                    volume INTEGER,
                    open_interest INTEGER,
                    iv DOUBLE PRECISION,
                    delta DOUBLE PRECISION,
                    gamma DOUBLE PRECISION,
                    theta DOUBLE PRECISION,
                    vega DOUBLE PRECISION,
                    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (underlying, expiry, strike, option_right)
                );
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chain_underlying ON option_chain_cache(underlying, expiry);"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS available_expirations (
                    id SERIAL PRIMARY KEY,
                    underlying TEXT NOT NULL,
                    sec_type TEXT NOT NULL DEFAULT 'FOP',
                    exchange TEXT NOT NULL DEFAULT 'CME',
                    expirations TEXT[] NOT NULL,
                    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (underlying, sec_type, exchange)
                );
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_avail_expir_underlying ON available_expirations(underlying, sec_type);"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_groups (
                    association_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    strategy_family TEXT,
                    underlying TEXT NOT NULL,
                    expiry_label TEXT,
                    matched_by TEXT,
                    leg_count INTEGER NOT NULL DEFAULT 0,
                    net_delta DOUBLE PRECISION,
                    net_gamma DOUBLE PRECISION,
                    net_theta DOUBLE PRECISION,
                    net_vega DOUBLE PRECISION,
                    net_spx_delta DOUBLE PRECISION,
                    market_value DOUBLE PRECISION,
                    unrealized_pnl DOUBLE PRECISION,
                    realized_pnl DOUBLE PRECISION,
                    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
                    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_strategy_groups_account ON strategy_groups(account_id, underlying, synced_at DESC);"
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_group_legs (
                    association_id TEXT NOT NULL REFERENCES strategy_groups(association_id) ON DELETE CASCADE,
                    account_id TEXT NOT NULL,
                    leg_index INTEGER NOT NULL,
                    conid BIGINT NOT NULL,
                    symbol TEXT NOT NULL,
                    sec_type TEXT NOT NULL,
                    underlying TEXT,
                    expiry DATE,
                    strike DOUBLE PRECISION,
                    option_right CHAR(1),
                    quantity DOUBLE PRECISION NOT NULL,
                    avg_cost DOUBLE PRECISION,
                    market_price DOUBLE PRECISION,
                    market_value DOUBLE PRECISION,
                    unrealized_pnl DOUBLE PRECISION,
                    realized_pnl DOUBLE PRECISION,
                    delta DOUBLE PRECISION,
                    gamma DOUBLE PRECISION,
                    theta DOUBLE PRECISION,
                    vega DOUBLE PRECISION,
                    iv DOUBLE PRECISION,
                    spx_delta DOUBLE PRECISION,
                    leg_role TEXT,
                    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (association_id, leg_index)
                );
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_strategy_group_legs_account ON strategy_group_legs(account_id, underlying, expiry);"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions_cache (
                    id SERIAL PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    conid BIGINT NOT NULL,
                    symbol TEXT NOT NULL,
                    sec_type TEXT NOT NULL,
                    underlying TEXT,
                    expiry DATE,
                    strike DOUBLE PRECISION,
                    option_right CHAR(1),
                    quantity DOUBLE PRECISION,
                    avg_cost DOUBLE PRECISION,
                    market_price DOUBLE PRECISION,
                    market_value DOUBLE PRECISION,
                    unrealized_pnl DOUBLE PRECISION,
                    realized_pnl DOUBLE PRECISION,
                    underlying_price DOUBLE PRECISION,
                    delta DOUBLE PRECISION,
                    gamma DOUBLE PRECISION,
                    theta DOUBLE PRECISION,
                    vega DOUBLE PRECISION,
                    iv DOUBLE PRECISION,
                    spx_delta DOUBLE PRECISION,
                    cached_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_positions_cache_account_cached ON positions_cache(account_id, cached_at DESC);"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_positions_cache_snapshot ON positions_cache(snapshot_id);"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_positions_cache_expiry ON positions_cache(account_id, expiry, cached_at DESC);"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_greeks_cache (
                    id SERIAL PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    total_delta DOUBLE PRECISION,
                    total_gamma DOUBLE PRECISION,
                    total_theta DOUBLE PRECISION,
                    total_vega DOUBLE PRECISION,
                    total_spx_delta DOUBLE PRECISION,
                    underlying_price DOUBLE PRECISION,
                    cached_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_portfolio_greeks_cache_account_cached ON portfolio_greeks_cache(account_id, cached_at DESC);"
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_metrics_cache (
                    id SERIAL PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    total_positions INTEGER,
                    total_value DOUBLE PRECISION,
                    total_spx_delta DOUBLE PRECISION,
                    total_delta DOUBLE PRECISION,
                    total_gamma DOUBLE PRECISION,
                    total_theta DOUBLE PRECISION,
                    total_vega DOUBLE PRECISION,
                    theta_vega_ratio DOUBLE PRECISION,
                    gross_exposure DOUBLE PRECISION,
                    net_exposure DOUBLE PRECISION,
                    options_count INTEGER,
                    stocks_count INTEGER,
                    nlv DOUBLE PRECISION,
                    buying_power DOUBLE PRECISION,
                    init_margin DOUBLE PRECISION,
                    maint_margin DOUBLE PRECISION,
                    cached_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_portfolio_metrics_cache_account_cached ON portfolio_metrics_cache(account_id, cached_at DESC);"
            )

    @staticmethod
    def _coerce_yyyymmdd_date(value: Any) -> date | None:
        if value is None:
            return None
        if isinstance(value, date):
            return value
        text = str(value).replace("-", "").strip()
        if len(text) != 8 or not text.isdigit():
            return None
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None

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

    async def replace_strategy_groups(self, account_id: str, groups: list[Any]) -> int:
        """Replace the active strategy associations snapshot for an account."""
        now = datetime.now(timezone.utc)
        group_rows: list[tuple[Any, ...]] = []
        leg_rows: list[tuple[Any, ...]] = []

        for group in groups:
            group_rows.append(
                (
                    str(getattr(group, "association_id", "")),
                    account_id,
                    str(getattr(group, "strategy_name", "Single Leg / Naked")),
                    getattr(group, "strategy_family", None),
                    str(getattr(group, "underlying", "")),
                    getattr(group, "expiry_label", None),
                    getattr(group, "matched_by", None),
                    len(list(getattr(group, "legs", []) or [])),
                    getattr(group, "net_delta", None),
                    getattr(group, "net_gamma", None),
                    getattr(group, "net_theta", None),
                    getattr(group, "net_vega", None),
                    getattr(group, "net_spx_delta", None),
                    getattr(group, "net_mkt_value", None),
                    getattr(group, "net_upnl", None),
                    getattr(group, "net_rpnl", None),
                    json.dumps({
                        "leg_ids": [int(getattr(leg, "conid", 0) or 0) for leg in (getattr(group, "legs", []) or [])],
                    }),
                    now,
                )
            )
            for leg_index, leg in enumerate(list(getattr(group, "legs", []) or [])):
                leg_rows.append(
                    (
                        str(getattr(group, "association_id", "")),
                        account_id,
                        leg_index,
                        int(getattr(leg, "conid", 0) or 0),
                        str(getattr(leg, "symbol", "") or ""),
                        str(getattr(leg, "sec_type", "") or ""),
                        getattr(leg, "underlying", None),
                        getattr(leg, "expiry", None),
                        getattr(leg, "strike", None),
                        getattr(leg, "right", None),
                        float(getattr(leg, "quantity", 0.0) or 0.0),
                        getattr(leg, "avg_cost", None),
                        getattr(leg, "market_price", None),
                        getattr(leg, "market_value", None),
                        getattr(leg, "unrealized_pnl", None),
                        getattr(leg, "realized_pnl", None),
                        getattr(leg, "delta", None),
                        getattr(leg, "gamma", None),
                        getattr(leg, "theta", None),
                        getattr(leg, "vega", None),
                        getattr(leg, "iv", None),
                        getattr(leg, "spx_delta", None),
                        None,
                        now,
                    )
                )

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM strategy_group_legs WHERE account_id = $1", account_id)
                await conn.execute("DELETE FROM strategy_groups WHERE account_id = $1", account_id)

                if group_rows:
                    await conn.executemany(
                        """
                        INSERT INTO strategy_groups (
                            association_id, account_id, strategy_name, strategy_family,
                            underlying, expiry_label, matched_by, leg_count,
                            net_delta, net_gamma, net_theta, net_vega, net_spx_delta,
                            market_value, unrealized_pnl, realized_pnl, metadata, synced_at
                        ) VALUES (
                            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb,$18
                        )
                        ON CONFLICT (association_id) DO UPDATE SET
                            account_id = EXCLUDED.account_id,
                            strategy_name = EXCLUDED.strategy_name,
                            strategy_family = EXCLUDED.strategy_family,
                            underlying = EXCLUDED.underlying,
                            expiry_label = EXCLUDED.expiry_label,
                            matched_by = EXCLUDED.matched_by,
                            leg_count = EXCLUDED.leg_count,
                            net_delta = EXCLUDED.net_delta,
                            net_gamma = EXCLUDED.net_gamma,
                            net_theta = EXCLUDED.net_theta,
                            net_vega = EXCLUDED.net_vega,
                            net_spx_delta = EXCLUDED.net_spx_delta,
                            market_value = EXCLUDED.market_value,
                            unrealized_pnl = EXCLUDED.unrealized_pnl,
                            realized_pnl = EXCLUDED.realized_pnl,
                            metadata = EXCLUDED.metadata,
                            synced_at = EXCLUDED.synced_at
                        """,
                        group_rows,
                    )
                if leg_rows:
                    await conn.executemany(
                        """
                        INSERT INTO strategy_group_legs (
                            association_id, account_id, leg_index, conid, symbol, sec_type,
                            underlying, expiry, strike, option_right, quantity,
                            avg_cost, market_price, market_value, unrealized_pnl, realized_pnl,
                            delta, gamma, theta, vega, iv, spx_delta, leg_role, synced_at
                        ) VALUES (
                            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                            $12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24
                        )
                        ON CONFLICT (association_id, leg_index) DO UPDATE SET
                            account_id = EXCLUDED.account_id,
                            conid = EXCLUDED.conid,
                            symbol = EXCLUDED.symbol,
                            sec_type = EXCLUDED.sec_type,
                            underlying = EXCLUDED.underlying,
                            expiry = EXCLUDED.expiry,
                            strike = EXCLUDED.strike,
                            option_right = EXCLUDED.option_right,
                            quantity = EXCLUDED.quantity,
                            avg_cost = EXCLUDED.avg_cost,
                            market_price = EXCLUDED.market_price,
                            market_value = EXCLUDED.market_value,
                            unrealized_pnl = EXCLUDED.unrealized_pnl,
                            realized_pnl = EXCLUDED.realized_pnl,
                            delta = EXCLUDED.delta,
                            gamma = EXCLUDED.gamma,
                            theta = EXCLUDED.theta,
                            vega = EXCLUDED.vega,
                            iv = EXCLUDED.iv,
                            spx_delta = EXCLUDED.spx_delta,
                            leg_role = EXCLUDED.leg_role,
                            synced_at = EXCLUDED.synced_at
                        """,
                        leg_rows,
                    )
        return len(group_rows)

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

    async def get_cached_greeks_by_contract(self, account_id: str) -> dict[tuple[str, str, float, str, str], dict[str, float | None]]:
        """Load last known raw (per-contract, un-scaled) Greeks from option_chain_cache.

        Sources from option_chain_cache (populated by live Greek fetches) rather than
        the positions table (which stores position-level scaled Greeks and is often empty
        for options).  Returns a dict keyed by (symbol, expiry_YYYYMMDD, strike, right,
        sec_type); each chain row is inserted under both 'FOP' and 'OPT' so lookup works
        regardless of whether the caller uses FOP or OPT as the sec_type.
        """
        rows = await self.pool.fetch(
            """
            SELECT underlying, expiry, strike, option_right, delta, gamma, theta, vega, iv
            FROM option_chain_cache
            WHERE (delta IS NOT NULL OR gamma IS NOT NULL OR theta IS NOT NULL
                   OR vega IS NOT NULL OR iv IS NOT NULL)
            ORDER BY fetched_at DESC
            """
        )
        result: dict[tuple[str, str, float, str, str], dict[str, float | None]] = {}
        for row in rows:
            expiry_value = row["expiry"]
            if hasattr(expiry_value, "strftime"):
                expiry_str = expiry_value.strftime("%Y%m%d")
            else:
                expiry_str = str(expiry_value or "").replace("-", "")[:8]

            greek_data: dict[str, float | None] = {
                "delta": row["delta"],
                "gamma": row["gamma"],
                "theta": row["theta"],
                "vega": row["vega"],
                "iv": row["iv"],
            }
            sym = str(row["underlying"] or "").upper()
            strike = round(float(row["strike"] or 0.0), 4)
            right = str(row["option_right"] or "").upper()
            # Insert under both FOP and OPT since chain cache doesn't store sec_type;
            # the correct variant will be matched when the engine looks up by contract signature.
            for sec_type in ("FOP", "OPT"):
                signature = (sym, expiry_str, strike, right, sec_type)
                if signature not in result:
                    result[signature] = greek_data
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
        expiry_date = self._coerce_yyyymmdd_date(expiry)
        if expiry_date is None:
            logger.debug("Skipping cached Greeks write for %s %.0f %s: invalid expiry %r", underlying, strike, option_right, expiry)
            return
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
                underlying, expiry_date, strike, option_right, conid,
                bid, ask, last, volume, open_interest,
                iv, delta, gamma, theta, vega
            )
        except Exception as exc:
            logger.debug("Failed to store cached Greeks for %s %s %.0f %s: %s", 
                        underlying, expiry, strike, option_right, exc)

    async def get_cached_chain(
        self,
        underlying: str,
        expiry: Any,
        *,
        max_age_seconds: float,
    ) -> list[dict[str, Any]]:
        expiry_date = self._coerce_yyyymmdd_date(expiry)
        if expiry_date is None:
            return []

        rows = await self.pool.fetch(
            """
            SELECT underlying, expiry, strike, option_right, conid,
                   bid, ask, last, volume, open_interest,
                   iv, delta, gamma, theta, vega, fetched_at
            FROM option_chain_cache
            WHERE underlying = $1
              AND expiry = $2
              AND fetched_at >= NOW() - ($3 * INTERVAL '1 second')
            ORDER BY strike ASC, option_right ASC
            """,
            underlying,
            expiry_date,
            max(float(max_age_seconds), 1.0),
        )
        return [dict(row) for row in rows]

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

    async def get_cached_account_snapshot(
        self,
        account_id: str,
        max_age_seconds: int = 30,
    ) -> dict[str, Any] | None:
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
        row = await self.pool.fetchrow(
            """
            SELECT account_id, net_liquidation, total_cash, buying_power,
                   init_margin, maint_margin, unrealized_pnl, realized_pnl,
                   timestamp AS cached_at
            FROM account_snapshots
            WHERE account_id = $1
              AND EXTRACT(EPOCH FROM timestamp) >= $2
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            account_id,
            cutoff,
        )
        return dict(row) if row else None

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

    # ── positions cache ───────────────────────────────────────────────────

    async def cache_positions_snapshot(
        self, account_id: str, snapshot_id: str, positions: list[dict[str, Any]]
    ) -> int:
        """Cache positions snapshot for fast retrieval by LLM tools."""
        if not positions:
            return 0

        sql = """
            INSERT INTO positions_cache (
                account_id, snapshot_id, conid, symbol, sec_type, underlying,
                expiry, strike, option_right, quantity, avg_cost, market_price,
                market_value, unrealized_pnl, realized_pnl, underlying_price,
                delta, gamma, theta, vega, iv, spx_delta, cached_at
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23
            )
        """
        now = datetime.now(timezone.utc)
        args = []
        for p in positions:
            args.append((
                account_id,
                snapshot_id,
                int(p.get("conid", 0)),
                str(p.get("symbol", "")),
                str(p.get("sec_type", "STK")),
                p.get("underlying"),
                p.get("expiry"),
                p.get("strike"),
                p.get("option_right"),
                float(p.get("quantity", 0)),
                p.get("avg_cost"),
                p.get("market_price"),
                p.get("market_value"),
                p.get("unrealized_pnl"),
                p.get("realized_pnl"),
                p.get("underlying_price"),
                p.get("delta"),
                p.get("gamma"),
                p.get("theta"),
                p.get("vega"),
                p.get("iv"),
                p.get("spx_delta"),
                now,
            ))

        async with self.pool.acquire() as conn:
            await conn.executemany(sql, args)
        return len(args)

    async def get_cached_positions(
        self, account_id: str, max_age_seconds: int = 60
    ) -> list[dict[str, Any]]:
        """Get latest cached positions if fresh enough."""
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds

        rows = await self.pool.fetch(
            """
            SELECT DISTINCT ON (conid)
                conid, symbol, sec_type, underlying, expiry, strike, option_right,
                quantity, avg_cost, market_price, market_value, unrealized_pnl,
                realized_pnl, underlying_price, delta, gamma, theta, vega, iv,
                spx_delta, cached_at
            FROM positions_cache
            WHERE account_id = $1
              AND EXTRACT(EPOCH FROM cached_at) >= $2
            ORDER BY conid, cached_at DESC
            """,
            account_id,
            cutoff,
        )
        return [dict(row) for row in rows]

    async def get_cached_positions_by_date(
        self, account_id: str, max_age_seconds: int = 60
    ) -> dict[str, list[dict[str, Any]]]:
        """Get cached positions grouped by expiry date."""
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds

        rows = await self.pool.fetch(
            """
            SELECT DISTINCT ON (conid)
                conid, symbol, sec_type, underlying, expiry, strike, option_right,
                quantity, avg_cost, market_price, market_value, unrealized_pnl,
                realized_pnl, underlying_price, delta, gamma, theta, vega, iv,
                spx_delta, cached_at
            FROM positions_cache
            WHERE account_id = $1
              AND EXTRACT(EPOCH FROM cached_at) >= $2
            ORDER BY conid, cached_at DESC
            """,
            account_id,
            cutoff,
        )

        # Group by expiry date
        by_date: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            row_dict = dict(row)
            expiry = row_dict.get("expiry")
            if expiry:
                date_key = str(expiry) if isinstance(expiry, str) else expiry.strftime("%Y-%m-%d")
            else:
                date_key = "No Expiry"

            if date_key not in by_date:
                by_date[date_key] = []
            by_date[date_key].append(row_dict)

        return by_date

    async def cache_portfolio_greeks(
        self,
        account_id: str,
        total_delta: float | None,
        total_gamma: float | None,
        total_theta: float | None,
        total_vega: float | None,
        total_spx_delta: float | None,
        underlying_price: float | None = None,
    ) -> None:
        """Cache aggregated portfolio Greeks."""
        await self.pool.execute(
            """
            INSERT INTO portfolio_greeks_cache (
                account_id, total_delta, total_gamma, total_theta,
                total_vega, total_spx_delta, underlying_price
            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            account_id,
            total_delta,
            total_gamma,
            total_theta,
            total_vega,
            total_spx_delta,
            underlying_price,
        )

    async def cache_portfolio_metrics(
        self,
        account_id: str,
        metrics: dict[str, Any],
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO portfolio_metrics_cache (
                account_id, total_positions, total_value, total_spx_delta,
                total_delta, total_gamma, total_theta, total_vega,
                theta_vega_ratio, gross_exposure, net_exposure,
                options_count, stocks_count, nlv, buying_power,
                init_margin, maint_margin
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8,
                $9, $10, $11,
                $12, $13, $14, $15,
                $16, $17
            )
            """,
            account_id,
            metrics.get("total_positions"),
            metrics.get("total_value"),
            metrics.get("total_spx_delta"),
            metrics.get("total_delta"),
            metrics.get("total_gamma"),
            metrics.get("total_theta"),
            metrics.get("total_vega"),
            metrics.get("theta_vega_ratio"),
            metrics.get("gross_exposure"),
            metrics.get("net_exposure"),
            metrics.get("options_count"),
            metrics.get("stocks_count"),
            metrics.get("nlv"),
            metrics.get("buying_power"),
            metrics.get("init_margin"),
            metrics.get("maint_margin"),
        )

    async def get_cached_portfolio_greeks(
        self, account_id: str, max_age_seconds: int = 60
    ) -> dict[str, Any] | None:
        """Get latest cached portfolio Greeks if fresh enough."""
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds

        row = await self.pool.fetchrow(
            """
            SELECT
                total_delta, total_gamma, total_theta, total_vega,
                total_spx_delta, underlying_price, cached_at
            FROM portfolio_greeks_cache
            WHERE account_id = $1
              AND EXTRACT(EPOCH FROM cached_at) >= $2
            ORDER BY cached_at DESC
            LIMIT 1
            """,
            account_id,
            cutoff,
        )
        return dict(row) if row else None

    async def get_cached_portfolio_metrics(
        self,
        account_id: str,
        max_age_seconds: int = 60,
    ) -> dict[str, Any] | None:
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
        row = await self.pool.fetchrow(
            """
            SELECT
                total_positions, total_value, total_spx_delta, total_delta,
                total_gamma, total_theta, total_vega, theta_vega_ratio,
                gross_exposure, net_exposure, options_count, stocks_count,
                nlv, buying_power, init_margin, maint_margin, cached_at
            FROM portfolio_metrics_cache
            WHERE account_id = $1
              AND EXTRACT(EPOCH FROM cached_at) >= $2
            ORDER BY cached_at DESC
            LIMIT 1
            """,
            account_id,
            cutoff,
        )
        return dict(row) if row else None

    async def get_portfolio_greeks_timeseries(
        self,
        account_id: str,
        lookback_minutes: int = 24 * 60,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """Return minute-resolution portfolio greek history for charting/analytics."""
        lookback_minutes = max(1, int(lookback_minutes))
        limit = max(1, int(limit))
        rows = await self.pool.fetch(
            """
            SELECT
                account_id,
                total_delta,
                total_gamma,
                total_theta,
                total_vega,
                total_spx_delta,
                underlying_price,
                cached_at
            FROM portfolio_greeks_cache
            WHERE account_id = $1
              AND cached_at >= NOW() - ($2 * INTERVAL '1 minute')
            ORDER BY cached_at DESC
            LIMIT $3
            """,
            account_id,
            lookback_minutes,
            limit,
        )
        return [dict(r) for r in rows]

    async def get_portfolio_metrics_timeseries(
        self,
        account_id: str,
        lookback_minutes: int = 24 * 60,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """Return minute-resolution portfolio metrics history for charting/analytics."""
        lookback_minutes = max(1, int(lookback_minutes))
        limit = max(1, int(limit))
        rows = await self.pool.fetch(
            """
            SELECT
                account_id,
                total_positions,
                total_value,
                total_spx_delta,
                total_delta,
                total_gamma,
                total_theta,
                total_vega,
                theta_vega_ratio,
                gross_exposure,
                net_exposure,
                options_count,
                stocks_count,
                nlv,
                buying_power,
                init_margin,
                maint_margin,
                cached_at
            FROM portfolio_metrics_cache
            WHERE account_id = $1
              AND cached_at >= NOW() - ($2 * INTERVAL '1 minute')
            ORDER BY cached_at DESC
            LIMIT $3
            """,
            account_id,
            lookback_minutes,
            limit,
        )
        return [dict(r) for r in rows]
