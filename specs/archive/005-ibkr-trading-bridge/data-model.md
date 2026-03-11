# Data Model: 005 — IBKR Trading Bridge

**Date**: 2026-02-21

---

## Entities

### 1. `portfolio_greeks` (PostgreSQL table)

Stores one aggregated portfolio-level Greeks snapshot per poll cycle.

| Column             | Type               | Nullable | Default       | Notes                                      |
| ------------------ | ------------------ | -------- | ------------- | ------------------------------------------ |
| `id`               | `BIGSERIAL`        | No       | auto          | PK                                         |
| `timestamp`        | `TIMESTAMPTZ`      | No       | `NOW()`       | UTC write time                             |
| `contract`         | `TEXT`             | No       | `'PORTFOLIO'` | Always `'PORTFOLIO'` for aggregate rows    |
| `delta`            | `DOUBLE PRECISION` | Yes      | —             | Net signed delta (sum of position × delta) |
| `gamma`            | `DOUBLE PRECISION` | Yes      | —             | Net gamma                                  |
| `vega`             | `DOUBLE PRECISION` | Yes      | —             | Net vega                                   |
| `theta`            | `DOUBLE PRECISION` | Yes      | —             | Net theta                                  |
| `underlying_price` | `DOUBLE PRECISION` | Yes      | —             | SPX/ES price at time of snapshot           |

**Index**: `CREATE INDEX IF NOT EXISTS idx_portfolio_greeks_ts ON portfolio_greeks (timestamp DESC)`

---

### 2. `api_logs` (PostgreSQL table)

Stores API lifecycle events (connect, disconnect, error, reconnect, watchdog sleep).

| Column      | Type          | Nullable | Default | Notes                                |
| ----------- | ------------- | -------- | ------- | ------------------------------------ |
| `id`        | `BIGSERIAL`   | No       | auto    | PK                                   |
| `timestamp` | `TIMESTAMPTZ` | No       | `NOW()` | UTC event time                       |
| `api_mode`  | `TEXT`        | No       | —       | `'SOCKET'` or `'PORTAL'`             |
| `message`   | `TEXT`        | No       | —       | Human-readable event description     |
| `status`    | `TEXT`        | No       | —       | `'info'` \| `'warning'` \| `'error'` |

**Index**: `CREATE INDEX IF NOT EXISTS idx_api_logs_ts ON api_logs (timestamp DESC)`

---

## Row Lifecycle

```
bridge/main.py (every 5 s)
  → bridge.get_portfolio_greeks()   → dict with delta, gamma, vega, theta, underlying_price
  → write_portfolio_snapshot(breaker, row)
      → breaker.write("portfolio_greeks", row)
          CLOSED → asyncpg insert → portfolio_greeks table
          OPEN   → JSONL buffer → ~/.portfolio_bridge_buffer.jsonl

bridge/main.py (on connect/disconnect/error)
  → log_api_event(breaker, api_mode, message, status)
      → breaker.write("api_logs", row)
```

---

## State Transitions

```
Bridge State Machine
--------------------
DISCONNECTED ──connect──► CONNECTED ──poll loop──► CONNECTED
     ▲                         │
     │              disconnect/error
     │                         ▼
     └──── watchdog ──── RECONNECTING
                         (backoff: 5s, 10s, 20s)
                         (23:40–23:59 ET → wait until 00:05 ET)

DB Circuit Breaker
------------------
CLOSED ──3 failures──► OPEN ──flush_loop probe──► HALF_OPEN
  ▲                                                    │
  └────────── all rows drained ────────────────────────┘
              (any row fails → back to OPEN)
```

---

## Validation Rules

- `IB_API_MODE` must be `SOCKET` or `PORTAL` (case-insensitive) — raises `ValueError` otherwise.
- `IB_CLIENT_ID` must not be `0` (reserved for iPad primary session).
- `delta`, `gamma`, `vega`, `theta` default to `0.0` if bridge returns no positions.
- `underlying_price` defaults to `None` if unavailable.
