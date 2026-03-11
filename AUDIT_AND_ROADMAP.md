# Portfolio IBKR — Comprehensive Audit & Roadmap

**Date**: 2026-02-23  
**Scope**: Full codebase audit (~18,000 lines), specs review (6 completed features + 1 active), architecture analysis, and forward-looking roadmap.  
**Excluded**: `oldproject/` (legacy reference)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Critical Bugs — Fix Immediately](#2-critical-bugs--fix-immediately)
3. [Security Vulnerabilities](#3-security-vulnerabilities)
4. [Architecture Issues](#4-architecture-issues)
5. [Portfolio Management Gaps](#5-portfolio-management-gaps)
6. [Code Quality & Technical Debt](#6-code-quality--technical-debt)
7. [Test Coverage Gaps](#7-test-coverage-gaps)
8. [New Feature Suggestions](#8-new-feature-suggestions)
9. [Phased Implementation Plan](#9-phased-implementation-plan)

---

## 1. Executive Summary

The system is a sophisticated multi-agent options portfolio manager with real-time Greeks streaming, regime-aware risk limits, AI-powered trade suggestions, and a Streamlit dashboard. Six feature specs have been completed (001→005 + 006), covering ~335 total tasks.

**Strengths:**

- Excellent risk matrix design with NLV-relative scaling and VIX/term-structure multipliers
- Solid adapter pattern for multi-broker support (IBKR + Tastytrade)
- Well-designed circuit breaker for database resilience
- Strong safety contracts (2-step trade confirmation, `transmit=False` order staging)
- Comprehensive execution engine with WhatIf simulation

**Weaknesses:**

- 11 critical/high-severity bugs in active code paths
- SQL injection in EventBus + circuit breaker
- Dashboard is a 1,731-line monolith
- Three divergent regime detection implementations
- No CI/CD pipeline
- Missing VaR/CVaR/Sharpe — only basic Greeks tracked
- No P&L attribution or strategy-level performance tracking

---

## 2. Critical Bugs — Fix Immediately

### BUG-001: Strike Calculation Double-Division (proposer_engine.py)

**File**: `agents/proposer_engine.py` ~L420  
**Impact**: All proposed hedge trades have nearly ATM strikes instead of OTM wings.

```python
# Current (WRONG): _PUT_SPREAD_WING is 0.02 (2%), dividing by 100 = 0.0002
strike_short = round(atm_price * (1 - self._PUT_SPREAD_WING / 100), -1)
# Fix:
strike_short = round(atm_price * (1 - self._PUT_SPREAD_WING), -1)
```

### BUG-002: VIX Always Returns 18.0 (proposer_engine.py)

**File**: `agents/proposer_engine.py` ~L673  
**Impact**: Trade proposer always uses VIX=18.0 for limit scaling regardless of actual market volatility. `BreachEvent` has no `_vix` attribute → `__dict__.get("_vix", 18.0)` always returns the default.

### BUG-003: Duplicate Email Notifications (notification_dispatcher.py)

**File**: `agent_tools/notification_dispatcher.py` ~L75  
**Impact**: Email backup block fires **unconditionally** after the primary channel `if/elif/else` chain, causing double-sends when email is the primary.

```python
# Duplicate condition:
if self.email_enabled and self.email_enabled and self.smtp_username:
```

### BUG-004: VIX Grows Monotonically (market_intelligence.py)

**File**: `agents/market_intelligence.py` ~L47  
**Impact**: `self.vix_level += 5.0` on every negative-high-impact news event. VIX never decrements — after 3 bad headlines, internal VIX is +15 above reality, triggering false crisis-mode escalations.

### BUG-005: TypeError on LLM Audit Job (portfolio_worker.py)

**File**: `workers/portfolio_worker.py` ~L178  
**Impact**: `LLMRiskAuditor(db=db, regime_detector=regime_detector)` — constructor does not accept `regime_detector` kwarg → `TypeError` crash when an `llm_audit` job executes.

### BUG-006: Duplicate OrderStatus Enum Values (models/order.py)

**File**: `models/order.py` ~L48-52  
**Impact**: `PARTIAL` vs `PARTIAL_FILL` and `CANCELLED` vs `CANCELED` are distinct enum members with different string values. `execution.py` uses `PARTIAL` while `order_manager.py` uses `PARTIAL_FILL`, causing silent status mismatches.

### BUG-007: Conflicting State Machines (order_manager.py vs order.py)

**File**: `core/order_manager.py` ~L37-53 vs `models/order.py` ~L56-64  
**Impact**: Two independent FSMs define incompatible transition graphs. `OrderStateMachine` allows `SIMULATED→STAGED`, but `Order._ALLOWED_TRANSITIONS` doesn't include `STAGED` at all. The `OrderStateMachine` class is actually dead code — never invoked at runtime.

### BUG-008: Broken `__main__` in 4 Agents

**Files**: `agents/capital_allocator.py`, `agents/execution_agent.py`, `agents/market_intelligence.py`, `agents/risk_manager.py`  
**Impact**: `asyncio.run(agent.start())` completes and closes the loop, then `asyncio.get_event_loop().run_forever()` creates a **new empty loop** — agent will never process events after first run.

### BUG-009: aiosqlite Missing from requirements.txt

**File**: `requirements.txt`  
**Impact**: `database/local_store.py` imports `aiosqlite` but the package is absent from requirements. Fresh installs will fail with `ModuleNotFoundError`.

### BUG-010: `NewsSentry()` Missing Args in Dashboard

**File**: `dashboard/app.py` ~L1298  
**Impact**: `NewsSentry()` instantiated without required `symbols` and `db` kwargs → `TypeError` when user clicks "Fetch News" in dashboard.

### BUG-011: Inconsistent Greeks Multiplier (Tastytrade)

**File**: `adapters/tastytrade_adapter.py` ~L107  
**Impact**: `IBKRAdapter.fetch_greeks()` applies `contract_multiplier` to position greeks. `TastytradeAdapter.fetch_greeks()` does **not** — same position produces different dollar-denominated greeks depending on which broker path executes. Critical for accurate portfolio-level aggregation.

---

## 3. Security Vulnerabilities

### SEC-001: SQL Injection in EventBus (CRITICAL)

**File**: `core/event_bus.py` ~L66  
**Risk**: `payload_str` is f-string interpolated into `NOTIFY {channel}, '{payload_str}'`. A single-quote in the payload breaks or exploits the query.  
**Fix**: Use `asyncpg`'s `$1` parameter binding or `asyncpg.utils.quote_literal()`.

### SEC-002: SQL Column Injection in Circuit Breaker (CRITICAL)

**File**: `database/circuit_breaker.py` ~L218  
**Risk**: `_db_insert()` builds column names from `row.keys()` via string interpolation. While currently safe (internal data), any future user-influenced data in keys would be injectable.  
**Fix**: Whitelist known column names or use SQLAlchemy ORM.

### SEC-003: SSL Verification Disabled Everywhere (HIGH)

**Files**: `core/execution.py`, `risk_engine/beta_weighter.py`, `dashboard/components/order_management.py`, `bridge/ib_bridge.py`  
**Risk**: All IBKR Client Portal HTTPS calls use `verify=False`. Even on localhost, this enables MITM attacks.  
**Fix**: Use a custom CA cert from the Client Portal's keystore, or add a `IBKR_SSL_VERIFY` env toggle that defaults to `True`.

### SEC-004: IBKR Reply Challenges Auto-Confirmed (HIGH)

**File**: `dashboard/components/order_management.py` ~L176-194  
**Risk**: The `_modify_order` function auto-confirms all IBKR warning challenges. These warnings include price limit violations, contract size issues, and market order warnings.  
**Fix**: Surface the warning message to the user for explicit confirmation.

### SEC-005: World-Readable /tmp Status Files (MEDIUM)

**Files**: `dashboard/components/ibkr_login.py`, `scripts/ibkr_auto_login.py`  
**Risk**: Login status written to `/tmp/ibkr_login_status.json` — world-readable on multi-user systems.  
**Fix**: Use `tempfile.mkdtemp()` with restricted permissions, or use `~/.cache/portfolioIBKR/`.

### SEC-006: Credentials in Module-Level Globals (MEDIUM)

**File**: `scripts/ibkr_auto_login.py` ~L60-64  
**Risk**: `IBKR_USER` and `IBKR_PASS` persist as global variables for the entire process lifetime.  
**Fix**: Scope credentials to the function that uses them, or use a `SecretStr` wrapper.

---

## 4. Architecture Issues

### ARCH-001: Dashboard Monolith (1,731 lines)

**File**: `dashboard/app.py`  
**Problem**: The entire Streamlit dashboard lives in one file with a single `main()` function spanning ~900+ lines. Component files exist in `dashboard/components/` but `app.py` still handles data fetching, caching, rendering, and state management.  
**Fix**: Extract into ~8 component modules with a thin `app.py` routing shell. Move data fetching to a `dashboard/data_layer.py` service.

### ARCH-002: Three Divergent Regime Detection Implementations

| Implementation                               | File                             | VIX Source     | Uses YAML?                    | Uses Scalers? |
| -------------------------------------------- | -------------------------------- | -------------- | ----------------------------- | ------------- |
| `RegimeDetector.detect_regime()`             | `risk_engine/regime_detector.py` | Parameter      | **No** (hardcoded thresholds) | No            |
| `RiskRegimeLoader.detect_regime()`           | `agents/proposer_engine.py`      | Parameter      | Yes                           | Yes           |
| `MarketIntelligenceAgent._evaluate_regime()` | `agents/market_intelligence.py`  | Internal state | **No** (hardcoded thresholds) | No            |

Three independent implementations can return different regime names for the same market conditions. Only `RiskRegimeLoader` actually reads the YAML config and applies VIX/TS scalers.  
**Fix**: Consolidate into a single `RegimeService` in `risk_engine/` that all consumers use.

### ARCH-003: Dual Database Layer Without Interface Contract

- `database/db_manager.py` (PostgreSQL, 885 lines) — used by streaming, workers, scripts
- `database/local_store.py` (SQLite, 514 lines) — used by dashboard, execution engine

Both have overlapping APIs (`insert_market_intel`, `get_recent_market_intel`, `upsert_market_intel`) but divergent schemas and semantics (`INSERT OR REPLACE` vs `DELETE` + `INSERT`).  
**Fix**: Define a `StorageProtocol` (Python `Protocol`) that both implement, ensuring feature parity.

### ARCH-004: DSN Construction Duplicated 3×

DSN building exists in:

1. `database/db_manager.py` → `dsn` property
2. `bridge/main.py` → `_build_dsn()`
3. `agent_config.py` → `StreamingEnvironmentConfig`

**Fix**: Extract to a single `config/database.py` utility.

### ARCH-005: IBKRAdapter God-Class (1,777 lines)

`adapters/ibkr_adapter.py` is a position fetcher, Greeks enricher, option chain fetcher, and margin simulator all in one class.  
**Fix**: Split into focused classes: `IBKRPositionFetcher`, `IBKRGreeksService`, `IBKROptionChainService`, `IBKRMarginSimulator`, all behind the unified `IBKRAdapter` facade.

### ARCH-006: BrokerAdapter ABC Incomplete

`adapters/base_adapter.py` declares only `fetch_positions()` and `fetch_greeks()`, but both concrete adapters also implement `compute_portfolio_greeks()` — which is not in the ABC. Polymorphic calls through `BrokerAdapter` will fail for this method.  
**Fix**: Add `compute_portfolio_greeks()` to the ABC. Also move `PolymarketAdapter` out of `adapters/` into `data_sources/` since it's not a broker adapter.

### ARCH-007: Blocking I/O in Async Code

| Location                                | Blocking Call                            | Fix                                       |
| --------------------------------------- | ---------------------------------------- | ----------------------------------------- |
| `risk_engine/beta_weighter.py` ~L213    | `requests.get()` inside coroutine        | Use `aiohttp` or `asyncio.to_thread()`    |
| `database/circuit_breaker.py` ~L228-268 | File I/O + `os.fsync()` under async lock | Use `aiofiles` or `asyncio.to_thread()`   |
| `agents/arb_hunter.py` ~L155            | Sync nested loops in async `scan()`      | Move computation to `asyncio.to_thread()` |

### ARCH-008: No Dependency Injection

Components are tightly coupled via direct imports and constructor wiring. The `DBManager` singleton pattern with a class-level `asyncio.Lock()` makes testing harder and couples unrelated modules.  
**Fix**: Introduce a lightweight factory/registry pattern for services.

### ARCH-009: Thread-Safety Violations in Streamlit

`st.session_state` is mutated from background threads in `dashboard/app.py` (snapshot loop) and `dashboard/components/ai_suggestions.py`. Streamlit's session state is **not** thread-safe.  
**Fix**: Use Streamlit's `@st.fragment` + `st.cache_resource` patterns, or communicate via a thread-safe queue.

---

## 5. Portfolio Management Gaps

### PM-001: No Portfolio-Level P&L Tracking

The system tracks Greeks in real-time but has **no daily/weekly/monthly P&L series**. Without a P&L time series, it's impossible to calculate Sharpe, Sortino, VaR, or CVaR.  
**Priority**: HIGH — this is the foundation for all risk-adjusted performance metrics.

### PM-002: No VaR / CVaR / Tail Risk Metrics

The `risk-metrics-calculation` skill defines VaR, CVaR, Sharpe, Sortino, Calmar, Omega, and drawdown analysis — but **none of these are implemented** anywhere in the codebase. Risk monitoring is purely Greeks-based.  
**Priority**: HIGH — VaR/CVaR are industry-standard risk measures.

### PM-003: No Stress Testing

No historical or Monte Carlo stress testing exists. The system cannot answer: "What would happen to my portfolio if VIX spiked to 40?" or "How would I have performed during March 2020?"  
**Priority**: MEDIUM — critical for professional portfolio management.

### PM-004: No Strategy-Level Attribution

Positions are tracked individually, but there's no concept of grouping positions into strategies (e.g., "SPX Iron Condor March", "SPY Calendar") and tracking P&L per strategy. The `active_strategies` table exists in the original spec but was never implemented.  
**Priority**: MEDIUM — essential for knowing which strategies are working.

### PM-005: No Correlation Analysis

The system doesn't track cross-position or cross-underlying correlations. In a crisis, correlated positions can amplify losses beyond what individual Greeks suggest.  
**Priority**: MEDIUM — stress correlation during market downturns is a key risk factor.

### PM-006: No Position Aging / Time Decay Tracking

Theta decay is tracked instantaneously but there's no historical tracking of how positions are aging — e.g., "this position has been at 50% max profit for 5 days" which is a common exit signal.  
**Priority**: MEDIUM — managing winners is as important as managing losers.

### PM-007: No Greeks P&L Decomposition

The current system shows delta, gamma, theta, vega as point-in-time snapshots. It doesn't decompose daily P&L into components: "Today you gained $X from theta decay, lost $Y from delta movement, and $Z from vega expansion."  
**Priority**: MEDIUM — critical for understanding what's driving returns.

### PM-008: Kelly Criterion / Position Sizing Not Connected to Live Data

`capital_allocator.py` has a half-Kelly implementation but uses hardcoded `max_position_size_pct=0.05` and `kelly_fraction=0.5`. It's not connected to historical win rates, expected values, or live strategy performance.  
**Priority**: LOW — useful but not urgent until PM-001 and PM-004 are done.

### PM-009: No Margin Utilization Time Series

Margin usage is checked point-in-time but never historicized. The system can't show "margin utilization increased from 30% to 65% this week."  
**Priority**: LOW — important for capital efficiency monitoring.

### PM-010: No Liquidity Risk Assessment

The spec mentions a "Liquidity Guard" (bid/ask spread > 10% = stale price) but it's not implemented. Illiquid positions can't be closed quickly in a crisis.  
**Priority**: LOW — important for real-world execution.

### PM-011: Sebastian Theta/Vega Ratio Only Forward-Looking

The |Θ|/|V| ratio (Sebastian's Insurance Model, target 0.25–0.40) is displayed in the dashboard but not tracked historically. There's no analysis of "when does my theta/vega ratio predict future performance?"  
**Priority**: LOW — analytical enhancement.

---

## 6. Code Quality & Technical Debt

### DEBT-001: 20+ Uses of Deprecated `datetime.utcnow()`

Mixed naive/aware datetimes across the codebase. `processor.py` and `unified_position.py` use `datetime.now(timezone.utc)` correctly, but `execution.py`, `order.py`, `ibkr_adapter.py`, `dashboard/app.py`, and 8+ other files use the deprecated `datetime.utcnow()`.  
**Fix**: Global search-and-replace with `datetime.now(timezone.utc)`.

### DEBT-002: Hardcoded Contract Multiplier Maps (4× duplication)

Futures/options multiplier lookup tables are duplicated in `ibkr_adapter.py` (~3 locations) and `core/processor.py`.  
**Fix**: Extract to `config/instruments.py` or `models/instruments.py`.

### DEBT-003: `_run_async()` Helper Duplicated

`dashboard/app.py` and `dashboard/components/trade_journal_view.py` have nearly identical `_run_async()` implementations.  
**Fix**: Extract to `dashboard/utils.py`.

### DEBT-004: Dead Code

| Location                                | Dead Code                                                | Action                  |
| --------------------------------------- | -------------------------------------------------------- | ----------------------- |
| `core/order_manager.py` L37-60          | `OrderStateMachine` class — never invoked                | Remove                  |
| `adapters/tastytrade_adapter.py` L17-47 | `TastytradeWebSocketClient` stub — no auth, no reconnect | Remove or complete      |
| `core/execution.py` ~L501               | `elif n_legs == 2:` nested inside `if n_legs == 2:`      | Fix logic               |
| `models/order.py` L48-52                | `PARTIAL_FILL` + `CANCELED` duplicates                   | Remove one of each pair |
| `agents/execution_agent.py` L44-65      | `_simulate_execution` and `_on_market_data` stubs        | Mark as TODO or remove  |

### DEBT-005: f-String Logging Anti-Pattern

`capital_allocator.py`, `execution_agent.py`, `market_intelligence.py`, `risk_manager.py` use `logger.info(f"...")` instead of `logger.info("...", arg1, arg2)`. The f-string is evaluated even when the log level would suppress the message.

### DEBT-006: EventBus.\_running Accessed as Private Attribute

Used in 3 locations (`execution.py`, `order_manager.py`, `tastytrade_adapter.py`). Should be exposed as a public `@property is_running`.

### DEBT-007: Risk Matrix Example/Live Key Mismatch

`risk_matrix.example.yaml` uses `max_beta_delta` but the live file uses `legacy_max_beta_delta`. A new contributor copying the example would get silent fallback to defaults.

### DEBT-008: SQLAlchemy Engine Created Per Request

`agents/trade_proposer.py` `_get_session()` creates a new `create_engine()` + `Session()` on **every** 5-minute cycle. SQLAlchemy engines are expensive to create. Also present in dashboard Approve/Reject button handlers.  
**Fix**: Create engine once at module/app level and reuse.

### DEBT-009: Double `resp.json()` Calls

`core/market_data.py` ~L134-145 and `risk_engine/beta_weighter.py` ~L241-242 call `resp.json()` twice, re-parsing the response body each time.

### DEBT-010: No Type-Hint Standard

The codebase mixes Python 3.9+ style (`list[X]`, `dict[str, Any]`) with legacy `typing` imports (`List[dict]`, `Optional[str]`). Should standardize on modern syntax since the project targets Python 3.13.

---

## 7. Test Coverage Gaps

### Current State: ~7,800 lines of tests across 35 files

| Module                          | Source Lines | Test Lines            | Coverage Rating        |
| ------------------------------- | ------------ | --------------------- | ---------------------- |
| `core/execution.py`             | 805          | 748                   | ★★★★★ Excellent        |
| `risk_engine/beta_weighter.py`  | 361          | 344                   | ★★★★★ Excellent        |
| `database/circuit_breaker.py`   | 288          | 231                   | ★★★★★ Excellent        |
| `agents/proposer_engine.py`     | 822          | 881                   | ★★★★★ Excellent        |
| `agents/trade_proposer.py`      | 243          | 351                   | ★★★★☆ Good             |
| `core/market_data.py`           | 440          | 312                   | ★★★★☆ Good             |
| `database/local_store.py`       | 514          | 311                   | ★★★☆☆ Adequate         |
| **`database/db_manager.py`**    | **885**      | **54**                | **★☆☆☆☆ Critical gap** |
| **`streaming/ibkr_ws.py`**      | **264**      | **46**                | **★☆☆☆☆ Critical gap** |
| **`streaming/tasty_dxlink.py`** | **121**      | **49**                | **★☆☆☆☆ Critical gap** |
| **`dashboard/app.py`**          | **1,731**    | **0**                 | **☆☆☆☆☆ No tests**     |
| **`scripts/portfolio_cli.py`**  | **837**      | **570** (greeks only) | **★★☆☆☆ Partial**      |
| **`bridge/main.py`**            | **169**      | **~10**               | **★☆☆☆☆ Minimal**      |

### Critical Test Gaps

1. **`database/db_manager.py`** — 885-line module with 54 lines of tests. Zero coverage for: `ensure_schema()`, `insert_trade()`, `fetch_snapshots()`, `insert_staged_order()`, all `market_intel` methods, entire worker job queue (`enqueue_job`, `claim_next_job`, `complete_job`, `fail_job`, `get_latest_job_result`, `cleanup_old_jobs`).

2. **`dashboard/`** — 4,000+ lines of Streamlit UI with **zero automated tests**. Rendering logic, data transformation, session state management all untested.

3. **`streaming/`** — 385 lines of WebSocket code with ~95 lines of tests. Reconnection, message parsing, subscription management, heartbeat — all poorly tested.

4. **No integration tests for Trade Proposer** — The full cycle (breach detection → candidate generation → margin simulation → DB persistence) has no end-to-end test against a real or mock database.

5. **No CI/CD Pipeline** — No `.github/workflows/`, no pre-commit hooks enforced, no automated test runs on push. The `pytest.ini` is configured but nothing triggers it automatically.

---

## 8. New Feature Suggestions

### Feature 007: Portfolio Analytics Engine

| Item                          | Description                                                                                                            |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| **Daily P&L Time Series**     | Record end-of-day NLV, calculate daily returns, persist to `portfolio_returns` table                                   |
| **VaR/CVaR Calculator**       | Implement historical, parametric, and Cornish-Fisher VaR using the installed `risk-metrics-calculation` skill patterns |
| **Greeks P&L Decomposition**  | Break daily P&L into delta, gamma, theta, vega, and "unexplained" components                                           |
| **Sharpe / Sortino / Calmar** | Rolling risk-adjusted return metrics displayed on dashboard                                                            |
| **Max Drawdown Tracker**      | Real-time watermark tracking with duration analysis                                                                    |
| **Dashboard Panel**           | New "Analytics" tab with rolling metrics charts                                                                        |

### Feature 008: Strategy Grouping & Attribution

| Item                           | Description                                                                                                |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| **Strategy Model**             | Group positions into named strategies (Iron Condor, Calendar, etc.)                                        |
| **Per-Strategy P&L**           | Track realized + unrealized P&L per strategy                                                               |
| **Strategy Performance Table** | Win rate, average win/loss, expectancy per strategy                                                        |
| **Strategy Greeks**            | Aggregate Greeks per strategy for targeted risk management                                                 |
| **Auto-Tagging**               | Heuristic strategy identification from leg structure (existing code in `execution.py` ~L490 has the start) |

### Feature 009: Stress Testing Module

| Item                       | Description                                                                                      |
| -------------------------- | ------------------------------------------------------------------------------------------------ |
| **Historical Scenarios**   | "What if March 2020 happened again?" — replay historical VIX/SPX paths against current positions |
| **Hypothetical Shocks**    | "What if VIX spikes to 40?" — parameterized scenario analysis                                    |
| **Monte Carlo Simulation** | Generate 10,000 scenarios with regime-aware volatility                                           |
| **Correlation Stress**     | Test portfolio under elevated cross-asset correlation (crisis conditions)                        |
| **Dashboard Panel**        | Interactive scenario builder with P&L impact visualization                                       |

### Feature 010: Position Lifecycle Manager

| Item                      | Description                                                                              |
| ------------------------- | ---------------------------------------------------------------------------------------- |
| **Position Aging**        | Track days-in-trade, % of max profit, % of max loss per position                         |
| **Exit Rules Engine**     | Configurable rules: "Close at 50% max profit", "Close at 21 DTE", "Close at 2× max loss" |
| **Alert on Exit Signals** | Telegram notification when a position hits an exit condition                             |
| **Dashboard Widget**      | Traffic-light indicators (green/yellow/red) for position health                          |

### Feature 011: Advanced Order Types

| Item                        | Description                                                                                                |
| --------------------------- | ---------------------------------------------------------------------------------------------------------- |
| **Bracket Orders**          | Entry + take-profit + stop-loss as a single atomic order                                                   |
| **Trailing Stops**          | Percentage or dollar-based trailing stop-loss orders                                                       |
| **OCO (One-Cancels-Other)** | Paired orders where execution of one cancels the other                                                     |
| **Scheduled Orders**        | "Roll this position at 3:45 PM on expiration Friday"                                                       |
| **TWAP/VWAP Execution**     | Time-weighted and volume-weighted average price execution algorithms (stub exists in `execution_agent.py`) |

### Feature 012: Enhanced Notification System

| Item                          | Description                                                                     |
| ----------------------------- | ------------------------------------------------------------------------------- |
| **Notification Preferences**  | Per-alert-type channel selection (e.g., "Breach → Telegram, Fills → Email")     |
| **Daily Summary Report**      | Automated end-of-day report: P&L, Greeks changes, regime status, open proposals |
| **Weekly Performance Digest** | Sharpe, drawdown, strategy attribution in a formatted Telegram message          |
| **Quiet Hours**               | Don't send notifications outside market hours (fix DST bug in process)          |
| **Escalation Rules**          | "If crisis_mode for >30 min with no action, escalate to phone call"             |

### Feature 013: Multi-Account Support

| Item                       | Description                                   |
| -------------------------- | --------------------------------------------- |
| **Account Selector**       | Dashboard dropdown to switch between accounts |
| **Cross-Account Greeks**   | Aggregate Greeks across all accounts          |
| **Account-Level Limits**   | Per-account risk matrix overrides             |
| **Consolidated Reporting** | Unified P&L and risk view across accounts     |

### Feature 014: Options Flow & Market Microstructure

| Item                         | Description                                                                     |
| ---------------------------- | ------------------------------------------------------------------------------- |
| **GEX Calculator**           | Gamma Exposure calculation and "Zero Gamma" level tracking (from original spec) |
| **Vanna/Charm Flows**        | Track dealer positioning impact on price (from original spec)                   |
| **Put/Call Ratio**           | Aggregate put/call OI and volume ratios                                         |
| **Unusual Options Activity** | Detect large block trades or unusual volume                                     |
| **OpEx Pinning Detection**   | Identify high-OI strikes near expiration for pinning signals                    |

### Feature 015: CI/CD & DevOps

| Item                        | Description                                                          |
| --------------------------- | -------------------------------------------------------------------- |
| **GitHub Actions Workflow** | Run `pytest tests/` on every push and PR                             |
| **Pre-Commit Hooks**        | Enforce `ruff`, `mypy --strict`, `detect-secrets`                    |
| **Docker Compose**          | Single-command setup: Postgres + Client Portal + Streamlit + Workers |
| **Health Check Endpoint**   | HTTP endpoint reporting status of all services                       |
| **Log Aggregation**         | Structured JSON logging → centralized viewer                         |

---

## 9. Phased Implementation Plan

### Phase 1: Stabilization (1-2 weeks)

**Goal**: Fix all critical bugs and security vulnerabilities. Zero new features.

| ID    | Task                                                                      | Priority | Effort |
| ----- | ------------------------------------------------------------------------- | -------- | ------ |
| P1-01 | Fix BUG-001: strike double-division in proposer_engine.py                 | CRITICAL | 15 min |
| P1-02 | Fix BUG-002: add `vix` field to `BreachEvent`, pass through proposer      | CRITICAL | 30 min |
| P1-03 | Fix BUG-003: deduplicate email send in notification_dispatcher.py         | CRITICAL | 15 min |
| P1-04 | Fix BUG-004: reset VIX to market level in market_intelligence.py          | CRITICAL | 30 min |
| P1-05 | Fix BUG-005: correct LLMRiskAuditor constructor call                      | CRITICAL | 10 min |
| P1-06 | Fix BUG-006: consolidate OrderStatus (remove PARTIAL_FILL, CANCELED)      | HIGH     | 1 hr   |
| P1-07 | Fix BUG-007: unify FSMs into single source in models/order.py             | HIGH     | 1 hr   |
| P1-08 | Fix BUG-008: fix asyncio `__main__` in 4 agent files                      | HIGH     | 30 min |
| P1-09 | Fix BUG-009: add `aiosqlite` to requirements.txt                          | HIGH     | 5 min  |
| P1-10 | Fix BUG-010: pass required args to NewsSentry in dashboard                | HIGH     | 15 min |
| P1-11 | Fix BUG-011: apply contract_multiplier in tastytrade_adapter.fetch_greeks | HIGH     | 30 min |
| P1-12 | Fix SEC-001: parameterize EventBus NOTIFY payload                         | CRITICAL | 30 min |
| P1-13 | Fix SEC-002: whitelist columns in circuit_breaker.\_db_insert             | HIGH     | 30 min |
| P1-14 | Fix SEC-004: surface IBKR reply challenges to user                        | HIGH     | 1 hr   |
| P1-15 | Replace all `datetime.utcnow()` with `datetime.now(timezone.utc)`         | MEDIUM   | 1 hr   |
| P1-16 | Fix risk_matrix.example.yaml key names to match live file                 | MEDIUM   | 15 min |

### Phase 2: Architecture Cleanup (2-3 weeks)

**Goal**: Reduce technical debt and establish solid foundations for new features.

| ID    | Task                                                               | Priority | Effort |
| ----- | ------------------------------------------------------------------ | -------- | ------ |
| P2-01 | Consolidate 3 regime detectors into single `RegimeService`         | HIGH     | 1 day  |
| P2-02 | Extract dashboard `app.py` into ~8 component modules               | HIGH     | 2 days |
| P2-03 | Split IBKRAdapter into focused sub-classes (1,777→4×~450 lines)    | MEDIUM   | 2 days |
| P2-04 | Define `StorageProtocol` for DBManager/LocalStore parity           | MEDIUM   | 1 day  |
| P2-05 | Extract DSN builder to `config/database.py`                        | MEDIUM   | 2 hrs  |
| P2-06 | Extract contract multiplier maps to `config/instruments.py`        | MEDIUM   | 2 hrs  |
| P2-07 | Add `compute_portfolio_greeks()` to BrokerAdapter ABC              | MEDIUM   | 1 hr   |
| P2-08 | Move PolymarketAdapter to `data_sources/`                          | LOW      | 30 min |
| P2-09 | Fix blocking I/O in async code (beta_weighter, circuit_breaker)    | MEDIUM   | 1 day  |
| P2-10 | Remove dead code (OrderStateMachine stub, TastytradeWS stub, etc.) | LOW      | 1 hr   |
| P2-11 | Add EventBus `is_running` public property                          | LOW      | 15 min |
| P2-12 | Standardize type hints to Python 3.13 syntax                       | LOW      | 2 hrs  |
| P2-13 | Fix Streamlit thread-safety (use st.cache_resource / fragments)    | MEDIUM   | 1 day  |

### Phase 3: Test Coverage & CI/CD (1-2 weeks)

**Goal**: Achieve 80%+ coverage on critical paths and automate testing.

| ID    | Task                                                                       | Priority | Effort |
| ----- | -------------------------------------------------------------------------- | -------- | ------ |
| P3-01 | Write tests for `db_manager.py` (target 30+ test cases)                    | HIGH     | 2 days |
| P3-02 | Write tests for `streaming/ibkr_ws.py` reconnection and parsing            | HIGH     | 1 day  |
| P3-03 | Write Streamlit component tests (using `AppTest` from `streamlit.testing`) | MEDIUM   | 2 days |
| P3-04 | Write integration test for Trade Proposer full cycle                       | MEDIUM   | 1 day  |
| P3-05 | Write tests for `bridge/main.py` daemon lifecycle                          | MEDIUM   | 1 day  |
| P3-06 | Set up GitHub Actions CI workflow (`pytest`, `ruff`, `mypy`)               | HIGH     | 4 hrs  |
| P3-07 | Add `pre-commit` config (ruff, mypy, detect-secrets)                       | MEDIUM   | 2 hrs  |
| P3-08 | Add test coverage reporting (pytest-cov → threshold 80%)                   | MEDIUM   | 1 hr   |

### Phase 4: Portfolio Analytics (Feature 007) (2-3 weeks)

**Goal**: Build the P&L and risk metrics foundation.

| ID    | Task                                                                     | Priority | Effort |
| ----- | ------------------------------------------------------------------------ | -------- | ------ |
| P4-01 | Create `portfolio_returns` PostgreSQL table (date, nlv, daily_return)    | HIGH     | 2 hrs  |
| P4-02 | Build `analytics/pnl_tracker.py` — EOD snapshot worker                   | HIGH     | 1 day  |
| P4-03 | Build `analytics/risk_metrics.py` — VaR, CVaR, Sharpe, Sortino, drawdown | HIGH     | 2 days |
| P4-04 | Build `analytics/greeks_attribution.py` — daily P&L decomposition        | HIGH     | 2 days |
| P4-05 | Add rolling metrics (63-day window)                                      | MEDIUM   | 1 day  |
| P4-06 | Add Analytics dashboard tab with Plotly charts                           | MEDIUM   | 2 days |
| P4-07 | Add margin utilization time series                                       | LOW      | 1 day  |
| P4-08 | Write comprehensive tests for analytics module                           | HIGH     | 2 days |

### Phase 5: Strategy & Stress Testing (Features 008-009) (3-4 weeks)

**Goal**: Strategy-level attribution and stress testing capabilities.

| ID    | Task                                                          | Priority | Effort |
| ----- | ------------------------------------------------------------- | -------- | ------ |
| P5-01 | Design `strategies` table and `StrategyGroup` model           | HIGH     | 1 day  |
| P5-02 | Build strategy auto-tagger from leg structure                 | MEDIUM   | 2 days |
| P5-03 | Build per-strategy P&L tracking                               | HIGH     | 2 days |
| P5-04 | Build strategy performance dashboard panel                    | MEDIUM   | 2 days |
| P5-05 | Build `analytics/stress_test.py` — historical scenario replay | MEDIUM   | 2 days |
| P5-06 | Build hypothetical shock analysis                             | MEDIUM   | 1 day  |
| P5-07 | Build Monte Carlo simulation with regime-aware volatility     | MEDIUM   | 2 days |
| P5-08 | Add stress test dashboard panel                               | MEDIUM   | 2 days |
| P5-09 | Add correlation stress analysis                               | LOW      | 1 day  |

### Phase 6: Position Lifecycle & Notifications (Features 010, 012) (2-3 weeks)

**Goal**: Automated position management signals and enhanced alerting.

| ID    | Task                                                            | Priority | Effort |
| ----- | --------------------------------------------------------------- | -------- | ------ |
| P6-01 | Build position aging tracker (days-in-trade, % max profit/loss) | MEDIUM   | 2 days |
| P6-02 | Build exit rules engine (configurable via YAML)                 | MEDIUM   | 2 days |
| P6-03 | Add position health dashboard widget (traffic lights)           | MEDIUM   | 1 day  |
| P6-04 | Build daily summary report generator                            | MEDIUM   | 2 days |
| P6-05 | Build weekly performance digest                                 | LOW      | 1 day  |
| P6-06 | Fix DST bug in telegram_bot.py market hours check               | MEDIUM   | 30 min |
| P6-07 | Add per-alert notification preferences                          | LOW      | 1 day  |
| P6-08 | Add quiet hours support                                         | LOW      | 2 hrs  |

### Phase 7: DevOps & Advanced Features (Features 011, 013-15) (Ongoing)

**Goal**: Production-readiness and advanced trading capabilities.

| ID    | Task                                                            | Priority | Effort |
| ----- | --------------------------------------------------------------- | -------- | ------ |
| P7-01 | Docker Compose (Postgres + Client Portal + Streamlit + Workers) | HIGH     | 2 days |
| P7-02 | Health check endpoint for all services                          | MEDIUM   | 1 day  |
| P7-03 | Structured JSON logging → centralized viewer                    | MEDIUM   | 1 day  |
| P7-04 | Multi-account support in dashboard and adapters                 | MEDIUM   | 3 days |
| P7-05 | GEX/Vanna/Charm flow calculations (from original spec)          | LOW      | 3 days |
| P7-06 | Bracket / OCO order types                                       | LOW      | 2 days |
| P7-07 | TWAP/VWAP execution algorithms                                  | LOW      | 3 days |
| P7-08 | Unusual options activity detection                              | LOW      | 2 days |

---

## Appendix A: File-Level Issue Index

For quick reference, here's every file with known issues:

| File                                       | Issues                                |
| ------------------------------------------ | ------------------------------------- |
| `adapters/base_adapter.py`                 | ARCH-006                              |
| `adapters/ibkr_adapter.py`                 | ARCH-005, DEBT-001, DEBT-002          |
| `adapters/polymarket_adapter.py`           | ARCH-006, DEBT-001                    |
| `adapters/tastytrade_adapter.py`           | BUG-011, DEBT-001, DEBT-004           |
| `agent_tools/notification_dispatcher.py`   | BUG-003                               |
| `agent_tools/market_data_tools.py`         | DEBT-001                              |
| `agents/capital_allocator.py`              | BUG-008, DEBT-005                     |
| `agents/execution_agent.py`                | BUG-008, DEBT-004, DEBT-005           |
| `agents/market_intelligence.py`            | BUG-004, BUG-008, ARCH-002            |
| `agents/proposer_engine.py`                | BUG-001, BUG-002                      |
| `agents/risk_manager.py`                   | BUG-008, ARCH-002                     |
| `agents/telegram_bot.py`                   | DST bug in market hours               |
| `agents/trade_proposer.py`                 | DEBT-008                              |
| `bridge/ib_bridge.py`                      | SEC-003                               |
| `bridge/main.py`                           | ARCH-004                              |
| `config/risk_matrix.example.yaml`          | DEBT-007                              |
| `core/event_bus.py`                        | SEC-001                               |
| `core/execution.py`                        | DEBT-001, DEBT-004, DEBT-006          |
| `core/market_data.py`                      | DEBT-003, DEBT-009                    |
| `core/order_manager.py`                    | BUG-007, DEBT-004, DEBT-006           |
| `dashboard/app.py`                         | ARCH-001, ARCH-009, BUG-010, DEBT-001 |
| `dashboard/components/ibkr_login.py`       | SEC-005                               |
| `dashboard/components/order_management.py` | SEC-003, SEC-004                      |
| `database/circuit_breaker.py`              | SEC-002, ARCH-007                     |
| `database/db_manager.py`                   | ARCH-004                              |
| `models/order.py`                          | BUG-006, BUG-007, DEBT-001            |
| `models/proposed_trade.py`                 | DEBT-001                              |
| `requirements.txt`                         | BUG-009                               |
| `risk_engine/beta_weighter.py`             | ARCH-007, DEBT-009, SEC-003           |
| `risk_engine/regime_detector.py`           | ARCH-002                              |
| `scripts/ibkr_auto_login.py`               | SEC-005, SEC-006                      |
| `workers/portfolio_worker.py`              | BUG-005                               |

---

## Appendix B: Metrics Summary

| Metric                                       | Value      |
| -------------------------------------------- | ---------- |
| Total source lines (excl. oldproject, tests) | ~11,100    |
| Total test lines                             | ~7,800     |
| Test-to-source ratio                         | 0.70       |
| Critical bugs                                | 11         |
| Security vulnerabilities                     | 6          |
| Architecture issues                          | 9          |
| Portfolio management gaps                    | 11         |
| Technical debt items                         | 10         |
| Suggested new features                       | 9          |
| Estimated Phase 1 effort                     | 1-2 weeks  |
| Estimated full roadmap                       | 3-4 months |
