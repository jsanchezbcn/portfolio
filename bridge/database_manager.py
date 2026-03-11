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

_DDL_ACCOUNT_STATUS_CACHE = """
CREATE TABLE IF NOT EXISTS account_status_cache (
    id               BIGSERIAL       PRIMARY KEY,
    timestamp        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    account_id       TEXT            NOT NULL,
    net_liquidation  DOUBLE PRECISION,
    buying_power     DOUBLE PRECISION,
    excess_liquidity DOUBLE PRECISION,
    maint_margin     DOUBLE PRECISION,
    unrealized_pnl   DOUBLE PRECISION,
    realized_pnl     DOUBLE PRECISION,
    raw_payload      JSONB           NOT NULL DEFAULT '{}'::JSONB
);
CREATE INDEX IF NOT EXISTS idx_account_status_cache_acct_ts
    ON account_status_cache (account_id, timestamp DESC);
"""

_DDL_ACTIVE_POSITIONS_CACHE = """
CREATE TABLE IF NOT EXISTS active_positions_cache (
    id               BIGSERIAL       PRIMARY KEY,
    timestamp        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    snapshot_id      TEXT            NOT NULL,
    account_id       TEXT            NOT NULL,
    symbol           TEXT            NOT NULL,
    broker           TEXT,
    instrument_type  TEXT,
    quantity         DOUBLE PRECISION,
    avg_price        DOUBLE PRECISION,
    market_value     DOUBLE PRECISION,
    unrealized_pnl   DOUBLE PRECISION,
    delta            DOUBLE PRECISION,
    gamma            DOUBLE PRECISION,
    theta            DOUBLE PRECISION,
    vega             DOUBLE PRECISION,
    iv               DOUBLE PRECISION,
    dte              INTEGER,
    underlying       TEXT,
    raw_payload      JSONB           NOT NULL DEFAULT '{}'::JSONB
);
CREATE INDEX IF NOT EXISTS idx_active_positions_cache_acct_ts
    ON active_positions_cache (account_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_active_positions_cache_snapshot
    ON active_positions_cache (snapshot_id);
"""

_DDL_TRADE_EXECUTIONS = """
CREATE TABLE IF NOT EXISTS trade_executions (
    id                  BIGSERIAL       PRIMARY KEY,
    timestamp           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    broker              TEXT            NOT NULL,
    account_id          TEXT,
    broker_execution_id TEXT,
    symbol              TEXT,
    side                TEXT,
    quantity            DOUBLE PRECISION,
    price               DOUBLE PRECISION,
    commission          DOUBLE PRECISION,
    execution_time      TIMESTAMPTZ,
    raw_payload         JSONB           NOT NULL DEFAULT '{}'::JSONB
);
CREATE INDEX IF NOT EXISTS idx_trade_executions_time
    ON trade_executions (execution_time DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_trade_executions_broker_exec
    ON trade_executions (broker, broker_execution_id);
"""

_DDL_PROPOSED_TRADES = """
CREATE TABLE IF NOT EXISTS proposed_trades (
    id                  BIGSERIAL           PRIMARY KEY,
    account_id          TEXT                NOT NULL,
    strategy_name       TEXT                NOT NULL,
    legs_json           JSONB               NOT NULL DEFAULT '[]',
    net_premium         DOUBLE PRECISION    NOT NULL DEFAULT 0.0,
    init_margin_impact  DOUBLE PRECISION    NOT NULL DEFAULT 0.0,
    maint_margin_impact DOUBLE PRECISION    NOT NULL DEFAULT 0.0,
    margin_impact       DOUBLE PRECISION    NOT NULL DEFAULT 0.0,
    efficiency_score    DOUBLE PRECISION    NOT NULL DEFAULT 0.0,
    delta_reduction     DOUBLE PRECISION    NOT NULL DEFAULT 0.0,
    vega_reduction      DOUBLE PRECISION    NOT NULL DEFAULT 0.0,
    status              TEXT                NOT NULL DEFAULT 'Pending',
    justification       TEXT                NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ         NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_proposed_trades_account_status
    ON proposed_trades (account_id, status);
CREATE INDEX IF NOT EXISTS idx_proposed_trades_created_at
    ON proposed_trades (created_at DESC);
"""


# ─── Schema bootstrap ────────────────────────────────────────────────────────

async def ensure_bridge_schema(pool: asyncpg.Pool) -> None:
    """Create bridge tables + indexes idempotently.

    Safe to call on every startup; uses IF NOT EXISTS throughout.
    """
    async with pool.acquire() as conn:
        await conn.execute(_DDL_PORTFOLIO_GREEKS)
        await conn.execute(_DDL_API_LOGS)
        await conn.execute(_DDL_ACCOUNT_STATUS_CACHE)
        await conn.execute(_DDL_ACTIVE_POSITIONS_CACHE)
        await conn.execute(_DDL_TRADE_EXECUTIONS)
    logger.info(
        "bridge schema ensured (portfolio_greeks, api_logs, account_status_cache, active_positions_cache, trade_executions)"
    )


async def ensure_proposed_trades_schema(pool: asyncpg.Pool) -> None:
    """Create the proposed_trades table + indexes idempotently.

    Called by agents/trade_proposer.py on startup.
    Safe to run multiple times; uses IF NOT EXISTS throughout.
    """
    async with pool.acquire() as conn:
        await conn.execute(_DDL_PROPOSED_TRADES)
    logger.info("bridge schema ensured (proposed_trades)")


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


def _to_float(value: object) -> float | None:
    if isinstance(value, dict):
        value = value.get("amount")
    if value in (None, "", "N/A"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


async def write_account_status_snapshot(
    breaker: DBCircuitBreaker,
    account_id: str,
    summary: dict,
) -> None:
    payload: dict = {
        "timestamp": datetime.now(timezone.utc),
        "account_id": account_id,
        "net_liquidation": _to_float(summary.get("netliquidation")),
        "buying_power": _to_float(summary.get("buyingpower")),
        "excess_liquidity": _to_float(summary.get("excessliquidity")),
        "maint_margin": _to_float(summary.get("maintmarginreq")),
        "unrealized_pnl": _to_float(summary.get("unrealizedpnl")),
        "realized_pnl": _to_float(summary.get("realizedpnl")),
        "raw_payload": summary or {},
    }
    await breaker.write("account_status_cache", payload)


async def write_active_position_snapshot(
    breaker: DBCircuitBreaker,
    *,
    account_id: str,
    snapshot_id: str,
    position_payload: dict,
) -> None:
    payload: dict = {
        "timestamp": datetime.now(timezone.utc),
        "snapshot_id": snapshot_id,
        "account_id": account_id,
        "symbol": str(position_payload.get("symbol") or ""),
        "broker": position_payload.get("broker"),
        "instrument_type": str(position_payload.get("instrument_type") or ""),
        "quantity": _to_float(position_payload.get("quantity")),
        "avg_price": _to_float(position_payload.get("avg_price")),
        "market_value": _to_float(position_payload.get("market_value")),
        "unrealized_pnl": _to_float(position_payload.get("unrealized_pnl")),
        "delta": _to_float(position_payload.get("delta")),
        "gamma": _to_float(position_payload.get("gamma")),
        "theta": _to_float(position_payload.get("theta")),
        "vega": _to_float(position_payload.get("vega")),
        "iv": _to_float(position_payload.get("iv")),
        "dte": position_payload.get("days_to_expiration"),
        "underlying": position_payload.get("underlying"),
        "raw_payload": position_payload,
    }
    await breaker.write("active_positions_cache", payload)


async def write_trade_execution(
    breaker: DBCircuitBreaker,
    execution_payload: dict,
) -> None:
    payload: dict = {
        "timestamp": datetime.now(timezone.utc),
        "broker": str(execution_payload.get("broker") or "IBKR"),
        "account_id": execution_payload.get("account_id"),
        "broker_execution_id": execution_payload.get("broker_execution_id"),
        "symbol": execution_payload.get("symbol"),
        "side": execution_payload.get("side"),
        "quantity": _to_float(execution_payload.get("quantity")),
        "price": _to_float(execution_payload.get("price")),
        "commission": _to_float(execution_payload.get("commission")),
        "execution_time": execution_payload.get("execution_time"),
        "raw_payload": execution_payload,
    }
    await breaker.write("trade_executions", payload)
