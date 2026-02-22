---
description: "Task list for 004-risk-managed-edge-portfolio"
---

# Tasks: Risk-Managed Edge Portfolio Management

**Input**: Design documents from `specs/004-risk-managed-edge-portfolio/`
**Prerequisites**: plan.md

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [x] T001 Clean up the `specs/archive/` specs and consolidate the vision into this single source of truth in `specs/004-risk-managed-edge-portfolio/plan.md`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [x] T002 Setup centralized event bus (e.g., PostgreSQL LISTEN/NOTIFY or Redis) in `core/event_bus.py`
- [x] T003 Refactor `core/` modules to use the centralized event bus for market data and order updates

**Checkpoint**: Foundation ready - user story implementation can now begin in parallel

---

## Phase 3: User Story 1 - Core Infrastructure Simplification (Priority: P1) 🎯 MVP

**Goal**: Decouple UI from Execution. The Streamlit dashboard should be purely a view layer. All order management, risk calculations, and data fetching should happen in backend workers/agents.

**Independent Test**: Verify that the Streamlit dashboard loads and displays data without making any direct API calls to brokers or blocking on data fetching.

### Implementation for User Story 1

- [x] T004 [US1] Move all Streamlit data fetching to background workers in `workers/portfolio_worker.py`
- [x] T005 [US1] Update `dashboard/app.py` to only read from the database (`worker_jobs` table)

**Checkpoint**: At this point, User Story 1 should be fully functional and testable independently

---

## Phase 4: User Story 2 - Real-Time Execution Engine (Priority: P2)

**Goal**: Move from a read-only/simulated environment to robust real order management with real-time data.

**Independent Test**: Verify that real-time price and Greeks updates are streaming via WebSockets and that orders pass through the state machine and pre-trade risk check.

### Implementation for User Story 2

- [x] T006 [P] [US2] Implement the Order State Machine (PENDING, SUBMITTED, PARTIAL_FILL, FILLED, CANCELED, REJECTED) in `core/order_manager.py`
- [x] T007 [P] [US2] Connect IBKR/Tastytrade WebSockets for real-time data streaming in `adapters/ibkr_adapter.py` and `adapters/tastytrade_adapter.py`
- [x] T008 [US2] Build the pre-trade risk simulation gate in `core/execution.py` (depends on T006)

**Checkpoint**: At this point, User Stories 1 AND 2 should both work independently

---

## Phase 5: User Story 3 - Agent Specialization & Skills Integration (Priority: P3)

**Goal**: Deploy specialized, single-purpose agents instead of monolithic scripts.

**Independent Test**: Verify that each agent (Risk, Allocation, Execution, Market Intelligence) can run independently and communicate via the event bus.

### Implementation for User Story 3

- [x] T009 [P] [US3] Install open-source agent skills: run `npx skills add 0xhubed/agent-trading-arena@risk-management` and `npx skills add wshobson/agents@risk-metrics-calculation`
- [x] T010 [P] [US3] Create Risk Management Agent in `agents/risk_manager.py` to monitor portfolio Greeks against limits and propose hedging orders
- [x] T011 [P] [US3] Create Capital Allocation Agent in `agents/capital_allocator.py` to determine optimal position size
- [x] T012 [P] [US3] Create Execution Agent in `agents/execution_agent.py` to minimize slippage and manage broker API interactions
- [x] T013 [P] [US3] Create Market Intelligence Agent in `agents/market_intelligence.py` to analyze news and adjust risk regime

**Checkpoint**: All user stories should now be independently functional

---

## Phase 6: User Story 4 - UI/UX Overhaul (Priority: P4)

**Goal**: Redesign the Streamlit dashboard for a "Risk First" view, highlighting margin usage, SPX Beta-Weighted Delta, and Vega exposure.

**Independent Test**: Verify the new dashboard layout correctly displays the "Greek Speedometer" and scenario analysis.

### Implementation for User Story 4

- [x] T014 [P] [US4] Install UI/UX skills: run `npx skills add vercel-labs/agent-skills@web-design-guidelines` and `npx skills add nextlevelbuilder/ui-ux-pro-max-skill@ui-ux-pro-max`
- [x] T015 [US4] Redesign the Streamlit dashboard in `dashboard/app.py` highlighting margin usage, SPX Beta-Weighted Delta, and Vega exposure

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories

- [x] T016 [P] Documentation updates in `docs/`
- [x] T017 Code cleanup and refactoring
- [x] T018 Performance optimization across all stories

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **User Stories (Phase 3+)**: All depend on Foundational phase completion
  - User stories can then proceed in parallel (if staffed)
  - Or sequentially in priority order (P1 → P2 → P3 → P4)
- **Polish (Final Phase)**: Depends on all desired user stories being complete

### Parallel Opportunities

- All Setup tasks marked [P] can run in parallel
- All Foundational tasks marked [P] can run in parallel (within Phase 2)
- Once Foundational phase completes, all user stories can start in parallel (if team capacity allows)
- Models within a story marked [P] can run in parallel
- Different user stories can be worked on in parallel by different team members

---

## Parallel Example: User Story 3

```bash
# Launch all agents for User Story 3 together:
Task: "Create Risk Management Agent in agents/risk_manager.py"
Task: "Create Capital Allocation Agent in agents/capital_allocator.py"
Task: "Create Execution Agent in agents/execution_agent.py"
Task: "Create Market Intelligence Agent in agents/market_intelligence.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL - blocks all stories)
3. Complete Phase 3: User Story 1
4. **STOP and VALIDATE**: Test User Story 1 independently
5. Deploy/demo if ready

### Incremental Delivery

1. Complete Setup + Foundational → Foundation ready
2. Add User Story 1 → Test independently → Deploy/Demo (MVP!)
3. Add User Story 2 → Test independently → Deploy/Demo
4. Add User Story 3 → Test independently → Deploy/Demo
5. Add User Story 4 → Test independently → Deploy/Demo
6. Each story adds value without breaking previous stories
