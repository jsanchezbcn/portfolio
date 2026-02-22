# portfolioIBKR Development Guidelines

Auto-generated from all feature plans. Last updated: 2026-02-14

## Active Technologies
- Python 3.12 (existing project) + `aiosqlite` (DB — already installed), `ib_insync` / IBKR CP REST (execution), `tastytrade` SDK v12+ (beta data), `yfinance` (beta fallback — add to requirements.txt), `streamlit` + `plotly` (dashboard — already installed), `agents/llm_client.py` (AI — already in project) (003-algo-execution-platform)
- SQLite via `aiosqlite` — extends `database/local_store.py` with two new tables (`trade_journal`, `account_snapshots`) (003-algo-execution-platform)
- Python 3.13 (`.venv`) + `ib_async 2.1.0`, `aiohttp>=3.9.0`, `asyncpg 0.31.0`, `python-dotenv` (005-ibkr-trading-bridge)
- PostgreSQL (`portfolio_engine` @ localhost:5432); new tables `portfolio_greeks` + `api_logs` (005-ibkr-trading-bridge)

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
- 005-ibkr-trading-bridge: Added Python 3.13 (`.venv`) + `ib_async 2.1.0`, `aiohttp>=3.9.0`, `asyncpg 0.31.0`, `python-dotenv`
- 003-algo-execution-platform: Added Python 3.12 (existing project) + `aiosqlite` (DB — already installed), `ib_insync` / IBKR CP REST (execution), `tastytrade` SDK v12+ (beta data), `yfinance` (beta fallback — add to requirements.txt), `streamlit` + `plotly` (dashboard — already installed), `agents/llm_client.py` (AI — already in project)

- 001-streaming-greeks-database: Added Python 3.11+ (asyncio runtime) + `asyncpg`, `websockets`, `tastytrade`, `python-dotenv`, existing internal adapters/models

<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
