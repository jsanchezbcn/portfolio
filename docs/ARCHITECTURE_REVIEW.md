# Architecture Review — Portfolio Risk Manager

**Reviewed**: 2026-02-23  
**Reviewer**: GitHub Copilot (Claude Sonnet 4.6)  
**Scope**: All source under `portfolioIBKR/` (excluding `oldproject/`)  
**Branch at review**: `006-trade-proposer`

---

## Executive Summary

The project has grown organically from a single-file script (`ibkr_portfolio_client.py`) into a multi-layer trading platform. The adapter pattern, data models, and core engine are solid. However, six features were added incrementally without a unifying integration pass, leaving behind:

- **3 regime-detection implementations** that partially conflict
- **2 IBKRWebSocketClient classes** (one unused)
- **2 separate database stacks** (PostgreSQL + SQLite) without clear ownership boundaries
- **2 worker patterns** (polling `worker_jobs` table vs. EventBus LISTEN/NOTIFY) both live simultaneously
- A **2,623-line root-level monolith** (`ibkr_portfolio_client.py`) still wired into the adapter layer
- A **1,628-line Streamlit monolith** (`dashboard/app.py`) doing rendering, threading, orchestration, and API calls

The 193 passing unit tests, the circuit-breaker, the SOCKET/PORTAL toggle, and the `risk_matrix.yaml`-driven limits system are all strong foundations. The priority now is a consolidation pass.

---

## 1. Critical Issues (Fix First)

### 1.1 Duplicate IBKRWebSocketClient

Two classes named `IBKRWebSocketClient` exist in the codebase:

| File | Status | Uses |
|---|---|---|
| `streaming/ibkr_ws.py` | Production implementation | `core/processor.py → DBManager` |
| `adapters/ibkr_adapter.py` (lines 23-62) | Dead code / vestigial | Never instantiated anywhere |

**Action**: Delete the copy in `adapters/ibkr_adapter.py`. It publishes raw JSON to EventBus without normalization and causes import confusion.

### 1.2 Three Regime-Detection Implementations

| Location | Mechanism | Consumers |
|---|---|---|
| `risk_engine/regime_detector.py` — `RegimeDetector` | YAML config, VIX + term_structure | `dashboard/app.py`, tests |
| `agents/market_intelligence.py` — `MarketIntelligenceAgent._evaluate_regime()` | Hard-coded thresholds (>22 = high vol), publishes REGIME_CHANGED to EventBus | Nothing (no subscribers confirmed active) |
| `agents/proposer_engine.py` — `RiskRegimeLoader` / `BreachDetector._detect_regime()` | YAML config, VIX + TS + recession_prob scalers | `trade_proposer.py` |

`MarketIntelligenceAgent` uses different thresholds than the YAML (e.g., `VIX > 22` hardcoded vs `VIX >= 25` in YAML `high_volatility`). The three implementations can disagree simultaneously.

**Action**: Make `RiskRegimeLoader` in `proposer_engine.py` the single source of truth. Have `RegimeDetector` delegate to it. Retire the hardcoded logic in `MarketIntelligenceAgent`.

### 1.3 `ibkr_portfolio_client.py` — 2,623-line Root Monolith

This file predates the adapter pattern and contains: HTTP client, authentication logic, position parsing, Greek calculations, Tastytrade fallback, beta estimation, and `IBKRClient`. It is still imported directly by:

- `adapters/ibkr_adapter.py` (wraps `IBKRClient`)
- `scripts/portfolio_cli.py` (instantiates `IBKRClient` 5 times)
- `scripts/verify_greeks_accuracy.py`

**Actions**:
1. Extract `IBKRClient` (the REST wrapper around Client Portal) into `adapters/ibkr_client.py`.
2. Move anything related to Greek computation into `adapters/ibkr_adapter.py`.
3. Keep `ibkr_portfolio_client.py` as a thin re-export shim until all imports are migrated, then delete.

### 1.4 `dashboard/app.py` — 1,628-line Streamlit Monolith

`app.py` currently handles:
- Layout and rendering (correct place for Streamlit)
- Background snapshot threading (`_snapshot_loop`)
- Job dispatch to `worker_jobs` table
- Direct adapter instantiation and API calls
- Inline HTML/CSS generation for regime banners
- `asyncio.run()` calls inside synchronous functions (anti-pattern in Streamlit)

The `_snapshot_loop` thread and adapter setup belong in `workers/` or a dedicated `AppState` service class.

**Actions**:
1. Move `_snapshot_loop` to `workers/portfolio_worker.py` as a new job type (`snapshot`).
2. Extract rendering sections into `dashboard/components/`: `regime_banner.py`, `greeks_panel.py`, `positions_table.py`.
3. Introduce a `dashboard/state.py` that holds the `IBKRAdapter`, `MarketDataService`, and `LocalStore` singletons — initialized once in `app.py`, passed to components.

---

## 2. Architectural Issues (Address in Next Sprint)

### 2.1 Two Worker Patterns Living Side-by-Side

**Pattern A — Job Queue (authoritative)**:
`workers/portfolio_worker.py` polls `worker_jobs` table in PostgreSQL. `dashboard/app.py` enqueues jobs (`fetch_greeks`, `llm_brief`, `llm_audit`, `restart_gateway`). Worker executes and writes result back.

**Pattern B — EventBus LISTEN/NOTIFY (experimental/legacy)**:
`agents/risk_manager.py` and `agents/market_intelligence.py` subscribe to channels over PostgreSQL NOTIFY. This requires a live Postgres connection at startup and leaves dead code if Postgres is unavailable.

Both patterns start from `start_dashboard.sh` simultaneously, meaning two workers are polling the same `worker_jobs` table while also two agents are trying to run LISTEN/NOTIFY. They don't coordinate. Pattern B agents are never confirmed as consumers of any live data path in the dashboard.

**Action**: Decide on one pattern per concern:
- **CPU-bound / long tasks**: Job queue (Pattern A). Already working well.
- **Real-time streaming events**: EventBus ONLY when Postgres is confirmed available; add a guard that downgrades gracefully to a no-op log when the connection fails. Or replace the EventBus entirely with in-process `asyncio.Queue` — there is only one process (portfolio_worker) that needs it.

### 2.2 Dual Database Stack Without Ownership Boundaries

| Store | Tables | Technology |
|---|---|---|
| `database/db_manager.py` | `greek_snapshots`, `worker_jobs`, `staged_orders` | asyncpg / PostgreSQL |
| `database/local_store.py` | `market_intel`, `trade_journal`, `account_snapshots` | aiosqlite / SQLite |
| `bridge/database_manager.py` | `portfolio_greeks`, `api_logs`, `proposed_trades` | asyncpg / PostgreSQL (via circuit breaker) |

The `proposed_trades` table schema lives in `bridge/database_manager.py` but `models/proposed_trade.py` defines it as a SQLModel entity. These can drift.

**Actions**:
1. Document the ownership boundary clearly: SQLite for journal/audit trail (no network dependency), PostgreSQL for real-time telemetry and job coordination.
2. Consolidate all DDL into a single `database/schema.py` (or Alembic migrations).
3. Have `bridge/database_manager.py` import the DDL string from `database/schema.py` rather than duplicating it.

### 2.3 `BrokerAdapter` ABC Is Underspecified

`adapters/base_adapter.py` defines only two abstract methods: `fetch_positions()` and `fetch_greeks()`. But `IBKRAdapter` has grown to include:
- `fetch_account_summary()`
- `simulate_margin_impact()` — What-If API (added for Spec 006)
- `compute_portfolio_greeks()` — delegates to `BetaWeighter`
- `fetch_accounts()`

None of these are on the `BrokerAdapter` ABC, so type-safe code and mocking are harder than necessary.

**Action**: Expand `base_adapter.py` to include the full interface contract. At minimum, add `fetch_account_summary()` and `simulate_margin_impact()` as abstract methods (with default `raise NotImplementedError` for adapters that don't support simulation).

### 2.4 `IBKRAdapter` Is Doing Too Much (1,780 lines)

The adapter has grown to handle:
- Position fetching (SOCKET and PORTAL paths)
- Greeks enrichment (TWS socket, Client Portal snapshot, Tastytrade fallback, stock option cache)
- Beta weighting
- What-If margin simulation (SOCKET and PORTAL paths)
- Low-level HTTP session management
- Greeks cache persistence to `.stock_option_greeks_cache.json`

**Suggested split**:
```
adapters/
  ibkr_positions.py      — fetch_positions() SOCKET + PORTAL
  ibkr_greeks.py         — fetch_greeks() all sources
  ibkr_simulation.py     — simulate_margin_impact()
  ibkr_adapter.py        — thin IBKRAdapter that composes the three above
```
Each sub-module can be tested in isolation.

### 2.5 Configuration Is Scattered and Not Type-Safe

| Config source | What it controls |
|---|---|
| `config/risk_matrix.yaml` | Regime limits, VIX scalers, term-structure scalers |
| `beta_config.json` (root) | Static beta overrides |
| `.env` | All credentials and feature flags |
| `agent_config.py` (root) | LLM system prompt + tool schemas |
| `pytest.ini` | Test configuration |

There is no central Pydantic `Settings` model. Each module reads env vars directly with `os.getenv()` scattered across 15+ files. A typo in a variable name silently falls through to a default.

**Action**: Introduce `config/settings.py` using `pydantic-settings`:
```python
class Settings(BaseSettings):
    ib_api_mode: Literal["SOCKET", "PORTAL"] = "PORTAL"
    ib_socket_port: int = 7496
    db_host: str = "localhost"
    greeks_disable_cache: bool = False
    proposer_interval: int = 300
    llm_model: str = "gpt-4.1"
    # ... all env vars in one place
    model_config = SettingsConfigDict(env_file=".env")
```
Import `settings` throughout instead of `os.getenv()` with magic strings.

### 2.6 Inline Comment in `.env` Requires Code Workaround

```
IB_API_MODE=SOCKET  # Use TWS/IB Gateway socket on 7496; set to PORTAL to use Client Portal REST
```

Multiple places in the code strip inline comments manually:
```python
_api_mode = os.getenv("IB_API_MODE", "PORTAL").split("#")[0].strip().upper()
```

Pydantic `BaseSettings` validators would handle this correctly and eliminate the workaround.

---

## 3. Code-Quality Issues

### 3.1 Root-Level Test Files Outside pytest Discovery Path

`pytest.ini` sets `testpaths = tests agents skills`. These test files are NOT discovered:
- `test_dashboard_playwright.py`
- `test_dashboard_playwright_2.py` through `_final.py` (6 files)
- `test_all_news.py`, `test_alpaca_auth.py`, `test_finnhub_alpaca.py`, `test_news_api.py`
- `test_notifications.py`, `test_telegram_notification.py`, `test_option_display.py`

Some of these are exploratory scripts, some are duplicated by `tests/test_dashboard_playwright_all.py`. Root-level test files should either be moved into `tests/` or deleted.

### 3.2 Root-Level Utility Scripts With No Home

The following files at the project root have no clear module home:
- `debug_api.py`, `debug_greeks_cli.py`, `debug_greeks_live.py` → should be in `scripts/`
- `demo_feature_readiness.py`, `demo_us7_deterministic.py` → one-off demos, should be in `scripts/demos/` or deleted
- `diagnostic_summary.py` → `scripts/`
- `portfolio_menu.py` → unclear purpose, review for deletion
- `ibkr_gateway_client.py` → overlaps with `ibkr_portfolio_client.py`; merge or delete
- `tastytrade_oauth_helper.py`, `tastytrade_options_fetcher.py`, `tastytrade_sdk_options_fetcher.py`, `tastyworks_client.py` → should be in `adapters/`

### 3.3 Multiple Copies of the IBKR Client Portal Gateway

```
clientportal/           ← active
clientportal-beta/      ← development copy
clientportal_backup_old/ ← stale backup
clientportal_latest.gw.zip ← archive
```

`start_dashboard.sh` only uses `clientportal/`. The other three copies add ~50MB to the repo and create confusion. Move the zip to a separate storage location, delete the old backup, keep only `clientportal/` and optionally `clientportal-beta/` if beta testing is active.

### 3.4 `datetime.utcnow()` Deprecation

`models/unified_position.py` line 57 uses:
```python
timestamp: datetime = Field(default_factory=datetime.utcnow)
```
`datetime.utcnow()` is deprecated in Python 3.12+. Replace with `datetime.now(timezone.utc)`.

### 3.5 `get_event_bus()` Has a Hidden PostgreSQL Dependency

```python
def get_event_bus(dsn: str | None = None) -> EventBus:
    global _event_bus
    if _event_bus is None:
        dsn = dsn or os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/portfolio")
        _event_bus = EventBus(dsn)
    return _event_bus
```

`EventBus.__init__()` does not connect until `start()` is called, but `core/execution.py`, `core/order_manager.py`, and `adapters/ibkr_adapter.py` all call `get_event_bus()` at import time via class constructors. If PostgreSQL is not available, `start()` will fail loudly. The EventBus should be made optional with a no-op fallback for components that don't strictly need it.

### 3.6 `asyncio.run()` Called Inside Synchronous Functions in Dashboard

`dashboard/app.py` wraps async adapter calls with `asyncio.run()` from synchronous Streamlit callbacks. This creates a new event loop on every call — problematic with ib_async which expects a persistent loop. The correct pattern is to run the entire async pipeline once per Streamlit refresh using `asyncio.run()` at the top level, not nested inside callbacks.

### 3.7 Missing `__all__` Exports

Most `__init__.py` files are empty. Packages like `adapters/`, `agents/`, `core/`, and `models/` have no `__all__` declarations, making it hard to understand the public API surface.

---

## 4. Testing Gaps

### 4.1 No Integration Tests for the Full Pipeline

`tests/integration/` exists but appears empty (based on directory listing). The most critical path — `IBKRAdapter.fetch_positions() → fetch_greeks() → compute_portfolio_greeks()` — has no integration test against a live (or wiremock) gateway.

### 4.2 Playwright Tests Have Strict-Mode Failures

From the terminal output, 14 Playwright tests fail due to:
- Multiple matching elements for `get_by_role("button", name="Refresh")` (3 elements)
- `get_by_role("button", name="🚨 Flatten Risk")` resolves to 2 elements
- `AttributeError: 'function' object has no attribute 'replace'` for lambda locators

These need scoped selectors (e.g., `page.locator('[data-testid="stSidebarUserContent"]').get_by_role(...)`).

### 4.3 `conftest.py` Could Provide Better Fixtures

`tests/conftest.py` should define a shared `MockIBKRAdapter` with deterministic positions and Greeks so all tests use the same fixture rather than building their own mocks per-file.

---

## 5. Security Issues

### 5.1 Telegram Bot Token Exposed in Terminal History

The terminal context shows:
```
bot8503210871:AAEaAn9AQu0yeXto3z10vE3Ag-qFDFC1Aec
```
This token is live. **Revoke and rotate it immediately** via BotFather. Ensure `.env` is in `.gitignore` (it should be, but double-check `git log -- .env`).

### 5.2 SSL Verification Disabled Globally

```python
# ibkr_portfolio_client.py
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
```
And in `bridge/ib_bridge.py`:
```python
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE
```
This is acceptable for the localhost Client Portal (uses a self-signed cert), but `disable_warnings` globally silences ALL SSL warnings in the process, including external API calls. Pin the IBKR self-signed cert instead and re-enable warnings for external calls.

### 5.3 No Input Validation on IBKR API Responses

The adapter parses raw API responses with `.get()` fallbacks but no schema validation. A malformed response (e.g., a string where a float is expected) can propagate as `None`/`0.0` through Greeks calculations silently. Adding Pydantic response models for IBKR REST payloads would catch these.

---

## 6. Performance Issues

### 6.1 Greeks Cache Per-File JSON (Race Condition Risk)

`IBKRAdapter` loads `.stock_option_greeks_cache.json` at startup and writes it on each update. Multiple processes (worker-1, worker-2, dashboard) all access the same file concurrently with no file lock — a race condition for stale writes.

**Fix**: Use the SQLite `LocalStore` for caching (already initialized per-process, safe with aiosqlite).

### 6.2 Beta Waterfall Makes Synchronous Outbound HTTP Calls

`BetaWeighter._get_beta_from_ibkr()` makes a synchronous `requests.get()` call to the Client Portal inside an async context:
```python
search_resp = session.get(
    f"{base_url}/v1/api/iserver/secdef/search", ...
)
```
This blocks the event loop. Replace with `aiohttp` or run in an executor (`loop.run_in_executor`).

### 6.3 Snapshot Loop Uses Thread Instead of Async Worker

`dashboard/app.py` spawns `_snapshot_loop` as a raw Python `threading.Thread`. This thread creates its own `LocalStore` instance, loops with `time.sleep()`, and calls both sync and async code. It should instead be a job type dispatched to `portfolio_worker.py` so there's a single process owning snapshot writes.

---

## 7. Architectural Simplifications

### 7.1 Eliminate the Bridge Daemon for the Dashboard Use Case

`bridge/ib_bridge.py` is a separate daemon (`bridge/main.py`) that connects to TWS and writes `portfolio_greeks` rows to PostgreSQL every 5 seconds. The dashboard's `portfolio_worker.py` also fetches Greeks (every job cycle). This is two processes doing the same work.

**Decision needed**: Is the bridge daemon needed for the dashboard use case, or only for future latency-sensitive streaming? If the dashboard is the primary consumer, the bridge adds infrastructure complexity without benefit. Candidate for deferring to a later phase.

### 7.2 Replace EventBus with `asyncio.Queue` for In-Process Communication

PostgreSQL LISTEN/NOTIFY (the EventBus) requires a live DB connection for what is essentially in-process event routing. `RiskManagerAgent`, `MarketIntelligenceAgent`, and `portfolio_worker` are all in the same address space. An `asyncio.Queue` or a thin pub/sub using `asyncio.Event` would accomplish the same result without PostgreSQL.

Reserve PostgreSQL NOTIFY for cross-process communication (e.g., dashboard notifying workers), not for agents running in the same process.

### 7.3 Consolidate the Three Tastytrade Files

```
tastyworks_client.py         (root, legacy)
tastytrade_options_fetcher.py (root)
tastytrade_sdk_options_fetcher.py (root)
adapters/tastytrade_adapter.py (correct location)
```

The root-level Tastytrade files are superseded by `adapters/tastytrade_adapter.py`. The adapter already handles session management, Greeks fetching, and beta retrieval. The root files should be removed once `adapters/tastytrade_adapter.py` covers all their functionality.

---

## 8. Future Roadmap Recommendations

### 8.1 Spec 007 — Configuration Service

Priority: **HIGH** before adding more features.  
Introduce `config/settings.py` (Pydantic `BaseSettings`) as described in §2.5. Add environment validation on startup so misconfigured deployments fail fast with a clear error.

### 8.2 Spec 008 — Schema Migrations (Alembic or SQLModel Migrations)

Priority: **HIGH** for PostgreSQL tables.  
Currently, schema changes are applied by `ensure_*_schema()` functions that run `CREATE TABLE IF NOT EXISTS`. This is idempotent for new tables but cannot alter existing tables. Add Alembic for PostgreSQL DDL and keep aiosqlite auto-migration for SQLite.

### 8.3 Spec 009 — Process Supervision

Priority: **MEDIUM**.  
Replace `start_dashboard.sh` (nohup + pkill) with `supervisord` or a `docker-compose.yml`. This gives automatic restart, unified log rotation, and health-check integration.

### 8.4 Spec 010 — Streaming Greeks Pipeline (Post-Dashboard)

Priority: **LOW** until live streaming is required.  
The `streaming/ibkr_ws.py → core/processor.py → DBManager` path is already implemented but not activated by the dashboard. Once latency drops below 500ms SLO become relevant (e.g., trading decisions requiring sub-second Greeks), activate this path and retire the snapshot polling.

### 8.5 Spec 011 — Historical P&L Attribution

Priority: **MEDIUM**.  
The `trade_journal` table plus `account_snapshots` in `LocalStore` have all the data needed for position-level P&L attribution. A `core/pnl_engine.py` module + dashboard tab would close the "Thesis-to-P&L" loop described in `specs/archive/000-SYSTEM-MANIFEST.md`.

### 8.6 Spec 012 — Order Lifecycle Notifications

Priority: **MEDIUM**.  
The `OrderStateMachine` in `core/order_manager.py` transitions through `DRAFT → SIMULATED → STAGED → SUBMITTED → FILLED`. Only submit and simulation are notified now. Wire all state transitions (especially `PARTIAL_FILL`, `REJECTED`) to `NotificationDispatcher` so the trader gets immediate Telegram feedback on order status.

### 8.7 Multi-Account Consolidation View

Priority: **LOW**.  
The dashboard supports multiple accounts via a selector, but Greeks and risk metrics are per-account only. A portfolio-level consolidated view (summing `spx_delta`, `vega`, `theta` across all IBKR accounts) is a natural next step given the data model already supports it (`spx_delta` on `UnifiedPosition`).

---

## 9. Quick Wins (1–2 hours each)

| # | Task | File(s) | Effort |
|---|------|---------|--------|
| Q1 | Delete vestigial `IBKRWebSocketClient` in `adapters/ibkr_adapter.py` (lines 23–62) | `adapters/ibkr_adapter.py` | 15 min |
| Q2 | Replace `datetime.utcnow` → `datetime.now(timezone.utc)` | `models/unified_position.py` | 5 min |
| Q3 | Move `debug_*.py`, `demo_*.py`, `diagnostic_summary.py` to `scripts/` | root | 15 min |
| Q4 | Move 11 root-level `test_*.py` files to `tests/` or delete | root | 30 min |
| Q5 | Delete `clientportal_backup_old/` and `clientportal_latest.gw.zip` from repo | root | 5 min |
| Q6 | Add `__all__` to `adapters/__init__.py`, `agents/__init__.py`, `models/__init__.py` | `*/___init__.py` | 20 min |
| Q7 | Add file lock (or use `LocalStore`) for `.stock_option_greeks_cache.json` writes | `adapters/ibkr_adapter.py` | 45 min |
| Q8 | Pin IBKR cert; re-enable SSL warnings for external APIs | `ibkr_portfolio_client.py` | 30 min |
| Q9 | Rotate Telegram bot token (security) | BotFather + `.env` | 5 min |
| Q10 | Add `PROPOSER_DB_URL` to `configs/settings.py` to decouple proposer from `DATABASE_URL` | `agents/trade_proposer.py` | 20 min |

---

## 10. Dependency Audit

### Unused or Redundant Dependencies

| Package | Status | Notes |
|---------|--------|-------|
| `psycopg2-binary` | **Redundant** | `asyncpg` is used exclusively for Postgres; `psycopg2-binary` is listed but never imported |
| `tastytrade-sdk>=0.0.0` | **Duplicate** | `tastytrade>=7.0.0` already installed; `tastytrade-sdk` is an old name for the same package |
| `sqlmodel` | **Underused** | Only `models/proposed_trade.py` uses it; everything else is raw asyncpg or aiosqlite |
| `websockets` | **Direct dep** | Used by vestigial WebSocket in `ibkr_adapter.py`; after Q1, only `streaming/ibkr_ws.py` uses it (still needed) |

### Version Pins Too Loose

`requirements.txt` uses `>=` for everything. For a trading system, pin exact versions for:
- `ib_async`, `asyncpg`, `aiohttp`, `tastytrade` — these have breaking API changes between minor versions
- Use `pip-tools` or `uv` to generate `requirements.lock` with hash-pinned versions.

---

## 11. Spec 006 (Trade Proposer) — Outstanding Items

Current status: Core engine built and tested (84 passing tests). Integration with live dashboard pending.

| Item | Status | Priority |
|------|--------|----------|
| `simulate_margin_impact()` — SOCKET path with live TWS | Implemented, not verified with real What-If response | P1 |
| Dashboard Trade Approval Queue tab | Not yet built | P2 |
| `proposed_trades` table visible in dashboard | Not yet connected | P2 |
| Regime mismatch between `proposer_engine.py` and `regime_detector.py` (§1.2) | Open — needs unification | P1 |
| `trade_proposer.py` started from `start_dashboard.sh` but PostgreSQL is optional | If PG is down, proposer loop will crash on DB write | P1 |

---

## Appendix: Layer Map (Current State)

```
┌──────────────────────────────────────────────────────────┐
│ dashboard/app.py  (1628 lines — Streamlit UI + threading) │
│  components/: order_builder, order_management,           │
│              ibkr_login, flatten_risk, ai_suggestions,  │
│              historical_charts, trade_journal_view      │
└────────────────────┬─────────────────────────────────────┘
                     │ imports
┌────────────────────▼─────────────────────────────────────┐
│  Adapters                                                │
│  adapters/ibkr_adapter.py   (wraps IBKRClient + ib_async)│
│  adapters/tastytrade_adapter.py                          │
│  adapters/polymarket_adapter.py                          │
│  [ibkr_portfolio_client.py → IBKRClient  ← needs refactor]│
└────────────────────┬─────────────────────────────────────┘
                     │ uses
┌────────────────────▼─────────────────────────────────────┐
│  Core / Risk / Models                                    │
│  core/execution.py    core/order_manager.py              │
│  risk_engine/beta_weighter.py                            │
│  risk_engine/regime_detector.py ← duplicated in agents/ │
│  models/unified_position.py   models/order.py            │
└────────────────────┬─────────────────────────────────────┘
                     │ reads/writes
┌────────────────────▼─────────────────────────────────────┐
│  Database Layer                                          │
│  database/local_store.py    (SQLite — journal, intel)    │
│  database/db_manager.py     (PostgreSQL — snapshots, jobs)│
│  database/circuit_breaker.py (PostgreSQL fault tolerance) │
│  bridge/database_manager.py (PostgreSQL — bridge tables) │
└──────────────────────────────────────────────────────────┘

Background Processes (start_dashboard.sh):
  workers/portfolio_worker.py  × 2  (job queue workers)
  agents/trade_proposer.py          (5-min risk monitoring loop)
  agents/telegram_bot.py            (Telegram interface)
  bridge/main.py                    (NOT started by default)
  streaming/ clients                (NOT activated in dashboard)
```

---

*This document reflects the architecture as of branch `006-trade-proposer`. Priority order: Critical (§1) → Architectural (§2) → Code Quality (§3) → Security (§5) → Quick Wins (§9).*
