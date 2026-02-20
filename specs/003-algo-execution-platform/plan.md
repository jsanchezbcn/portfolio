# Implementation Plan: Read-Write Algorithmic Execution & Journaling Platform

**Branch**: `003-algo-execution-platform` | **Date**: 2026-02-19 | **Spec**: [spec.md](spec.md)

## Summary

Upgrade the Portfolio Risk Management Dashboard from read-only to read-write by implementing: (1) a `BetaWeighter` class that correctly computes SPX Equivalent Delta using the dollar-beta formula, fixing three existing bugs in the adapter layer; (2) an execution engine with IBKR what-if margin simulation before any live order; (3) an SQLite trade journal capturing full fill context including pre/post Greeks and VIX; (4) an AI risk analyst extension to `llm_risk_auditor.py` that returns 3 structured trade suggestions on limit breach; (5) a 15-minute background snapshot logger with Plotly historical charts; and (6) a "Flatten Risk" panic button that generates buy-to-close orders for all short option legs with 2-step confirmation.

## Technical Context

**Language/Version**: Python 3.12 (existing project)
**Primary Dependencies**: `aiosqlite` (DB — already installed), `ib_insync` / IBKR CP REST (execution), `tastytrade` SDK v12+ (beta data), `yfinance` (beta fallback — add to requirements.txt), `streamlit` + `plotly` (dashboard — already installed), `agents/llm_client.py` (AI — already in project)
**Storage**: SQLite via `aiosqlite` — extends `database/local_store.py` with two new tables (`trade_journal`, `account_snapshots`)
**Testing**: `pytest` with `pytest-asyncio` — unit tests mock broker APIs; integration tests marked `@pytest.mark.integration`; Streamlit UI tested manually per Constitution §I
**Target Platform**: macOS desktop (developer's machine), single-user, local process
**Project Type**: Single project — extends existing `portfolioIBKR` monorepo
**Performance Goals**: Simulation response <5s; dashboard load <10s (live mode); Greeks computation <1s for 100 positions (Constitution performance standard)
**Constraints**: No external server or cloud service for journaling (local SQLite only); IBKR manual browser auth required (cannot be automated); Tastytrade SDK OAuth credentials in `.env`; zero live orders without explicit 2-step user confirmation
**Scale/Scope**: Single account, ~50–200 positions, ~35k snapshot rows/year, ~1k journal entries/year

## Constitution Check

_GATE: Must pass before Phase 0 research. Re-check after Phase 1 design._

| Gate                                                                           | Status | Notes                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------------------------------------------------------------------ | ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---- | ---------------------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **§I Test-First**: Unit tests for all business logic                           | PASS   | `BetaWeighter`, `ExecutionEngine`, `TradeJournal`, `OrderBuilder` all in `tests/test_*.py`. IBKR adapter tested via fixture mocks per Constitution §I IBKR Testing Strategy.                                                                                                                                                                                                     |
| **§I Coverage**: ≥80% for `risk_engine/`, `core/`, `models/`, `adapters/`      | PASS   | New modules `risk_engine/beta_weighter.py` and `core/execution.py` will have unit test files before merge. Dashboard components tested manually (Streamlit — Constitution §I Dashboard Testing).                                                                                                                                                                                 |
| **§II Adapter Pattern**: New broker calls via adapter                          | PASS   | `BetaWeighter` calls Tastytrade via adapter pattern (`adapters/tastytrade_adapter.py`). IBKR what-if calls go through a new `core/execution.py` that wraps `ibkr_gateway_client.py` — no raw HTTP in business logic.                                                                                                                                                             |
| **§III Taleb**: Gamma warning for 0–7 DTE, threshold=5.0                       | PASS   | Existing `regime_detector.py` check unchanged. New positions added to this feature (combo legs in OrderBuilder) pass through the same DTE-bucketing logic at fill time.                                                                                                                                                                                                          |
| **§III Sebastian**:                                                            | Theta  | /                                                                                                                                                                                                                                                                                                                                                                                | Vega | ratio target 0.25–0.40 | PASS | Delta/Theta chart required by spec is a new metric (Theta/Delta, not Theta/Vega) — additive, does not replace the existing Sebastian ratio visualization. Both charts present in the historical panel. |
| **§III Natenberg**: IV vs HV edge analysis                                     | N/A    | This feature does not modify or remove the existing IV/HV comparison; it is unaffected. Note: Constitution §III Natenberg refers to "US7" in the `001-portfolio-risk-manager` feature's story numbering (IV/HV edge analysis), which was delivered in that feature. This feature's US7 is the Flatten Risk button — a different story — and does not carry the IV/HV obligation. |
| **§IV Security**: No hardcoded credentials                                     | PASS   | `TT_SECRET`, `TT_REFRESH`, `IBKR_ACCOUNT_ID` via `.env`. `.env` already in `.gitignore`.                                                                                                                                                                                                                                                                                         |
| **§V Graceful Degradation**: Beta unavailable → default 1.0 + warning          | PASS   | `BetaWeighter` returns `beta_unavailable=True` flag per FR-003; dashboard shows ⚠ badge per spec.                                                                                                                                                                                                                                                                                |
| **§V Graceful Degradation**: IBKR unreachable → fallback + banner              | PASS   | `ExecutionEngine.simulate()` returns 503 Error with user-friendly message; submit button disabled.                                                                                                                                                                                                                                                                               |
| **§V Graceful Degradation**: AI unavailable → breach alert without suggestions | PASS   | `suggest_trades()` catches all exceptions and returns empty list (never raises); breach alert still shows per FR-023.                                                                                                                                                                                                                                                            |

**Post-Design Re-check**: No new violations introduced by Phase 1 design. SQLite `local_store.py` extension is additive — existing tables unchanged. `llm_risk_auditor.py` `suggest_trades()` is a new method — existing `audit()` method is unchanged.

## Project Structure

### Documentation (this feature)

```text
specs/003-algo-execution-platform/
├── plan.md                                   # This file
├── spec.md                                   # Feature specification
├── research.md                               # Phase 0: all research decisions
├── data-model.md                             # Phase 1: SQLite tables + Python dataclasses
├── quickstart.md                             # Phase 1: how to run + file map
├── contracts/
│   └── execution-platform-api.openapi.yaml  # Phase 1: internal module contracts
├── checklists/
│   └── requirements.md                      # Spec quality checklist
└── screenshots/
    ├── broker-ibkr-risk-navigator.png        # HIGH CHURN — replace when delta changes
    ├── broker-tastytrade-beta-delta.png      # HIGH CHURN — replace when delta changes
    ├── ui-order-builder.png                  # low churn
    ├── ui-journal-view.png                   # low churn
    ├── ui-historical-charts.png              # low churn
    ├── ui-ai-suggestions.png                 # low churn
    └── ui-flatten-risk-dialog.png            # low churn
```

### Source Code (repository root)

This feature extends the existing monorepo — no new top-level directories.

```text
# NEW files
risk_engine/
└── beta_weighter.py           # BetaWeighter class (Module 1 — FR-001–006)

core/
└── execution.py               # ExecutionEngine: simulate + submit + flatten (Module 2 — FR-007–013)

models/
└── order.py                   # Order, OrderLeg, SimulationResult, AITradeSuggestion, RiskBreach

dashboard/components/
├── order_builder.py           # Streamlit order builder + simulate + submit UI
├── trade_journal_view.py      # Journal display, filtering, CSV export
├── historical_charts.py       # Plotly Account Value vs Delta + Delta/Theta ratio charts
└── ai_suggestions.py          # AI suggestion cards, auto-fill OrderBuilder on click

tests/
├── test_beta_weighter.py      # Unit tests for BetaWeighter (mock Tastytrade + yfinance)
├── test_execution.py          # Unit tests for ExecutionEngine (mock IBKR gateway)
├── test_trade_journal.py      # Unit tests for journal write + query (in-memory SQLite)
├── test_order_builder.py      # Unit tests for Order dataclass validation
└── fixtures/
    └── sample_whatif_response.json   # Mock IBKR whatif API response

# MODIFIED files
adapters/ibkr_adapter.py       # Fix: pass underlying price (not strike) to spx_weighted_delta
beta_config.json               # Fix: "MES" → 1.0
agents/llm_risk_auditor.py     # Extend: add suggest_trades() method
database/local_store.py        # Extend: add trade_journal + account_snapshots tables
models/unified_position.py     # Extend: add BetaWeightedPosition dataclass
dashboard/app.py               # Extend: wire new panels + start snapshot background task
requirements.txt               # Add: yfinance
```

**Structure Decision**: Single project, extending existing monorepo. No new top-level directories. New Python modules in existing packages (`risk_engine/`, `core/`, `models/`, `dashboard/components/`). Dashboard components extracted per-panel into `dashboard/components/` (Constitution §II Dashboard Architecture note: "future refactor: extract to components/\*.py if complexity grows" — complexity now justifies this, given 4 independent new panels).
