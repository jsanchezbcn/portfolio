"""
bridge/main.py
───────────────
Entry-point for the IBKR trading bridge daemon.

Reads every 30 seconds (default), aggregates portfolio-level Greeks, and
persists to PostgreSQL via a DBCircuitBreaker.

Usage:
  python -m bridge.main                      # SOCKET mode (default)
  IB_API_MODE=PORTAL python -m bridge.main   # PORTAL mode

Environment variables:
  IB_API_MODE          SOCKET | PORTAL          (default: SOCKET)
  IB_SOCKET_HOST       127.0.0.1               (SOCKET only)
  IB_SOCKET_PORT       7496                    (SOCKET only)
  IB_CLIENT_ID         10                      (SOCKET only)
  IBKR_GATEWAY_URL     https://localhost:5001  (PORTAL only)
  IBKR_ACCOUNT_ID      <account number>        (PORTAL only)
    BRIDGE_POLL_INTERVAL 30                      (seconds)
  DB_HOST              localhost
  DB_PORT              5432
  DB_NAME              portfolio_engine
  DB_USER              portfolio
  DB_PASS              <password>
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import sys
from datetime import datetime, timezone
from functools import partial

import asyncpg
from dotenv import load_dotenv

from bridge.database_manager import ensure_bridge_schema, log_api_event, write_portfolio_snapshot
from bridge.database_manager import (
    write_account_status_snapshot,
    write_active_position_snapshot,
    write_trade_execution,
)
from bridge.ib_bridge import (
    API_MODE_SOCKET,
    IBridgeBase,
    Watchdog,
    build_bridge_from_env,
    normalize_api_mode,
)
from adapters.ibkr_adapter import IBKRAdapter
from database.circuit_breaker import DBCircuitBreaker

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _build_dsn() -> str:
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "portfolio_engine")
    user = os.getenv("DB_USER", "portfolio")
    password = os.getenv("DB_PASS", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


def _redacted(dsn: str) -> str:
    return re.sub(r":(.[^:@]*)@", ":***@", dsn, count=1)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _resolve_account_ids() -> list[str]:
    accounts_env = [a.strip() for a in os.getenv("IB_ACCOUNTS", "").split(",") if a.strip()]
    if accounts_env:
        return accounts_env
    single = (os.getenv("IBKR_ACCOUNT_ID") or "").strip()
    return [single] if single else []


def _position_to_dict(position: object) -> dict:
    model_dump = getattr(position, "model_dump", None)
    if callable(model_dump):
        payload = model_dump()
        return payload if isinstance(payload, dict) else {}
    dict_fn = getattr(position, "dict", None)
    if callable(dict_fn):
        payload = dict_fn()
        return payload if isinstance(payload, dict) else {}
    return {}


# ── main coroutine ────────────────────────────────────────────────────────────

async def run() -> None:
    _configure_logging()
    load_dotenv()

    mode = normalize_api_mode(os.getenv("IB_API_MODE", API_MODE_SOCKET))

    poll_interval = int(os.getenv("BRIDGE_POLL_INTERVAL", "30"))

    # ── database ──────────────────────────────────────────────────────────
    dsn = _build_dsn()
    logger.info("Connecting to Postgres at %s", _redacted(dsn))
    pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=1,
        max_size=5,
        command_timeout=10.0,
    )
    await ensure_bridge_schema(pool)

    breaker = DBCircuitBreaker(pool)
    asyncio.create_task(breaker.flush_loop(), name="circuit_breaker_flush")
    logger.info("DBCircuitBreaker started (state=%s)", breaker.state)

    # ── bridge ────────────────────────────────────────────────────────────
    bridge: IBridgeBase = build_bridge_from_env()
    adapter = IBKRAdapter()
    account_ids = _resolve_account_ids()
    logger.info("Bridge cache sync accounts: %s", ", ".join(account_ids) if account_ids else "<none>")

    # Partial for watchdog → log_api_event callbacks
    async def _on_reconnect(message: str, status: str) -> None:
        await log_api_event(breaker, mode, message, status)

    await bridge.connect()
    await log_api_event(breaker, mode, f"Bridge connected ({mode} mode)", "info")

    watchdog = Watchdog()
    asyncio.create_task(watchdog.run(bridge, on_reconnect_cb=_on_reconnect), name="watchdog")
    logger.info("Watchdog started")

    # ── poll loop ─────────────────────────────────────────────────────────
    logger.info("Polling IBKR Greeks every %d s …", poll_interval)

    stop_event = asyncio.Event()

    def _handle_signal(sig: signal.Signals) -> None:
        logger.info("Received %s — shutting down", sig.name)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, partial(_handle_signal, sig))

    try:
        last_exec_time: datetime | None = None
        seen_execution_ids: set[str] = set()
        while not stop_event.is_set():
            try:
                row = await bridge.get_portfolio_greeks()
                await write_portfolio_snapshot(breaker, row)
                logger.debug(
                    "Snapshot written  δ=%.2f  γ=%.4f  ν=%.2f  θ=%.2f",
                    row.get("delta") or 0,
                    row.get("gamma") or 0,
                    row.get("vega")  or 0,
                    row.get("theta") or 0,
                )

                for account_id in account_ids:
                    try:
                        summary = await asyncio.to_thread(adapter.get_account_summary, account_id)
                        if isinstance(summary, dict) and summary:
                            await write_account_status_snapshot(breaker, account_id, summary)
                    except Exception as exc:
                        logger.warning("Account summary sync failed for %s: %s", account_id, exc)

                    try:
                        positions = await adapter.fetch_positions(account_id)
                        if positions:
                            positions = await adapter.fetch_greeks(positions)
                            snapshot_id = f"{account_id}:{datetime.now(timezone.utc).isoformat()}"
                            for position in positions:
                                pos_payload = _position_to_dict(position)
                                if pos_payload:
                                    await write_active_position_snapshot(
                                        breaker,
                                        account_id=account_id,
                                        snapshot_id=snapshot_id,
                                        position_payload=pos_payload,
                                    )
                    except Exception as exc:
                        logger.warning("Position cache sync failed for %s: %s", account_id, exc)

                try:
                    execution_rows = await bridge.get_recent_executions(since=last_exec_time)
                    max_seen_time = last_exec_time
                    for execution in execution_rows:
                        exec_id = str(execution.get("broker_execution_id") or "")
                        if exec_id and exec_id in seen_execution_ids:
                            continue
                        await write_trade_execution(breaker, execution)
                        if exec_id:
                            seen_execution_ids.add(exec_id)
                        exec_time = execution.get("execution_time")
                        if isinstance(exec_time, datetime):
                            if max_seen_time is None or exec_time > max_seen_time:
                                max_seen_time = exec_time
                    last_exec_time = max_seen_time
                    if len(seen_execution_ids) > 5000:
                        seen_execution_ids = set(list(seen_execution_ids)[-3000:])
                except Exception as exc:
                    logger.warning("Execution sync failed: %s", exc)
            except Exception as exc:
                logger.error("Poll cycle error: %s", exc, exc_info=True)
                await log_api_event(breaker, mode, str(exc), "error")

            try:
                await asyncio.wait_for(
                    asyncio.shield(stop_event.wait()),
                    timeout=poll_interval,
                )
            except asyncio.TimeoutError:
                pass  # normal — just woke up for next poll
    finally:
        logger.info("Shutting down bridge …")
        await bridge.disconnect()
        await log_api_event(breaker, mode, "Bridge disconnected (graceful shutdown)", "info")
        # Give circuit breaker one last flush
        await asyncio.sleep(1)
        await pool.close()
        logger.info("Shutdown complete")


# ── entry-point ───────────────────────────────────────────────────────────────

def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
