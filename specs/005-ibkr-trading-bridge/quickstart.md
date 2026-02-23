# Quickstart: 005 — IBKR Trading Bridge

**Branch**: `005-ibkr-trading-bridge`

---

## Prerequisites

- IBKR Gateway running (`clientportal/` — already managed by `start_dashboard.sh`)
- PostgreSQL `portfolio_engine` DB accessible (`localhost:5432`)
- `.venv` activated with `ib_async>=2.1.0`, `asyncpg`, `aiohttp`, `python-dotenv`

---

## Run (SOCKET mode)

```bash
# Ensure .env has:
#   IB_API_MODE=SOCKET
#   IB_SOCKET_PORT=7496
#   IB_CLIENT_ID=10

source .venv/bin/activate
python -m bridge.main
```

---

## Run (PORTAL mode)

```bash
# Ensure .env has:
#   IB_API_MODE=PORTAL
#   IBKR_GATEWAY_URL=https://localhost:5001

source .venv/bin/activate
python -m bridge.main
```

---

## Expected startup output

```
[2026-02-21 10:00:00] bridge: IB_API_MODE=SOCKET, host=127.0.0.1, port=7496, clientId=10
[2026-02-21 10:00:00] bridge: Schema ensured (portfolio_greeks, api_logs)
[2026-02-21 10:00:00] bridge: DBCircuitBreaker flush loop started (interval=60s)
[2026-02-21 10:00:01] bridge: Connected to IBKR SOCKET
[2026-02-21 10:00:06] bridge: Greeks written → delta=-12.3, gamma=0.05, vega=-843.2, theta=312.1
```

---

## Run tests

```bash
source .venv/bin/activate
python -m pytest tests/test_ib_bridge.py -v
```

---

## Environment variables (full list)

| Variable | Default | Description |
|----------|---------|-------------|
| `IB_API_MODE` | `SOCKET` | `SOCKET` or `PORTAL` |
| `IB_SOCKET_HOST` | `127.0.0.1` | Gateway host |
| `IB_SOCKET_PORT` | `7496` | Gateway port (jsancapi sub-user) |
| `IB_CLIENT_ID` | `10` | TWS client ID |
| `IB_ACCOUNT` | `` | Account ID; empty = auto-detect |
| `IBKR_GATEWAY_URL` | `https://localhost:5001` | Client Portal URL |
| `BRIDGE_POLL_INTERVAL` | `5` | Seconds between DB writes |
| `BRIDGE_BUFFER_PATH` | `~/.portfolio_bridge_buffer.jsonl` | Circuit breaker JSONL buffer |
| `DB_HOST` | `localhost` | PostgreSQL host |
| `DB_PORT` | `5432` | PostgreSQL port |
| `DB_NAME` | `portfolio_engine` | Database name |
| `DB_USER` | `portfolio` | Database user |
| `DB_PASS` | — | Database password |

---

## Verify data is flowing

```sql
-- In psql:
SELECT timestamp, delta, gamma, vega, theta
FROM portfolio_greeks
ORDER BY timestamp DESC
LIMIT 5;

SELECT timestamp, api_mode, status, message
FROM api_logs
ORDER BY timestamp DESC
LIMIT 10;
```
