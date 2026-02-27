# Tasks: IBKR Trading Bridge

**Input**: Design documents from `/specs/005-ibkr-trading-bridge/`
**Prerequisites**: `plan.md` (required), `spec.md` (required for user stories), `research.md`, `data-model.md`, `contracts/`

**Tests**: Test tasks are included because the feature spec explicitly requires unit tests in `tests/test_ib_bridge.py`.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., `US1`, `US2`, `US3`)
- Every task includes an exact file path

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create bridge module skeleton and baseline configuration for implementation.

- [X] T001 Create bridge package initializer in `bridge/__init__.py`
- [X] T002 Add bridge runtime configuration block to `.env.example` for `IB_API_MODE`, socket, portal, and polling settings in `.env.example`
- [X] T003 [P] Add bridge dependency notes for `ib_async`, `aiohttp`, and DB requirements in `requirements.txt`
- [X] T004 [P] Create test module scaffold for bridge behavior in `tests/test_ib_bridge.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Implement shared primitives required by all user stories.

**⚠️ CRITICAL**: No user story work should begin until this phase is complete.

- [X] T005 Implement bridge mode constants and validation helper (`SOCKET|PORTAL`) in `bridge/ib_bridge.py`
- [X] T006 Implement shared `IBridgeBase` protocol/ABC contract in `bridge/ib_bridge.py`
- [X] T007 [P] Implement bridge DB manager shell with async function signatures in `bridge/database_manager.py`
- [X] T008 [P] Implement structured bridge logger factory and common log format in `bridge/main.py`
- [X] T009 Implement startup wiring between config, bridge selection, and DB manager in `bridge/main.py`

**Checkpoint**: Foundation ready — user story implementation can proceed.

---

## Phase 3: User Story 1 - Connection Toggle (Priority: P1) 🎯 MVP

**Goal**: Toggle between SOCKET and PORTAL modes using `.env` only, with startup failure on invalid mode.

**Independent Test**: Running bridge startup with `IB_API_MODE=SOCKET` and `IB_API_MODE=PORTAL` selects the correct implementation; invalid mode raises `ValueError` immediately.

### Tests for User Story 1

- [X] T010 [US1] Add test for valid `SOCKET` mode selection in `tests/test_ib_bridge.py`
- [X] T011 [US1] Add test for valid `PORTAL` mode selection in `tests/test_ib_bridge.py`
- [X] T012 [US1] Add test asserting invalid `IB_API_MODE` raises `ValueError` in `tests/test_ib_bridge.py`

### Implementation for User Story 1

- [X] T013 [US1] Implement `SocketBridge` connection lifecycle (`connect`, `disconnect`, `is_connected`) in `bridge/ib_bridge.py`
- [X] T014 [US1] Implement `PortalBridge` connection/session lifecycle (`connect`, `disconnect`, `is_connected`) in `bridge/ib_bridge.py`
- [X] T015 [US1] Implement `build_bridge_from_env()` factory using `IB_API_MODE` in `bridge/ib_bridge.py`
- [X] T016 [US1] Wire mode-validation startup path to fail-fast before loop start in `bridge/main.py`
- [X] T017 [US1] Log connect/disconnect/error lifecycle events for both modes in `bridge/database_manager.py`

**Checkpoint**: Mode toggle works independently and is testable via startup configuration only.

---

## Phase 4: User Story 2 - Schema Bootstrap (Priority: P1)

**Goal**: Ensure `portfolio_greeks` and `api_logs` tables exist at startup with idempotent behavior.

**Independent Test**: Running startup twice creates schema on first run and no-ops on second run without errors.

### Tests for User Story 2

- [X] T018 [US2] Add test that `ensure_bridge_schema()` creates both tables in `tests/test_ib_bridge.py`
- [X] T019 [US2] Add test that `ensure_bridge_schema()` is idempotent in `tests/test_ib_bridge.py`

### Implementation for User Story 2

- [X] T020 [US2] Implement DDL for `portfolio_greeks` plus timestamp index in `bridge/database_manager.py`
- [X] T021 [US2] Implement DDL for `api_logs` plus timestamp index in `bridge/database_manager.py`
- [X] T022 [US2] Implement `ensure_bridge_schema()` orchestration for both tables in `bridge/database_manager.py`
- [X] T023 [US2] Call `ensure_bridge_schema()` during startup before bridge polling starts in `bridge/main.py`

**Checkpoint**: Schema bootstrap is complete and independently verifiable.

---

## Phase 5: User Story 3 - 5-Second Greeks Loop (Priority: P1)

**Goal**: Persist one portfolio aggregate row every 5 seconds into `portfolio_greeks`.

**Independent Test**: With bridge running, two consecutive snapshots are written approximately 5 seconds apart and contain required fields.

### Tests for User Story 3

- [X] T024 [US3] Add test that `get_portfolio_greeks()` returns required payload keys in `tests/test_ib_bridge.py`
- [X] T025 [US3] Add test that polling loop schedules snapshots at configured interval in `tests/test_ib_bridge.py`
- [X] T026 [US3] Add test that `write_portfolio_snapshot()` persists contract `PORTFOLIO` rows in `tests/test_ib_bridge.py`

### Implementation for User Story 3

- [X] T027 [US3] Implement SOCKET-mode portfolio Greeks aggregation (options via modelGreeks, non-options defaults) in `bridge/ib_bridge.py`
- [X] T028 [US3] Implement PORTAL-mode Greeks snapshot fetch and aggregate mapping in `bridge/ib_bridge.py`
- [X] T029 [US3] Implement `write_portfolio_snapshot()` with required row schema in `bridge/database_manager.py`
- [X] T030 [US3] Implement async 5-second polling loop with drift-aware timing in `bridge/main.py`
- [X] T031 [US3] Bind loop to `BRIDGE_POLL_INTERVAL` env var defaulting to 5 in `bridge/main.py`

**Checkpoint**: 5-second snapshot writing works independently.

---

## Phase 6: User Story 4 - Circuit Breaker Buffering (Priority: P1)

**Goal**: On DB failure, buffer rows to JSONL and automatically flush every 60 seconds with ordering preserved.

**Independent Test**: Simulated DB outage buffers rows to file; recovery flushes buffered rows in order and resumes normal writes.

### Tests for User Story 4

- [X] T032 [US4] Add test that DB write failures are buffered to JSONL path in `tests/test_ib_bridge.py`
- [X] T033 [US4] Add test that buffered rows flush in-order after DB recovery in `tests/test_ib_bridge.py`
- [X] T034 [US4] Add test that flush loop uses 60-second retry cadence in `tests/test_ib_bridge.py`

### Implementation for User Story 4

- [X] T035 [US4] Integrate `DBCircuitBreaker` for snapshot writes in `bridge/database_manager.py`
- [X] T036 [US4] Integrate `DBCircuitBreaker` for lifecycle log writes in `bridge/database_manager.py`
- [X] T037 [US4] Wire `BRIDGE_BUFFER_PATH` and flush task startup in DB manager bootstrap in `bridge/database_manager.py`
- [X] T038 [US4] Start and manage background buffer flush loop in service runtime lifecycle in `bridge/main.py`

**Checkpoint**: Outage buffering and recovery are independently testable.

---

## Phase 7: User Story 5 - SOCKET Watchdog (Priority: P2)

**Goal**: Detect SOCKET disconnects, apply ET reset-window hold, otherwise reconnect with 5/10/20 second backoff and log each attempt.

**Independent Test**: Simulated disconnect during reset window delays reconnect until 00:05 ET; non-window disconnect uses 5/10/20 retry sequence and logs attempts.

### Tests for User Story 5

- [X] T039 [US5] Add test for reset-window detection (`23:40–23:59 ET`) in `tests/test_ib_bridge.py`
- [X] T040 [US5] Add test for wait-until-00:05 ET calculation in `tests/test_ib_bridge.py`
- [X] T041 [US5] Add test for reconnect backoff sequence `5,10,20` in `tests/test_ib_bridge.py`
- [X] T042 [US5] Add test that each reconnect attempt is logged to `api_logs` in `tests/test_ib_bridge.py`

### Implementation for User Story 5

- [X] T043 [US5] Implement ET timezone watchdog helpers using `zoneinfo` in `bridge/ib_bridge.py`
- [X] T044 [US5] Implement SOCKET reconnect loop with 5/10/20 backoff in `bridge/ib_bridge.py`
- [X] T045 [US5] Implement reset-window hold-until-00:05 behavior in `bridge/ib_bridge.py`
- [X] T046 [US5] Emit watchdog reconnect lifecycle events through DB log writer in `bridge/main.py`

**Checkpoint**: Watchdog reconnect logic is independently testable and complete.

---

## Final Phase: Polish & Cross-Cutting Concerns

**Purpose**: Final hardening, docs, and verification across stories.

- [X] T047 [P] Update operational runbook and env docs for bridge startup/recovery in `specs/005-ibkr-trading-bridge/quickstart.md`
- [X] T048 [P] Align contract examples and payload fields with final implementation in `specs/005-ibkr-trading-bridge/contracts/bridge_interface.md`
- [X] T049 Execute full bridge test module and capture final pass status in `tests/test_ib_bridge.py`
- [X] T050 Run feature quickstart validation commands and record outcomes in `specs/005-ibkr-trading-bridge/quickstart.md`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies.
- **Phase 2 (Foundational)**: Depends on Phase 1; blocks all user stories.
- **Phase 3–7 (User Stories)**: Depend on Phase 2 completion.
- **Final Phase (Polish)**: Depends on all targeted user stories.

### User Story Dependencies

- **US1 (P1)**: Starts after Foundational; no dependency on other stories.
- **US2 (P1)**: Starts after Foundational; independent from US1 implementation details.
- **US3 (P1)**: Starts after Foundational; uses schema from US2 at runtime.
- **US4 (P1)**: Starts after Foundational; integrates with US3 writes.
- **US5 (P2)**: Starts after Foundational; depends on US1 SOCKET bridge lifecycle.

### Recommended Completion Order

1. US1 → mode correctness and startup validation
2. US2 → schema safety/idempotency
3. US3 → core 5-second value delivery (MVP)
4. US4 → resilience under DB failures
5. US5 → SOCKET operational robustness

---

## Parallel Execution Examples

### US1 Parallel Example

```bash
# Parallelizable after test stubs are in place:
Task: T013 in bridge/ib_bridge.py
Task: T014 in bridge/ib_bridge.py
```

### US2 Parallel Example

```bash
# Parallelizable DDL implementation tasks:
Task: T020 in bridge/database_manager.py
Task: T021 in bridge/database_manager.py
```

### US3 Parallel Example

```bash
# Parallelizable bridge mode aggregators:
Task: T027 in bridge/ib_bridge.py
Task: T028 in bridge/ib_bridge.py
```

### US4 Parallel Example

```bash
# Parallelizable DB manager integrations:
Task: T035 in bridge/database_manager.py
Task: T036 in bridge/database_manager.py
```

### US5 Parallel Example

```bash
# Parallelizable helper and backoff implementations:
Task: T043 in bridge/ib_bridge.py
Task: T044 in bridge/ib_bridge.py
```

---

## Implementation Strategy

### MVP First (Recommended Scope)

1. Complete Phase 1 and Phase 2.
2. Complete US1, US2, and US3.
3. Validate independent test criteria for US3 (core business value).
4. Demo/deploy MVP.

### Incremental Delivery

1. Add US4 for DB outage resilience.
2. Add US5 for SOCKET watchdog reliability.
3. Finish polish tasks and re-run quickstart validation.

### Team Parallelization

1. One developer on DB manager tasks (`bridge/database_manager.py`).
2. One developer on bridge mode/watchdog tasks (`bridge/ib_bridge.py`).
3. One developer on runtime wiring/tests (`bridge/main.py`, `tests/test_ib_bridge.py`).
