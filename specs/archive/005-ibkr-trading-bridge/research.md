# Research: 005 — IBKR Trading Bridge

**Date**: 2026-02-21  
**Status**: COMPLETE — all unknowns resolved

---

## 1. ib_async 2.1.0 — SOCKET Mode API

**Decision**: Use `ib_async.IB` for SOCKET mode.

**Key APIs confirmed** (via `pip show ib_async` + `python -c "from ib_async import IB"`):

| Method                                                     | Signature                                                           | Notes                                  |
| ---------------------------------------------------------- | ------------------------------------------------------------------- | -------------------------------------- |
| `IB.connectAsync(host, port, clientId, timeout, readonly)` | `(self, host='127.0.0.1', port=7497, clientId=1, timeout=4.0, ...)` | Awaitable                              |
| `IB.disconnect()`                                          | `(self) -> str`                                                     | Synchronous                            |
| `IB.isConnected()`                                         | `(self) -> bool`                                                    | State check                            |
| `IB.portfolio(account)`                                    | `(self, account='') -> list[PortfolioItem]`                         | Returns all open positions             |
| `IB.reqMktData(contract, genericTickList, ...)`            | Returns `Ticker`                                                    | Ticker.modelGreeks → OptionComputation |

**PortfolioItem fields** (namedtuple):  
`account, averageCost, contract, count, index, marketPrice, marketValue, position, realizedPNL, unrealizedPNL`

**OptionComputation fields** (via `Ticker.modelGreeks`):  
`delta, gamma, vega, theta, undPrice, pvDividend, price, impliedVol, optPrice`

**Greeks strategy for SOCKET mode**:

1. `IB.portfolio()` → all positions
2. For each position where `contract.secType == 'OPT'`: - `ticker = IB.reqMktData(contract)` with `genericTickList='10,13'` - Wait for `ticker.modelGreeks` to populate (up to 3 s) - Multiply by `position * multiplier`
3. For equities/futures: delta = position (no options Greeks)
4. Sum all → net portfolio δ, γ, ϑ, ν

**Port confirmed**: `IB_SOCKET_PORT=7496` in `.env` (sub-user jsancapi; not 4001 which was old default)

**clientId**: `IB_CLIENT_ID=10` — never conflicts with iPad (`clientId=0`)

---

## 2. PORTAL Mode — REST API

**Decision**: Use `aiohttp.ClientSession` with `ssl=False` (self-signed CP cert).

**Endpoints**:

| Endpoint                                                                           | Purpose                                                         |
| ---------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `GET /v1/api/portfolio/{acct}/positions/0`                                         | All positions (page 0)                                          |
| `GET /v1/api/iserver/marketdata/snapshot?conids={cids}&fields=7308,7309,7310,7311` | Greeks snapshot (delta=7308, gamma=7309, vega=7310, theta=7311) |
| `GET /v1/api/iserver/account/pnl/partitioned`                                      | Account-level PnL                                               |
| `POST /v1/api/iserver/reauthenticate`                                              | If session expires                                              |

**Fields**: `7308`=delta, `7309`=gamma, `7310`=vega, `7311`=theta, `31`=lastPrice.

**Pattern** (aligned with existing `IBKRClient` in `ibkr_portfolio_client.py`):

- `ssl=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)` with `check_hostname=False, verify_mode=CERT_NONE`

---

## 3. Circuit Breaker — Existing Implementation

**Decision**: Reuse `database/circuit_breaker.py` as-is (zero modifications).

**Confirmed behaviour**:

- `DBCircuitBreaker(pool)` — takes `asyncpg.Pool`
- States: `CLOSED` → `OPEN` after 3 consecutive failures → `HALF_OPEN` during flush probe
- Buffer path: env `BRIDGE_BUFFER_PATH` overrides `~/.portfolio_bridge_buffer.jsonl`
- Flush interval: 60 s (background `flush_loop()` task)
- `await breaker.write(table, row_dict)` — the only write entrypoint needed
- `_load_buffer()` called in `__init__` — recovers on restart; starts OPEN if buffer exists

---

## 4. PostgreSQL Schema — New Tables

**Decision**: Two new tables in `portfolio_engine` DB; `ensure_bridge_schema()` in `bridge/database_manager.py`.

**New tables** (bridge-owned, not in `db_manager.py`):

```sql
CREATE TABLE IF NOT EXISTS portfolio_greeks (
    id               BIGSERIAL PRIMARY KEY,
    timestamp        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    contract         TEXT        NOT NULL DEFAULT 'PORTFOLIO',
    delta            DOUBLE PRECISION,
    gamma            DOUBLE PRECISION,
    vega             DOUBLE PRECISION,
    theta            DOUBLE PRECISION,
    underlying_price DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS api_logs (
    id        BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    api_mode  TEXT        NOT NULL,
    message   TEXT        NOT NULL,
    status    TEXT        NOT NULL   -- 'info' | 'warning' | 'error'
);
```

**Rationale**: Separate from `greek_snapshots` (per-position in `db_manager.py`). Bridge writes portfolio-level aggregates only.

---

## 5. Watchdog — ET Window Logic

**Decision**: Use `zoneinfo.ZoneInfo("America/New_York")` (stdlib, Python 3.9+).

**Algorithm**:

```
def in_reset_window(now_et):
    return now_et.hour == 23 and now_et.minute >= 40

def seconds_until_safe(now_et):
    next_day = (now_et + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return (next_day - now_et).total_seconds()
```

**Reconnect backoff**: `[5, 10, 20]` seconds with max 3 attempts before giving up and scheduling next try.

---

## 6. Environment Variables (final list)

| Variable                                              | Default                            | Required                                     |
| ----------------------------------------------------- | ---------------------------------- | -------------------------------------------- |
| `IB_API_MODE`                                         | `SOCKET`                           | No                                           |
| `IB_SOCKET_HOST`                                      | `127.0.0.1`                        | No                                           |
| `IB_SOCKET_PORT`                                      | `7496`                             | No                                           |
| `IB_CLIENT_ID`                                        | `10`                               | No                                           |
| `IB_ACCOUNT`                                          | ``                                 | No (auto-detect from `IB.managedAccounts()`) |
| `IBKR_GATEWAY_URL`                                    | `https://localhost:5001`           | No                                           |
| `BRIDGE_POLL_INTERVAL`                                | `5`                                | No                                           |
| `BRIDGE_BUFFER_PATH`                                  | `~/.portfolio_bridge_buffer.jsonl` | No                                           |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASS` | See `.env`                         | Yes                                          |

---

## 7. Alternatives Considered

| Choice                           | Alternative                | Rejected Because                                                                    |
| -------------------------------- | -------------------------- | ----------------------------------------------------------------------------------- |
| `ib_async` for SOCKET            | `ibapi` (official TWS API) | `ib_async` already installed, async-native, cleaner                                 |
| `asyncpg` pool passed to breaker | New ORM                    | asyncpg already used throughout codebase                                            |
| Single `portfolio_greeks` table  | Reuse `greek_snapshots`    | `greek_snapshots` is per-position; bridge writes aggregates; separation of concerns |
| `zoneinfo` for ET                | `pytz`                     | stdlib preferred; `pytz` not installed                                              |
