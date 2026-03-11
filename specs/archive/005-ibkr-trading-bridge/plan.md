# Implementation Plan: IBKR Trading Bridge

**Branch**: `005-ibkr-trading-bridge` | **Date**: 2026-02-22 | **Spec**: `/specs/005-ibkr-trading-bridge/spec.md`
**Input**: Feature specification from `/specs/005-ibkr-trading-bridge/spec.md`

**Note**: This template is filled in by the `/speckit.plan` command. See `.specify/templates/commands/plan.md` for the execution workflow.

## Summary

Build a standalone `bridge/` service that supports `IB_API_MODE=SOCKET|PORTAL`, polls net portfolio Greeks every 5 seconds, persists snapshots and API lifecycle logs to PostgreSQL, and survives DB outages via JSONL buffering with automatic replay. Implement mode-specific bridge clients (`ib_async` for SOCKET, `aiohttp` for PORTAL), schema bootstrap on startup, and an ET-aware watchdog for IBKR daily reset handling in SOCKET mode.

## Technical Context

<!--
  ACTION REQUIRED: Replace the content in this section with the technical details
  for the project. The structure here is presented in advisory capacity to guide
  the iteration process.
-->

**Language/Version**: Python 3.13.x  
**Primary Dependencies**: `ib_async==2.1.0`, `aiohttp>=3.9.0`, `psycopg2-binary`, existing `database.circuit_breaker.DBCircuitBreaker`  
**Storage**: PostgreSQL tables (`portfolio_greeks`, `api_logs`) + local JSONL buffer file (`~/.portfolio_bridge_buffer.jsonl`)  
**Testing**: `pytest` with unit tests and mocked IB/DB dependencies  
**Target Platform**: macOS/Linux runtime with IB Gateway/TWS connectivity and PostgreSQL access
**Project Type**: Single Python service module (`bridge/`) integrated into existing repository  
**Performance Goals**: Persist one portfolio snapshot every 5s; buffer flush retry every 60s; reconnect backoff at 5/10/20s  
**Constraints**: Must not modify `database/db_manager.py`; must preserve iPad session isolation (`clientId=10` for bridge, iPad on `0`); no hardcoded credentials; graceful degradation required  
**Scale/Scope**: Single account to small multi-account IBKR portfolios; long-running daemon process with continuous polling

## Constitution Check

_GATE: Must pass before Phase 0 research. Re-check after Phase 1 design._

- **Test-First Development**: PASS. Plan includes `tests/test_ib_bridge.py` unit coverage for bridge logic, DB write fallback, watchdog behavior, and mode toggle validation.
- **Adapter Pattern Enforcement**: PASS. Existing adapters remain untouched; `bridge/` is a standalone integration surface and does not violate adapter contracts.
- **Security Requirements**: PASS. All credentials/config from environment variables; no secrets in code; account identifiers only in logs where already permitted.
- **Graceful Degradation**: PASS. Circuit breaker buffering and replay explicitly implemented for DB outages; API errors logged and handled without process crash.
- **Performance/Data Standards**: PASS with scope note. 5-second polling and deterministic write cadence are explicit; Greeks source is bridge-level portfolio net values.

**Post-Design Re-check (after Phase 1 artifacts)**: PASS. Data model, contracts, and quickstart preserve all constitutional constraints and do not introduce violations requiring exceptions.

## Project Structure

### Documentation (this feature)

```text
specs/005-ibkr-trading-bridge/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
bridge/
├── __init__.py
├── database_manager.py
├── ib_bridge.py
└── main.py

database/
└── circuit_breaker.py

specs/005-ibkr-trading-bridge/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
└── contracts/

tests/
└── test_ib_bridge.py
```

**Structure Decision**: Single Python project structure using existing repository layout, adding a focused `bridge/` module and feature-local spec artifacts under `specs/005-ibkr-trading-bridge/`.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation                  | Why Needed         | Simpler Alternative Rejected Because |
| -------------------------- | ------------------ | ------------------------------------ |
| [e.g., 4th project]        | [current need]     | [why 3 projects insufficient]        |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient]  |
