# Tasks: Streaming Greeks and Database

**Input**: Design documents from `/specs/001-streaming-greeks-database/`  
**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Add dependencies and create module scaffolding for streaming + persistence.

- [x] T001 Add required dependencies (`asyncpg`, `websockets`, `tastytrade`, `python-dotenv`) in `./requirements.txt`
- [x] T002 Create module directories and package initializers in `database/__init__.py`, `streaming/__init__.py`, `core/__init__.py`
- [x] T003 [P] Add streaming/database environment variable loading helpers in `agent_config.py`
- [x] T004 [P] Add startup logging channels for stream + DB ingestion in `./logging_config.py`
- [x] T005 [P] Add foundational unit test scaffolding for DB manager and processor in `tests/test_db_manager.py`, `tests/test_stream_processor.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Implement core components that all user stories depend on.

**⚠️ CRITICAL**: No user story work starts before this phase is complete.

- [x] T006 Implement asyncpg pool singleton and lifecycle methods in `database/db_manager.py`
- [x] T007 Implement schema bootstrap for `trades` and partitioned `greek_snapshots` tables in `database/db_manager.py`
- [x] T008 Implement buffered batch flush (`1s` or `50` records) using `executemany` in `database/db_manager.py`
- [x] T009 [P] Implement normalized DTO/dataclass for persisted snapshot rows in `core/processor.py`
- [x] T010 Implement source-agnostic enqueue and backpressure-safe buffer interface in `core/processor.py`
- [x] T011 [P] Implement latency measurement helper (`received_at` to `persisted_at`) in `core/processor.py`
- [x] T012 Implement stream supervisor skeleton with independent task isolation in `core/processor.py`
- [x] T013 Wire DB and processor initialization into runtime entrypoint in `./ibkr_portfolio_client.py`
- [x] T014 Add adapter-bridge task to keep stream normalization compliant with `BrokerAdapter` outputs in `adapters/ibkr_adapter.py`, `adapters/tastytrade_adapter.py`

**Checkpoint**: Foundation ready. User stories can now be implemented independently.

---

## Phase 3: User Story 1 - Real-time IBKR Greeks Persistence (Priority: P1) 🎯 MVP

**Goal**: Stream IBKR Greeks via WebSocket with heartbeat and persist normalized snapshots in real time.

**Independent Test**: Run IBKR stream only, verify heartbeat at 60s, and confirm rows written to `greek_snapshots` with p95 latency under 500ms.

### Implementation for User Story 1

- [x] T015 [P] [US1] Add failing unit tests for IBKR websocket heartbeat/reconnect behavior in `tests/test_ibkr_streaming.py`
- [x] T016 [US1] Implement IBKR websocket client connect/handshake with TLS handling in `streaming/ibkr_ws.py`
- [x] T017 [US1] Implement IBKR message loop and routing callback into processor in `streaming/ibkr_ws.py`
- [x] T018 [US1] Implement 60-second heartbeat keepalive logic in `streaming/ibkr_ws.py`
- [x] T019 [US1] Implement active-option subscription payload builder for IBKR contracts in `streaming/ibkr_ws.py`
- [x] T020 [US1] Implement IBKR reconnect strategy with bounded backoff in `streaming/ibkr_ws.py`
- [x] T021 [US1] Add IBKR stream lifecycle control wiring in `./ibkr_gateway_client.py`
- [x] T022 [US1] Add IBKR-only ingestion startup path for independent execution in `./debug_greeks_cli.py`

**Checkpoint**: US1 is independently functional and demoable.

---

## Phase 4: User Story 2 - Real-time Tastytrade Greeks Persistence (Priority: P2)

**Goal**: Stream Tastytrade Greeks for all open positions and persist them continuously.

**Independent Test**: Run Tastytrade stream only, subscribe all open positions, and verify continuous writes to `greek_snapshots`.

### Implementation for User Story 2

- [x] T023 [P] [US2] Add failing unit tests for DXLink subscription/recovery behavior in `tests/test_tasty_dxlink_streaming.py`
- [x] T024 [US2] Implement DXLink streamer wrapper and authentication initialization in `streaming/tasty_dxlink.py`
- [x] T025 [US2] Implement open-position discovery and subscription mapping in `streaming/tasty_dxlink.py`
- [x] T026 [US2] Implement Tastytrade message loop and routing callback into processor in `streaming/tasty_dxlink.py`
- [x] T027 [US2] Implement Tastytrade reconnect/recovery behavior with stream isolation in `streaming/tasty_dxlink.py`
- [x] T028 [US2] Add Tastytrade stream lifecycle wiring in `./tastytrade_sdk_options_fetcher.py`
- [x] T029 [US2] Add Tastytrade-only ingestion startup path for independent execution in `./debug_greeks_cli.py`

**Checkpoint**: US2 is independently functional and demoable.

---

## Phase 5: User Story 3 - Unified Stored Risk Timeline (Priority: P3)

**Goal**: Normalize both brokers into one UnifiedPosition-compatible persistence schema and expose query/control operations.

**Independent Test**: Replay mixed IBKR + Tastytrade updates and verify unified query results with required fields and consistent contract identity.

### Implementation for User Story 3

- [x] T030 [P] [US3] Add failing unit tests for normalization/schema completeness in `tests/test_stream_processor.py`
- [x] T031 [US3] Implement IBKR payload-to-unified normalization mapping in `core/processor.py`
- [x] T032 [US3] Implement Tastytrade payload-to-unified normalization mapping in `core/processor.py`
- [x] T033 [US3] Implement dedupe/out-of-order handling policy during snapshot enqueue in `core/processor.py`
- [x] T034 [US3] Implement snapshot query service for broker/account/contract/time filters in `agent_tools/portfolio_tools.py`
- [x] T035 [US3] Implement trade logging write path to `trades` table in `database/db_manager.py`
- [x] T036 [US3] Implement runtime stream status exposure (`connected/degraded/disconnected`) in `core/processor.py`
- [x] T037 [US3] Add API handlers for `/v1/streaming/start`, `/v1/streaming/stop`, `/v1/streaming/status`, `/v1/greeks/snapshots`, `/v1/trades` in `agent_tools/portfolio_tools.py`

**Checkpoint**: US3 is independently functional and demoable.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Final hardening, docs, and operational validation across all stories.

- [x] T038 [P] Add operational runbook updates for streaming startup/recovery in `docs/DEMO_RUNBOOK.md`
- [x] T039 [P] Add quick diagnostics commands for DB/stream health in `specs/001-streaming-greeks-database/quickstart.md`
- [x] T040 Validate end-to-end latency SLO logging output and threshold alerts in `core/processor.py`
- [x] T041 Add measurable SC-001 persistence-rate validation task in `tests/integration/test_streaming_persistence_integration.py`
- [x] T042 Add measurable SC-004 outage-isolation timing validation task in `tests/integration/test_streaming_failover_integration.py`
- [x] T043 Add measurable SC-005 schema-completeness validation task in `tests/integration/test_streaming_schema_integration.py`
- [x] T044 Run full quickstart scenario and capture verification notes in `specs/001-streaming-greeks-database/quickstart.md`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: Starts immediately.
- **Phase 2 (Foundational)**: Depends on Phase 1; blocks all user stories.
- **Phase 3 (US1)**: Depends on Phase 2.
- **Phase 4 (US2)**: Depends on Phase 2.
- **Phase 5 (US3)**: Depends on Phase 2 and uses outputs from US1/US2 stream payload formats.
- **Phase 6 (Polish)**: Depends on completion of selected user stories.

### User Story Dependencies

- **US1 (P1)**: No dependency on US2/US3 after foundational phase.
- **US2 (P2)**: No dependency on US1/US3 after foundational phase.
- **US3 (P3)**: Depends on having both source mappings available (US1 + US2), but can begin with stub payload fixtures and finalize after both streams land.

### Task-Level Parallel Opportunities

- **Setup**: `T003`, `T004` can run in parallel with dependency install/module creation completion.
- **Foundational**: `T008` and `T010` run in parallel once `T005` starts.
- **Story parallelism**: US1 and US2 can be developed concurrently by separate developers after Phase 2.

---

## Parallel Execution Examples

### User Story 1

```bash
# Parallelizable US1 work after T013 is complete:
T015 Implement 60-second heartbeat keepalive logic in streaming/ibkr_ws.py
T016 Implement active-option subscription payload builder in streaming/ibkr_ws.py
T017 Implement IBKR reconnect strategy in streaming/ibkr_ws.py
```

### User Story 2

```bash
# Parallelizable US2 work after T020 is complete:
T021 Implement open-position subscription mapping in streaming/tasty_dxlink.py
T022 Implement Tastytrade message loop routing in streaming/tasty_dxlink.py
T023 Implement Tastytrade reconnect/recovery logic in streaming/tasty_dxlink.py
```

### User Story 3

```bash
# Parallelizable US3 work after processor interfaces are stable:
T029 Implement snapshot query service in agent_tools/portfolio_tools.py
T030 Implement trade logging write path in database/db_manager.py
T031 Implement stream status exposure in core/processor.py
```

---

## Implementation Strategy

### MVP First (US1)

1. Complete Phase 1 and Phase 2.
2. Complete Phase 3 (US1).
3. Validate IBKR heartbeat + persistence + latency target.
4. Demo/deploy MVP scope.

### Incremental Delivery

1. Add US2 for cross-broker completeness.
2. Add US3 for unified normalization + query/control surfaces.
3. Execute polish and operational validation.

### Team Parallel Plan

1. One developer completes Foundation tasks.
2. Then split:
   - Dev A: US1 (IBKR stream)
   - Dev B: US2 (Tastytrade stream)
   - Dev C: US3 (normalization/query API)
