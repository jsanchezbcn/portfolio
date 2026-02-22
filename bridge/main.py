"""
bridge/main.py
───────────────
Entry-point for the IBKR trading bridge daemon.

Reads every 5 seconds (default), aggregates portfolio-level Greeks, and
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
  BRIDGE_POLL_INTERVAL 5                       (seconds)
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
from functools import partial

import asyncpg
from dotenv import load_dotenv

from bridge.database_manager import ensure_bridge_schema, log_api_event, write_portfolio_snapshot
from bridge.ib_bridge import IBridgeBase, PortalBridge, SocketBridge, Watchdog
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


# ── main coroutine ────────────────────────────────────────────────────────────

async def run() -> None:
    _configure_logging()
    load_dotenv()

    mode = os.getenv("IB_API_MODE", "SOCKET").strip().upper()
    if mode not in ("SOCKET", "PORTAL"):
        raise ValueError(f"IB_API_MODE must be 'SOCKET' or 'PORTAL', got {mode!r}")

    poll_interval = int(os.getenv("BRIDGE_POLL_INTERVAL", "5"))

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
    bridge: IBridgeBase = SocketBridge() if mode == "SOCKET" else PortalBridge()

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
