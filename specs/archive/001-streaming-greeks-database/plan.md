# Implementation Plan: Streaming Greeks and Database

**Branch**: `001-streaming-greeks-database` | **Date**: 2026-02-14 | **Spec**: `/specs/001-streaming-greeks-database/spec.md`
**Input**: Feature specification from `/specs/001-streaming-greeks-database/spec.md`

**Note**: This template is filled in by the `/speckit.plan` command. See `.specify/templates/commands/plan.md` for the execution workflow.

## Summary

Replace polling-based Greeks collection with a dual-streaming ingestion engine (IBKR WebSocket + Tastytrade DXLink), normalize all inbound events to the UnifiedPosition-aligned shape, and persist high-frequency snapshots into PostgreSQL via asyncpg batching that satisfies sub-500ms tick-to-write latency goals.

## Technical Context

**Language/Version**: Python 3.11+ (asyncio runtime)  
**Primary Dependencies**: `asyncpg`, `websockets`, `tastytrade`, `python-dotenv`, existing internal adapters/models  
**Storage**: PostgreSQL 16 (local instance; DB name `portfolio_engine`)  
**Testing**: `pytest` unit tests + focused integration tests for DB and stream adapters  
**Target Platform**: macOS/Linux local runtime for dashboard and ingestion services  
**Project Type**: Single Python project (monorepo layout)  
**Performance Goals**: <500ms tick-to-durable-write latency for >=95% ticks; high-frequency write throughput for active options streams  
**Constraints**: Must use `asyncpg` for DB interactions; must use `websockets` for IBKR; heartbeat every 60s for IBKR; empty DB bootstrap supported  
**Scale/Scope**: Current portfolio scale (tens of positions, bursty intra-second ticks), extensible to hundreds of contracts across two broker streams

## Constitution Check

_GATE: Must pass before Phase 0 research. Re-check after Phase 1 design._

**Pre-Phase-0 Gate Review**

- **Test-First Development (PASS)**: Plan includes unit tests for processor mapping, DB batching logic, and streamer message handling; integration tests marked separately where credentials/network are required.
- **Adapter Pattern (PASS)**: Existing adapter abstraction remains intact; streaming ingestion is additive and maps payloads into UnifiedPosition-compatible records before persistence.
- **Trading Literature Principles (PASS)**: No changes to strategy math semantics; persisted Greeks enhance timeliness but do not alter Taleb/Natenberg/Sebastian rules.
- **Security Requirements (PASS)**: Credentials remain env-driven via `.env`; no secrets in source; DB and broker auth parameters loaded from environment.
- **Graceful Degradation (PASS)**: Design includes independent stream failure isolation, reconnect strategy, and safe handling of partial payloads.

**Gate Result**: PASS — no constitutional violations requiring exception tracking.

## Project Structure

### Documentation (this feature)

```text
specs/001-streaming-greeks-database/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
adapters/
agent_tools/
dashboard/
models/
risk_engine/
tests/
├── integration/
└── test_*.py

# New feature modules
database/
└── db_manager.py

streaming/
├── ibkr_ws.py
└── tasty_dxlink.py

core/
└── processor.py
```

**Structure Decision**: Use the existing single-project Python repository and add focused modules (`database/`, `streaming/`, `core/`) to avoid cross-cutting refactors and preserve current adapter/dashboard boundaries.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
| --------- | ---------- | ------------------------------------ |
| None      | N/A        | N/A                                  |

## Phase 0 Research Output

See `/specs/001-streaming-greeks-database/research.md`.

## Phase 1 Design Output

- Data model: `/specs/001-streaming-greeks-database/data-model.md`
- Contracts: `/specs/001-streaming-greeks-database/contracts/streaming-greeks-api.openapi.yaml`
- Quickstart: `/specs/001-streaming-greeks-database/quickstart.md`

## Post-Design Constitution Check

- **Test-First Development (PASS)**: Design artifacts define testable unit/integration slices for DB batching, stream parsing, reconnect, and normalization.
- **Adapter Pattern (PASS)**: Normalization remains centered on UnifiedPosition semantics and does not bypass existing broker abstraction boundaries.
- **Security Requirements (PASS)**: Contracts/quickstart use env vars only; no credential material captured.
- **Graceful Degradation (PASS)**: Data model includes stream session state and failure isolation, supporting degraded single-broker operation.

**Final Gate Result**: PASS
