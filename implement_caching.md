# Implementation Plan: Comprehensive Caching & Trade Tracking

This document outlines the architecture for a "Warm Cache" system that ensures instant dashboard loads, background updates every 60 seconds, and comprehensive trade tracking for future backtesting and analysis.

## 1. Current State & Bottlenecks

- **Dashboard Load Time**: Currently, the dashboard often fetches positions and Greeks on-demand or relies on fragmented Streamlit caches. This can lead to 10-30s wait times if APIs are slow.
- **Fragmented Background Tasks**:
  - `bridge/main.py` polls Greeks every 5s into **Postgres**.
  - `dashboard/app.py` has a background thread polling snapshots into **SQLite**.
- **Missing Executions**: No automated system currently pulls all historical/live executions (trades) into a durable database for performance analysis.

## 2. Proposed Architecture: "Omni-Cache"

### A. Unified Background Sync Service (`portfolio_sync.py`)

Evolve the current `bridge/main.py` into a full-scale synchronization daemon.

- **Poll Interval**: 60 seconds (configurable).
- **Responsibility**:
  1. **Fetch Portfolio Summary**: Net Liq, Buying Power, Margin, P&L.
  2. **Fetch Positions**: Full position list including all contract metadata.
  3. **Fetch Greeks**: Enrich positions using IBKR Model Greeks or Tastytrade fallback.
  4. **Fetch Executions**: Check for new trades since the last sync.
  5. **Fetch Orders**: Current open orders.
- **Persistence**: Write all the above into a unified **PostgreSQL** (preferred for multi-process access) or **SQLite** database.

### B. Persistent Schema Expansion

We need a robust schema to support both the live dashboard and historical analysis.

#### 1. Account Summary Cache (`account_status_cache`)

Stores the high-level health of the account.

```sql
CREATE TABLE IF NOT EXISTS account_status_cache (
    account_id          TEXT PRIMARY KEY,
    net_liquidation     DOUBLE PRECISION,
    buying_power        DOUBLE PRECISION,
    excess_liquidity    DOUBLE PRECISION,
    maint_margin        DOUBLE PRECISION,
    unrealized_pnl      DOUBLE PRECISION,
    realized_pnl        DOUBLE PRECISION,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

#### 2. Positions Cache (`active_positions_cache`)

Stores individual positions. This allows the dashboard to render the table immediately.

```sql
CREATE TABLE IF NOT EXISTS active_positions_cache (
    id                  BIGSERIAL PRIMARY KEY,
    account_id          TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    quantity            DOUBLE PRECISION NOT NULL,
    avg_price           DOUBLE PRECISION,
    market_value        DOUBLE PRECISION,
    delta               DOUBLE PRECISION,
    gamma               DOUBLE PRECISION,
    theta               DOUBLE PRECISION,
    vega                DOUBLE PRECISION,
    iv                  DOUBLE PRECISION,
    dte                 INTEGER,
    instrument_type     TEXT,
    raw_json            JSONB, -- Full UnifiedPosition payload
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pos_cache_account ON active_positions_cache(account_id);
```

#### 3. Trade Executions (`trade_executions`)

A permanent record of every fill. Essential for backtesting and tax/performance reporting.

```sql
CREATE TABLE IF NOT EXISTS trade_executions (
    id                  BIGSERIAL PRIMARY KEY,
    broker_execution_id TEXT NOT NULL,
    broker              TEXT NOT NULL, -- 'IBKR' or 'TASTY'
    account_id          TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    side                TEXT NOT NULL, -- 'BUY' or 'SELL'
    quantity            DOUBLE PRECISION NOT NULL,
    price               DOUBLE PRECISION NOT NULL,
    commission          DOUBLE PRECISION,
    execution_time      TIMESTAMPTZ NOT NULL,
    raw_payload         JSONB,
    UNIQUE(broker, broker_execution_id)
);
CREATE INDEX IF NOT EXISTS idx_exec_time ON trade_executions(execution_time DESC);
```

### C. Dashboard "Instant Load" Strategy

- **Stage 1 (Initial Load)**: Query the `active_positions_cache` and `account_status_cache` tables. This takes milliseconds.
- **Stage 2 (Stale Check)**: If the `timestamp` of the last sync is > 60s, trigger an asynchronous refresh request.
- **Stage 3 (Subscription)**: The dashboard polls the _database_ (not the API) every 5-10s for changes written by the background sync service.

## 3. Detailed Background Logic

### A. The Sync Loop (`bridge/main.py`)

1. **Fetch Positions**: Call `adapter.fetch_positions()`.
2. **Enrich Greeks**: Call `adapter.fetch_greeks()`.
3. **Atomic Update**:
   - `DELETE FROM active_positions_cache WHERE account_id = ...`
   - `INSERT INTO active_positions_cache (...)`
   - `INSERT INTO account_status_cache (...) ON CONFLICT (account_id) DO UPDATE ...`
4. **Execution Reconciler**:
   - Fetch last 24h of executions from Broker.
   - `INSERT INTO trade_executions (...) ON CONFLICT DO NOTHING`.

### B. Tastytrade Fallback

In the background, if IBKR model Greeks are missing, the sync service will use the `TastytradeAdapter` to fill them before writing to the cache. This ensures the dashboard always has "Best Effort" Greeks without the user waiting for multiple API calls.

## 4. Backtesting & Analysis Benefits

By storing every trade in `trade_executions`, we can:

1. **Reconstruct Portfolio State**: See what the Greeks were _at the moment of entry_.
2. **Win/Loss Analysis**: Automatically group executions into "Trades" (Entry to Exit) to calculate Win Rate, Profit Factor, etc.
3. **Regime Correlation**: Map trades against the `regime_history` to see which market conditions favor our strategies.

## 5. Backward Compatibility & Migration

- **Feature Flag**: Introduce `USE_PERSISTENT_CACHE=True` in `.env`. When `False`, the system behaves as before (on-demand fetching).
- **Graceful Fallback**: If the cache table is empty, the dashboard should automatically trigger a "Force Synchronize" and display a progress bar.
- **Unified DB Manager**: Move `DBCircuitBreaker` logic into a central `DatabaseService` that handles both Postgres (for cloud/server deployment) and SQLite (for local development) consistently.

## 6. Next Actions

1. **Phase 1: Database Foundation**
   - [ ] Implement `ensure_cache_schema()` in `bridge/database_manager.py`.
   - [ ] Add `trade_executions` table for permanent record keeping.

2. **Phase 2: Synchronizer Daemon**
   - [ ] Extend `bridge/main.py` to fetch positions and account metrics.
   - [ ] Implement background Greek enrichment.
   - [ ] Add execution reconciliation logic (fetching recent trades).

3. **Phase 3: Dashboard Integration**
   - [ ] Refactor `dashboard/app.py` to read from `active_positions_cache`.
   - [ ] Add a "Last Synced: X seconds ago" indicator to the UI.
   - [ ] Implement a "Sync Now" button that triggers a background update.

## 7. Implementation Status (Completed)

The following items from this plan are now implemented in code:

- ✅ **Bridge schema expanded** in `bridge/database_manager.py` with:
   - `account_status_cache`
   - `active_positions_cache`
   - `trade_executions`
- ✅ **Bridge write helpers added**:
   - `write_account_status_snapshot(...)`
   - `write_active_position_snapshot(...)`
   - `write_trade_execution(...)`
- ✅ **Bridge poller enhanced** in `bridge/main.py`:
   - Continues writing portfolio Greeks.
   - Now also writes account summary snapshots and per-position cache snapshots for configured accounts.
   - Adds best-effort execution ingestion each poll cycle.
- ✅ **Execution fetch API added** to bridge interface in `bridge/ib_bridge.py`:
   - `SocketBridge.get_recent_executions(...)` (ib_async-backed)
   - `PortalBridge.get_recent_executions(...)` best-effort endpoint support
- ✅ **Dashboard warm-cache path fixed**:
   - `dashboard/app.py` now deserializes worker-cached `UnifiedPosition` payloads correctly (Pydantic model).
   - Cache-first fallback is used before expensive live fetch when snapshots exist.
- ✅ **Worker serialization fixed** in `workers/portfolio_worker.py` so background `fetch_greeks` results are persisted reliably.

### Remaining Work (Next Increment)

- ⏳ Read directly from `active_positions_cache` / `account_status_cache` in dashboard (today dashboard still uses worker + snapshot flow).
- ⏳ Add explicit "Last synced" from bridge cache tables and a "Sync now" control.
- ⏳ Add deduplication constraints/upsert semantics for `trade_executions` (currently best-effort append with in-memory dedupe during runtime).

## 8. Cadence Update (Applied)

- ✅ Portfolio and Greeks refresh cadence is now **30 seconds** by default.
   - `dashboard/app.py`: `PORTFOLIO_REFRESH_SECONDS=30` drives worker freshness checks.
   - `bridge/main.py`: `BRIDGE_POLL_INTERVAL` default updated from `5` to `30` seconds.
