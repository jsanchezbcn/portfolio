# Implementation Plan: 005 — IBKR Trading Bridge

**Branch**: `005-ibkr-trading-bridge` | **Date**: 2026-02-21 | **Spec**: [specs/005-ibkr-trading-bridge/spec.md](spec.md)
**Input**: Feature specification from `/specs/005-ibkr-trading-bridge/spec.md`

## Summary

Build a standalone `bridge/` module that connects to IBKR via SOCKET (`ib_async 2.1.0`) or REST PORTAL (`aiohttp`), toggled by `IB_API_MODE` env var. On a 5-second loop, aggregates net portfolio Greeks and persists to a new `portfolio_greeks` PostgreSQL table. API lifecycle events go to `api_logs`. Database writes are protected by the existing `DBCircuitBreaker` (JSONL buffer fallback). SOCKET mode includes a watchdog that handles the nightly 11:45 PM ET IBKR reset.

## Technical Context

**Language/Version**: Python 3.13 (`.venv`)  
**Primary Dependencies**: `ib_async 2.1.0`, `aiohttp>=3.9.0`, `asyncpg 0.31.0`, `python-dotenv`  
**Storage**: PostgreSQL (`portfolio_engine` @ localhost:5432); new tables `portfolio_greeks` + `api_logs`  
**Testing**: pytest (existing `pytest.ini`); mocked `IB`, mocked `asyncpg.Pool`, `aioresponses`  
**Target Platform**: macOS (dev) / Linux (prod), asyncio single-process daemon  
**Project Type**: single — `bridge/` package at repo root  
**Performance Goals**: ≤100 ms to aggregate + write Greeks per 5-second cycle  
**Constraints**: `clientId=10` only; never modify `database/db_manager.py`; `circuit_breaker.py` reused as-is  
**Scale/Scope**: single account, continuous loop, ~30–60 option positions typical

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Gate | Status | Notes |
|------|--------|-------|
| Test-first (TDD) | ✅ PASS | `tests/test_ib_bridge.py` created before shipping |
| No hardcoded credentials | ✅ PASS | All config via `.env` / `os.getenv()` |
| Adapter pattern respected | ✅ PASS | `SocketBridge` and `PortalBridge` share `IBridgeBase` ABC |
| Graceful degradation | ✅ PASS | `DBCircuitBreaker` → JSONL buffer on DB failure |
| No modification of stable modules | ✅ PASS | `database/db_manager.py` and `circuit_breaker.py` untouched |
| No clientId conflict | ✅ PASS | Bridge uses `clientId=10`; iPad uses `clientId=0` |

## Project Structure

### Documentation (this feature)

```text
specs/005-ibkr-trading-bridge/
├── plan.md              ✅ This file
├── research.md          ✅ Phase 0 output
├── data-model.md        ✅ Phase 1 output
├── quickstart.md        ✅ Phase 1 output
└── contracts/           ✅ Phase 1 output (no HTTP endpoints — internal service)
```

### Source Code (repository root)

```text
bridge/
├── __init__.py             # Re-exports BridgeBase, SocketBridge, PortalBridge
├── database_manager.py     # ensure_bridge_schema(), write_portfolio_snapshot(), log_api_event()
├── ib_bridge.py            # IBridgeBase, SocketBridge, PortalBridge, Watchdog
└── main.py                 # Entry point: asyncio.run(main())

tests/
└── test_ib_bridge.py       # All 005 unit tests (mocked IB + DB)
```

**Structure Decision**: Single-package option. `bridge/` lives at repo root alongside `adapters/`, `agents/`, etc. No new top-level app separation needed — this is a background daemon, not a web service.

## Complexity Tracking

No constitution violations.
