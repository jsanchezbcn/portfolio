"""
bridge/database_manager.py
───────────────────────────
Schema bootstrap and thin write helpers for the IBKR trading bridge.

Tables managed here (separate from the main db_manager.py tables):
  - portfolio_greeks  — one row per poll cycle (5 s default)
  - api_logs          — lifecycle events (connect, disconnect, error, watchdog)

All writes go through a DBCircuitBreaker instance so the daemon stays alive
even when Postgres is temporarily unavailable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import asyncpg

from database.circuit_breaker import DBCircuitBreaker

logger = logging.getLogger(__name__)

# ─── DDL ────────────────────────────────────────────────────────────────────

_DDL_PORTFOLIO_GREEKS = """
CREATE TABLE IF NOT EXISTS portfolio_greeks (
    id               BIGSERIAL       PRIMARY KEY,
    timestamp        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    contract         TEXT            NOT NULL DEFAULT 'PORTFOLIO',
    delta            DOUBLE PRECISION,
    gamma            DOUBLE PRECISION,
    vega             DOUBLE PRECISION,
    theta            DOUBLE PRECISION,
    underlying_price DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_portfolio_greeks_ts
    ON portfolio_greeks (timestamp DESC);
"""

_DDL_API_LOGS = """
CREATE TABLE IF NOT EXISTS api_logs (
    id        BIGSERIAL   PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    api_mode  TEXT        NOT NULL,
    message   TEXT        NOT NULL,
    status    TEXT        NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_logs_ts
    ON api_logs (timestamp DESC);
"""


# ─── Schema bootstrap ────────────────────────────────────────────────────────

async def ensure_bridge_schema(pool: asyncpg.Pool) -> None:
    """Create bridge tables + indexes idempotently.

    Safe to call on every startup; uses IF NOT EXISTS throughout.
    """
    async with pool.acquire() as conn:
        await conn.execute(_DDL_PORTFOLIO_GREEKS)
        await conn.execute(_DDL_API_LOGS)
    logger.info("bridge schema ensured (portfolio_greeks, api_logs)")


# ─── Write helpers ────────────────────────────────────────────────────────────

async def write_portfolio_snapshot(
    breaker: DBCircuitBreaker,
    row: dict,
) -> None:
    """Persist one portfolio Greeks snapshot via the circuit breaker.

    Expected keys in *row*:
        timestamp        – datetime (UTC)  [optional; defaults to now]
        contract         – str             [optional; defaults to 'PORTFOLIO']
        delta            – float | None
        gamma            – float | None
        vega             – float | None
        theta            – float | None
        underlying_price – float | None
    """
    payload: dict = {
        "timestamp":        row.get("timestamp", datetime.now(timezone.utc)),
        "contract":         row.get("contract", "PORTFOLIO"),
        "delta":            row.get("delta"),
        "gamma":            row.get("gamma"),
        "vega":             row.get("vega"),
        "theta":            row.get("theta"),
        "underlying_price": row.get("underlying_price"),
    }
    await breaker.write("portfolio_greeks", payload)


async def log_api_event(
    breaker: DBCircuitBreaker,
    api_mode: str,
    message: str,
    status: str,
) -> None:
    """Log one API lifecycle event via the circuit breaker.

    Args:
        breaker:  Active DBCircuitBreaker instance.
        api_mode: 'SOCKET' or 'PORTAL'.
        message:  Human-readable description of the event.
        status:   'info' | 'warning' | 'error'.
    """
    payload: dict = {
        "timestamp": datetime.now(timezone.utc),
        "api_mode":  api_mode,
        "message":   message,
        "status":    status,
    }
    await breaker.write("api_logs", payload)
