# Tasks: Trade Proposer Agent

**Input**: Design documents from `/specs/006-trade-proposer/`
**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/ ✅, quickstart.md ✅

**User Stories**:

- [US1] Risk Breach Detection (P1)
- [US2] Capital-Efficient Candidate Generation (P1)
- [US3] Trade Approval Queue (P2)

**Format**: `- [ ] [TaskID] [P?] [Story?] Description with file path`

- **[P]**: Parallelizable (different files, no incomplete dependencies)
- **[Story]**: User story label (US1/US2/US3) for Phase 3+ tasks only

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Verify dependencies and environment configuration for the new agent

- [X] T001 Add `sqlmodel`, `asyncpg`, `psycopg2-binary` to requirements.txt if not already present
- [X] T002 Add `PROPOSER_INTERVAL`, `PROPOSER_NOTIFY_THRESHOLD`, and `PROPOSER_DB_URL` to `.env.example` with defaults

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure (DB model, schema migration, and simulation interface) that ALL user stories depend on

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T003 Create `models/proposed_trade.py` from `specs/006-trade-proposer/contracts/proposed_trade.py` — add `strategy_name`, `net_premium`, `delta_reduction`, `vega_reduction`, `init_margin_impact`, `maint_margin_impact` fields matching `data-model.md`
- [X] T004 Add `ensure_proposed_trades_schema()` function to `bridge/database_manager.py` — creates `proposed_trades` table via `SQLModel.metadata.create_all(engine, tables=[ProposedTrade.__table__])`
- [X] T005 [P] Implement `simulate_margin_impact(account_id: str, legs: list[dict]) -> dict` in `adapters/ibkr_adapter.py` — SOCKET mode: build `Bag` contract from legs, call `ib.whatIfOrder(contract, MarketOrder(...))`, return `{init_margin_change, maint_margin_change}` from `OrderState`
- [X] T006 [P] Implement `simulate_margin_impact` PORTAL mode in `adapters/ibkr_adapter.py` — `POST /v1/api/iserver/account/{accountId}/orders/whatif` with `secType: "BAG"`, extract margin fields from response; select branch based on `IB_API_MODE` env var
- [X] T007 Create `RiskRegimeLoader` class in `agents/proposer_engine.py` — loads `config/risk_matrix.yaml`, applies VIX scalers and term-structure scalers, returns active regime name + effective limit dict for a given `{vix, term_structure, recession_prob}`

**Checkpoint**: DB table exists, `simulate_margin_impact()` returns margin data, `RiskRegimeLoader` maps market conditions to regime limits

---

## Phase 3: User Story 1 — Risk Breach Detection (Priority: P1) 🎯 MVP

**Goal**: Continuously compare live portfolio Greeks against the regime-adjusted risk matrix limits and quantify what must change to restore compliance

**Independent Test**: Inject a synthetic breach state (e.g., `vega=-6000` with `NLV=50000` in `neutral_volatility`) and verify `BreachDetector.check()` returns the exact `distance_to_target` required to fix the vega breach

- [X] T008 [US1] Implement `BreachDetector.check(greeks_snapshot: dict, account_nlv: float) -> list[BreachEvent]` in `agents/proposer_engine.py` — compare `total_vega`, `spx_delta`, `daily_theta`, `gamma` against NLV-scaled limits from `RiskRegimeLoader`; return list of `BreachEvent(greek, current_value, limit, distance_to_target, regime)`
- [X] T009 [US1] Implement `BreachDetector._detect_regime(vix: float, term_structure: float, recession_prob: float) -> str` in `agents/proposer_engine.py` — maps to `low_volatility`, `neutral_volatility`, `high_volatility`, or `crisis_mode` per `risk_matrix.yaml` conditions
- [X] T010 [P] [US1] Implement `BreachDetector._distance_to_target(current, limit) -> float` in `agents/proposer_engine.py` — returns signed overshoot amount (negative for short-vega breach, positive for delta breach)
- [X] T011 [P] [US1] Implement margin utilization guard: if `margin_used / nlv > max_margin_pct`, add `BreachEvent(greek='margin', ...)` to breach list in `agents/proposer_engine.py`
- [X] T012 [US1] Write `tests/test_proposer_engine.py` — `TestBreachDetector` class: 4 regimes × vega/delta/theta breach scenarios using `neutral_volatility` limits from `risk_matrix.yaml`; 0 breaches passing case; `crisis_mode` flat-limit case

**Checkpoint**: `pytest tests/test_proposer_engine.py::TestBreachDetector` passes; breach detection correctly uses NLV-scaled limits not legacy absolute values

---

## Phase 4: User Story 2 — Capital-Efficient Candidate Generation (Priority: P1)

**Goal**: For each detected breach, generate SPX/SPY/ES option candidates, simulate their margin impact, score by efficiency, and persist the top-3 to `proposed_trades` — marking prior Pending records as Superseded

**Independent Test**: Given a mocked vega breach at `vega_reduction_needed=-500` and a mocked `simulate_margin_impact` returning `{init_margin_change: 2000}`, verify `ProposerEngine.generate()` returns a ranked list where the highest `efficiency_score` item is first and `ProposerEngine.persist_top3()` flips existing Pending rows to Superseded before inserting

- [X] T013 [P] [US2] Implement `CandidateGenerator.fetch_benchmark_options(underlying: str, dte_min=30, dte_max=60) -> list[dict]` in `agents/proposer_engine.py` — calls `IBKRAdapter` contract details for SPX/SPY/ES only (enforce allowlist FR-011); returns liquid options as leg dicts `{conId, symbol, action, quantity, strike, expiry}`
- [X] T014 [US2] Implement `ProposerEngine.generate(breaches: list[BreachEvent], account_id: str) -> list[CandidateTrade]` in `agents/proposer_engine.py` — for each breach type: build directional candidates (put spreads for delta/vega breaches, call spreads for upside), call `adapter.simulate_margin_impact()`, compute `EfficiencyScore = weighted_risk_reduction / max(init_margin_impact, 1.0) + (n_legs * arb_fee_per_leg)`
- [X] T015 [US2] Implement `ProposerEngine._rank_candidates(candidates: list[CandidateTrade]) -> list[CandidateTrade]` in `agents/proposer_engine.py` — sort descending by `efficiency_score`; apply FR-007 margin filter (reject if `margin_impact > available_margin`); return top-3
- [X] T016 [US2] Implement `ProposerEngine.persist_top3(account_id: str, candidates: list[CandidateTrade], session: Session)` in `agents/proposer_engine.py` — execute SQL `UPDATE proposed_trades SET status='Superseded' WHERE account_id=? AND status='Pending'` (FR-014), then `session.add_all([ProposedTrade(...) for c in candidates[:3]])`
- [X] T017 [P] [US2] Implement `_build_justification(breach: BreachEvent, candidate: CandidateTrade) -> str` in `agents/proposer_engine.py` — returns human-readable string e.g. `"Corrects Vega breach (-8,000 vs -4,800 limit) in neutral_volatility regime. Score: 0.72"`
- [X] T018 [US2] Extend `tests/test_proposer_engine.py` — `TestProposerEngine` class: mock `simulate_margin_impact`, verify score ranking, verify Supersede SQL is called before insert, verify `legs_json` at least 1 leg, verify `efficiency_score > 0` constraint

**Checkpoint**: `pytest tests/test_proposer_engine.py` all pass; `proposed_trades` table grows by ≤3 rows per run with prior Pending rows set to Superseded

---

## Phase 5: User Story 3 — Trade Approval Queue (Priority: P2)

**Goal**: Expose `Pending` proposals in the Streamlit dashboard for human review, with Approve/Reject actions and automated 5-minute monitoring loop

**Independent Test**: Start the agent with `MOCK_BREACH=TRUE python -m agents.trade_proposer --run-once`, verify 1–3 `Pending` rows appear in `proposed_trades`, then check the Streamlit dashboard shows the "Trade Proposer Queue" section with strategy name, efficiency score, and justification

- [X] T019 [US3] Create `agents/trade_proposer.py` — async 300s monitoring loop: fetch Greeks snapshot via `IBKRAdapter`, detect regime, call `BreachDetector.check()`, call `ProposerEngine.generate()` + `persist_top3()`, trigger Option C notification if `efficiency_score > PROPOSER_NOTIFY_THRESHOLD` or `regime == "crisis_mode"`
- [X] T020 [P] [US3] Implement `--run-once` and `MOCK_BREACH=TRUE` mode in `agents/trade_proposer.py` — if `MOCK_BREACH=TRUE`, inject synthetic breach state matching `neutral_volatility` vega limit breach; run one cycle and exit (enables CI testing per quickstart.md)
- [X] T021 [US3] Add Option C notification call to `agents/trade_proposer.py` — call `NotificationDispatcher.send(message, channel='telegram')` when `any(c.efficiency_score > threshold for c in top3)` or `regime == 'crisis_mode'`; import from `agent_tools/notification_dispatcher.py`
- [X] T022 [US3] Add "Trade Proposer Queue" panel to `dashboard/app.py` — query `SELECT * FROM proposed_trades WHERE status='Pending' ORDER BY efficiency_score DESC`; render as `st.dataframe` with columns: strategy_name, efficiency_score, justification, created_at; add Approve/Reject buttons that `UPDATE proposed_trades SET status=?`
- [X] T023 [P] [US3] Write `tests/test_trade_proposer.py` — `TestTradeProposerLoop`: mock `IBKRAdapter.fetch_greeks`, mock `ProposerEngine.generate`, assert `persist_top3` called; `TestOptionCNotification`: assert `NotificationDispatcher.send` called when score > 0.5; not called when score < 0.5 and regime != crisis_mode

**Checkpoint**: `pytest tests/test_trade_proposer.py` passes; `MOCK_BREACH=TRUE python -m agents.trade_proposer --run-once` exits 0 with proposals in DB; dashboard shows queue panel

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T024 [P] Add `__main__.py` entry-point guard to `agents/trade_proposer.py` — `if __name__ == "__main__": asyncio.run(main())` so `python -m agents.trade_proposer` works per quickstart.md
- [X] T025 [P] Add `trade_proposer` worker to `start_dashboard.sh` — start with `python -m agents.trade_proposer &` alongside existing workers; store PID
- [X] T026 Update `README.md` — document `agents/trade_proposer.py`, `agents/proposer_engine.py`, `models/proposed_trade.py` in the project structure section
- [X] T027 Update `.github/agents/copilot-instructions.md` — note `proposed_trades` table, `ProposerEngine`, `BreachDetector`, and `Option C` notification pattern
- [X] T028 Run full test suite `pytest tests/ -q --tb=short` and confirm 0 regressions against 193 baseline
- [X] T029 Add approval-gated order draft actions in `dashboard/app.py` for both proposed trades and arbitrage signals — prefill Order Builder legs only; require existing explicit submit approval flow for live orders
- [X] T030 Ensure LocalStore snapshot persistence captures live non-zero Greeks from dashboard summary on refresh cadence in `dashboard/app.py`
- [X] T031 Add package-level bid/ask/mid/spread preview in `dashboard/app.py` for selected proposed trades and arbitrage signal drafts prior to order creation

---

## Dependency Graph

```
T001 → T003 → T007 → T008 → T009 → T014 → T019
T002 ↗                ↘    ↘      ↗       ↗
T004 ← T003           T010  T011  T015    T020
T005 ← T003                 ↓     T016 → T022
T006 ← T003           T012  T013  T017
                              ↘    ↗
                               T018 → T023
```

**Phases that can run in parallel after T007**:

- US1 (T008–T012) and US2 (T013–T018) share `proposer_engine.py` — implement sequentially within the file; tests [T012, T018] are parallelizable with other phase's non-engine files
- T005 and T006 (Socket vs Portal simulation modes) are fully parallelizable
- T013 and T017 (candidate generator and justification builder) are parallelizable

---

## Implementation Strategy

| Phase     | Deliverable                     | Independently Testable?                                            |
| --------- | ------------------------------- | ------------------------------------------------------------------ |
| Phase 1+2 | DB model + simulation interface | Yes: `pytest tests/test_ibkr_adapter.py`                           |
| Phase 3   | Breach detection                | Yes: `pytest tests/test_proposer_engine.py::TestBreachDetector`    |
| Phase 4   | Engine + persistence            | Yes: `pytest tests/test_proposer_engine.py`                        |
| Phase 5   | Full agent + dashboard panel    | Yes: `MOCK_BREACH=TRUE python -m agents.trade_proposer --run-once` |
| Phase 6   | Polish + integration            | Yes: `pytest tests/ -q` (0 regressions)                            |

**MVP Scope**: Phase 1 + Phase 2 + Phase 3 = Risk breach detection with regime-aware limits. Everything else builds on top.

**Parallel Execution per Story**:

- Within US1: T009 and T010 and T011 can run in parallel (distinct methods in same file)
- Within US2: T013 and T017 can run in parallel
- Within US3: T020 and T021 and T023 can run in parallel
