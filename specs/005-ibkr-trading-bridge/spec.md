# Feature Spec 005: IBKR Trading Bridge

**Branch**: `005-ibkr-trading-bridge`  
**Date**: 2026-02-21  
**Status**: APPROVED

---

## Overview

Build a standalone, production-grade IBKR trading bridge (`bridge/`) that:

1. Connects to IBKR via a toggle: **SOCKET** (`ib_async` → Gateway port 4001, jsancapi sub-user) or **PORTAL** (Client Portal REST via `aiohttp`).
2. Writes net portfolio Greeks (delta, gamma, vega, theta, underlying_price) to a `portfolio_greeks` PostgreSQL table **every 5 seconds**.
3. Logs every API lifecycle event (connect, disconnect, error, reconnect) to an `api_logs` table.
4. Implements a **circuit breaker**: when PG is unreachable, rows are cached to `~/.portfolio_bridge_buffer.jsonl` and flushed automatically on recovery (retry every 60 s).
5. In SOCKET mode: includes a **watchdog** that detects the IBKR 11:45 PM ET daily reset and reconnects after the window.

---

## User Stories

### US-1 — Connection Toggle
As a developer I can switch `IB_API_MODE=SOCKET` ↔ `IB_API_MODE=PORTAL` in `.env` without touching code.  
**AC**: Invalid value raises `ValueError` at startup.

### US-2 — Schema Bootstrap
On startup `bridge/main.py` calls `ensure_bridge_schema()` which creates `portfolio_greeks` and `api_logs` if absent; no-ops if they exist.

### US-3 — 5-Second Greeks Loop
Every 5 seconds write one row:  
`portfolio_greeks(timestamp, contract='PORTFOLIO', delta, gamma, vega, theta, underlying_price)`.

### US-4 — Circuit Breaker
DB write failure → buffer to JSONL file; background loop retries every 60 s; drain on recovery (order preserved).

### US-5 — SOCKET Watchdog
Detects disconnect; if `23:40–23:59 ET` waits until `00:05 ET`; otherwise exponential back-off (3 retries: 5 s, 10 s, 20 s); each attempt logged to `api_logs`.

---

## Deliverables

| File | Purpose |
|------|---------|
| `bridge/database_manager.py` | Bridge PG layer: `ensure_bridge_schema()`, `write_portfolio_snapshot()`, `log_api_event()`, wraps `DBCircuitBreaker` |
| `bridge/ib_bridge.py` | Dual-mode bridge: `SocketBridge` (ib_async) + `PortalBridge` (aiohttp) + watchdog |
| `bridge/main.py` | Entry point: loads config, boots schema, starts asyncio loop |
| `tests/test_ib_bridge.py` | Unit tests (mocked IB, mocked DB) |

---

## Configuration (.env)

```
IB_API_MODE=SOCKET        # SOCKET | PORTAL (default: SOCKET)
IB_SOCKET_HOST=127.0.0.1  # Gateway host for SOCKET mode
IB_SOCKET_PORT=4001        # Gateway port for SOCKET mode (jsancapi sub-user)
IB_CLIENT_ID=10            # ib_async client ID (must not clash with iPad)
IB_ACCOUNT=                # Account ID (e.g. U2052408) — empty = auto-detect
IBKR_GATEWAY_URL=https://localhost:5001  # For PORTAL mode
BRIDGE_POLL_INTERVAL=5     # Seconds between DB writes
BRIDGE_BUFFER_PATH=~/.portfolio_bridge_buffer.jsonl
```

---

## Constraints

- Must NOT disrupt iPad's primary IBKR session (`clientId=10` reserved for bridge; iPad uses 0).
- Must NOT replace or modify `database/db_manager.py` (active, used by dashboard and workers).
- `database/circuit_breaker.py` is reused as-is (11 tests pass).
- `ib_async 2.1.0` already installed.
- `aiohttp >= 3.9.0` already installed.
