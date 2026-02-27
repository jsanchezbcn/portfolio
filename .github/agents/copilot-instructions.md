# portfolioIBKR Development Guidelines

Auto-generated from all feature plans. Last updated: 2026-02-14

## Active Technologies
- Python 3.12 (existing project) + `aiosqlite` (DB — already installed), `ib_insync` / IBKR CP REST (execution), `tastytrade` SDK v12+ (beta data), `yfinance` (beta fallback — add to requirements.txt), `streamlit` + `plotly` (dashboard — already installed), `agents/llm_client.py` (AI — already in project) (003-algo-execution-platform)
- SQLite via `aiosqlite` — extends `database/local_store.py` with two new tables (`trade_journal`, `account_snapshots`) (003-algo-execution-platform)
- Python 3.13 (`.venv`) + `ib_async 2.1.0`, `aiohttp>=3.9.0`, `asyncpg 0.31.0`, `python-dotenv` (005-ibkr-trading-bridge)
- PostgreSQL (`portfolio_engine` @ localhost:5432); new tables `portfolio_greeks` + `api_logs` (005-ibkr-trading-bridge)
- Python 3.13.x + `ib_async==2.1.0`, `aiohttp>=3.9.0`, `psycopg2-binary`, existing `database.circuit_breaker.DBCircuitBreaker` (005-ibkr-trading-bridge)
- PostgreSQL tables (`portfolio_greeks`, `api_logs`) + local JSONL buffer file (`~/.portfolio_bridge_buffer.jsonl`) (005-ibkr-trading-bridge)
- Python 3.13 + `ib_async`, `aiohttp`, `SQLAlchemy/SQLModel`, `NotificationDispatcher` (006-trade-proposer)
- PostgreSQL (Table: `proposed_trades`) (006-trade-proposer)

- Python 3.11+ (asyncio runtime) + `asyncpg`, `websockets`, `tastytrade`, `python-dotenv`, existing internal adapters/models (001-streaming-greeks-database)

## Project Structure

```text
src/
tests/
```

## Commands

cd src [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] pytest [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] ruff check .

## Code Style

Python 3.11+ (asyncio runtime): Follow standard conventions

## Recent Changes
- 006-trade-proposer: Added Python 3.13 + `ib_async`, `aiohttp`, `SQLAlchemy/SQLModel`, `NotificationDispatcher`
- 005-ibkr-trading-bridge: Added Python 3.13.x + `ib_async==2.1.0`, `aiohttp>=3.9.0`, `psycopg2-binary`, existing `database.circuit_breaker.DBCircuitBreaker`
- 005-ibkr-trading-bridge: Added Python 3.13 (`.venv`) + `ib_async 2.1.0`, `aiohttp>=3.9.0`, `asyncpg 0.31.0`, `python-dotenv`


<!-- MANUAL ADDITIONS START -->

## Feature 006: Trade Proposer (agents/trade_proposer.py)

**Key components:**
- `agents/proposer_engine.py` — `RiskRegimeLoader`, `BreachDetector`, `CandidateGenerator`, `ProposerEngine`, `_build_justification`
- `agents/trade_proposer.py` — 300s async loop; `--run-once` flag; `MOCK_BREACH=TRUE` for CI
- `models/proposed_trade.py` — SQLModel ORM for `proposed_trades` PostgreSQL table
- `adapters/ibkr_adapter.py` — `simulate_margin_impact(account_id, legs)` method (SOCKET + PORTAL modes)
- `bridge/database_manager.py` — `ensure_proposed_trades_schema(pool)` creates the table

**proposed_trades table columns:** `id`, `account_id`, `strategy_name`, `legs_json` (JSONB), `net_premium`, `init_margin_impact`, `maint_margin_impact`, `margin_impact`, `efficiency_score`, `delta_reduction`, `vega_reduction`, `status` (Pending/Approved/Rejected/Superseded), `justification`, `created_at`

**Supersede rule (FR-014):** Before inserting new proposals, all `Pending` rows for the account are set to `Superseded` so the dashboard always shows the freshest top-3.

**Option C notification:** `send_alert()` is fired when `any(c.efficiency_score > PROPOSER_NOTIFY_THRESHOLD for c in top3)` OR `regime == "crisis_mode"`.

**Benchmark allowlist (FR-011):** Only SPX, SPY, and ES are valid candidates.

**CI smoke test:** `MOCK_BREACH=TRUE python -m agents.trade_proposer --run-once`

**Tests:**
- `tests/test_proposer_engine.py` — 71 tests covering RiskRegimeLoader, BreachDetector (4 regimes), CandidateGenerator, ProposerEngine scoring/ranking/persistence
- `tests/test_trade_proposer.py` — 13 tests covering run_cycle, Option C notification, MOCK_BREACH mode, --run-once CLI

<!-- MANUAL ADDITIONS END -->
