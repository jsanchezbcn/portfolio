# Feature Spec: IBKR Trading Bridge (US-7)

**Branch**: `004-risk-managed-edge-portfolio`  
**Date**: 2026-02-21  
**Status**: APPROVED

---

## Overview

Build a production-grade, non-blocking Python trading bridge that connects to IBKR via either a Socket (ib_async) or REST (Client Portal) transport, and persists live portfolio Greeks and API logs to the existing PostgreSQL database every 5 seconds.

---

## User Stories

### US-7.1 — Connection Toggle
As a developer, I want to switch between SOCKET and PORTAL IBKR connections via a single `.env` variable (`IB_API_MODE`) without changing code, so that the production iPad session is never disrupted.

**Acceptance Criteria:**
- `IB_API_MODE=SOCKET` → ib_async connection to `127.0.0.1:4001` (jsancapi sub-user)
- `IB_API_MODE=PORTAL` → aiohttp polling of `IBKR_GATEWAY_URL` (Client Portal REST)
- Default: `SOCKET`
- Invalid value logs an error and halts startup

### US-7.2 — PostgreSQL Schema Management
As an operator, I want the bridge to verify and create required tables on startup, so that no manual DB setup is needed.

**Acceptance Criteria:**
- On connect: run "schema check" — create tables if absent, skip if present
- Table `portfolio_greeks`: `(id BIGSERIAL PK, timestamp TIMESTAMPTZ, contract TEXT, delta DOUBLE, gamma DOUBLE, vega DOUBLE, theta DOUBLE, underlying_price DOUBLE)`
- Table `api_logs`: `(id BIGSERIAL PK, timestamp TIMESTAMPTZ, api_mode TEXT, message TEXT, status TEXT)`
- Uses existing `asyncpg` pool from `database/db_manager.py`

### US-7.3 — 5-Second Greeks Persistence Loop
As a risk manager, I want Net Portfolio Vega and Net Portfolio Gamma written to the DB every 5 seconds, so I have a time-series audit trail of my risk exposure.

**Acceptance Criteria:**
- Every 5 seconds: fetch net portfolio Greeks (delta, gamma, vega, theta) + underlying_price
- Write one row to `portfolio_greeks` with `contract='PORTFOLIO'`
- Non-blocking: DB write does NOT pause the market data feed
- On SOCKET mode: uses ib_async reqPortfolioUpdates / reqPnL
- On PORTAL mode: polls `/v1/api/portfolio/{accountId}/positions/0` + `/v1/api/portfolio/{accountId}/summary`

### US-7.4 — Circuit Breaker / Offline Buffer
As an operator, I want undelivered DB rows cached locally when PostgreSQL is unreachable, so no data is lost during DB outages.

**Acceptance Criteria:**
- If DB write fails: append to `~/.portfolio_bridge_buffer.jsonl` (JSON Lines)
- Retry flush every 60 seconds
- On reconnect: drain buffer file into DB in order
- Log each circuit-breaker event to `api_logs`

### US-7.5 — Socket Watchdog (SOCKET mode only)
As an operator, I want the ib_async connection automatically recovered after the IBKR 11:45 PM ET daily reset, so the bridge restarts without manual intervention.

**Acceptance Criteria:**
- Watchdog checks connection health every 30 seconds
- If disconnected AND current time is between 23:40–23:59 ET → wait until 00:05 ET then reconnect
- Otherwise disconnect → immediate reconnect (up to 3 retries with exponential back-off)
- Each reconnect attempt logged to `api_logs`

---

## Technical Requirements

| # | Requirement | Priority |
|---|---|---|
| TR-1 | `IB_API_MODE` env var controls transport | P1 |
| TR-2 | `ib_async` library for SOCKET mode | P1 |
| TR-3 | `aiohttp` for PORTAL mode | P1 |
| TR-4 | `asyncpg` pool (reuse existing `DBManager`) | P1 |
| TR-5 | Circuit breaker with JSON Lines local buffer | P1 |
| TR-6 | Schema auto-create on startup | P1 |
| TR-7 | Non-blocking asyncio loop | P1 |
| TR-8 | Watchdog for 11:45 PM ET reset (SOCKET) | P2 |
| TR-9 | `jsancapi` sub-user on port 4001 | P1 |
| TR-10 | Unit tests for bridge logic (mocked) | P1 |

---

## Out of Scope

- Order execution via the bridge (handled by `adapters/ibkr_adapter.py`)
- Dashboard UI changes
- Tastytrade or Polymarket integration in this feature

---

## Constraints

- Must not break existing `database/db_manager.py` (`greek_snapshots`, `trades` tables)
- `ib_async` not currently installed → install as part of this feature
- Buffer file path: `~/.portfolio_bridge_buffer.jsonl` (configurable via `BRIDGE_BUFFER_PATH`)
- Port 4001 is IBKR Gateway live/paper trading (jsancapi sub-account)
