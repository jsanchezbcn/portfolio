# Implementation Plan: Trade Proposer Agent

**Branch**: `006-trade-proposer` | **Date**: 2026-02-23 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/006-trade-proposer/spec.md`

## Summary

Build an autonomous agent that monitors portfolio Greeks every 5 minutes. Upon detecting a risk breach (based on `risk_matrix.yaml`), it generates candidate hedges using liquid benchmarks (SPX, SPY, /ES), simulates their margin impact via IBKR's What-If API, and persists the most efficient trades to PostgreSQL. Following **Option C**, notifications are only sent when actionable solutions are found.

## Technical Context

**Language/Version**: Python 3.13  
**Primary Dependencies**: `ib_async`, `aiohttp`, `SQLAlchemy/SQLModel`, `NotificationDispatcher`  
**Storage**: PostgreSQL (Table: `proposed_trades`)  
**Testing**: `pytest` with `IBKRAdapter` mocking and snapshot fixtures.  
**Target Platform**: macOS/Linux  
**Project Type**: Python Worker/Agent  
**Performance Goals**: < 15s for 5-candidate simulation; 5-minute monitoring frequency.  
**Constraints**: Benchmarks limited to SPX, SPY, /ES. Must handle "Supersede" logic for stale proposals.

## Constitution Check

_GATE: Must pass before Phase 0 research. Re-check after Phase 1 design._

1. **Test-First**: PROPOSED. Business logic for `EfficiencyScore` and `ProposerLoop` will have 80%+ coverage.
2. **IBKR Strategy**: PROPOSED. Will use `.portfolio_snapshot.json` for detection tests and mock `simulate_margin` for generation tests.
3. **Adapter Pattern**: PROPOSED. `IBKRAdapter` will be extended with the simulation interface, maintaining `BrokerAdapter` compatibility.
4. **Trading Literature**: PROPOSED. Solutions will be ranked by efficiency and weighted by Sebastian's Theta/Vega ratio targets.

## Project Structure

### Documentation (this feature)

```text
specs/006-trade-proposer/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
└── tasks.md             # Phase 2 output
```

### Source Code

```text
agents/
├── trade_proposer.py    # Main agent logic and monitoring loop
├── proposer_engine.py   # Strategy generation and ranking engine

adapters/
├── ibkr_adapter.py      # Extended with what-if simulation hooks

models/
├── proposed_trade.py    # SQLModel entity for database persistence

tests/
├── test_trade_proposer.py
├── test_proposer_engine.py
```

**Structure Decision**: Single project integration. Adding the agent to the `agents/` directory and extending the `adapters/` to support simulation.

## Complexity Tracking

| Violation              | Why Needed                                      | Simpler Alternative Rejected Because                    |
| ---------------------- | ----------------------------------------------- | ------------------------------------------------------- |
| Extended `IBKRAdapter` | Must decouple simulation logic from agent loop  | Direct API calls in agent would violate Adapter pattern |
| "Supersede" Logic      | Prevents executing stale trades in fast markets | Deleting old trades would lose audit trail              |

## Final Status

- Phase 0: Research completed (`research.md`).
- Phase 1: Design artifacts generated (`data-model.md`, `quickstart.md`, `contracts/`).
- Constitution Check: Passed.
- Notification Strategy: **Option C** (Solution-based alerts) selected.
