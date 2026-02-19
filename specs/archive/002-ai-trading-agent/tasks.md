# Tasks: AI Trading Agent — Order Manager, Sentiment Sentry & Trade Explainer

**Input**: `specs/002-ai-trading-agent/plan.md`, `specs/002-ai-trading-agent/spec.md`
**Branch**: `002-ai-trading-agent`

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no shared dependencies)
- **[Story]**: User story this task belongs to (US1, US2, US3, US4)
- Tests are included (explicitly requested in the feature description)

---

## Phase 1: Setup

**Purpose**: Project directory and dependency scaffolding — no business logic yet.

- [X] T001 Add `apscheduler>=3.10.0`, `httpx>=0.27.0`, `openai>=1.0.0` to `requirements.txt` _(note: no `copilot-sdk` PyPI package — use `openai` with GitHub Models endpoint or OpenAI API; set `OPENAI_API_BASE` env var to override)_
- [X] T002 [P] Create `agents/__init__.py` package file
- [X] T003 [P] Create `skills/__init__.py` package file

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: DB schema extensions and shared base that ALL user stories depend on.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T004 Add `staged_orders` table migration to `database/db_manager.py` (columns: `id`, `tws_order_id`, `account_id`, `instrument_type`, `symbol`, `expiration`, `strike`, `quantity`, `direction`, `limit_price`, `status`, `created_at`)
- [X] T005 [P] Add `market_intel` table migration to `database/db_manager.py` (columns: `id`, `trade_id`, `symbol`, `source`, `content`, `sentiment_score`, `created_at`)
- [X] T006 [P] Add `signals` table migration to `database/db_manager.py` (columns: `id`, `signal_type`, `legs_json`, `net_value`, `confidence`, `status`, `detected_at`, `updated_at`)
- [X] T007 [P] Add `trade_journal` table migration to `database/db_manager.py` (columns: `trade_id`, `symbol`, `entry_greeks_json`, `thesis`, `entry_at`)

- [X] T007b [P] Add `insert_trade_journal_entry()` async convenience method to `database/db_manager.py` for seeding `trade_journal` from `stage_order()` context (US4 depends on this being populated in production; mockable in tests but method must exist)

**Checkpoint**: DB schema ready — migrations apply cleanly on a fresh database.

---

## Phase 3: User Story 1 — Stage an order without transmitting (Priority: P1) 🎯 MVP

**Goal**: A trader calls `stage_order()` with a `/MES` futures request; the order appears in TWS with `transmit=False`, an order ID is returned, and a `StagedOrder` record is persisted to the database. Fully testable without Stages 2 or 3.

**Independent Test**: `python -m pytest tests/test_orders.py -v` — all tests pass with IBKR gateway up, or all non-integration tests pass with mocked gateway.

### Tests for User Story 1 ⚠️ Write first — ensure they FAIL before implementing

- [X] T008 [P] [US1] Write `tests/test_orders.py`: test `OrderRequest` Pydantic validation (valid STK, valid FUT /MES, invalid instrument type raises)
- [X] T009 [P] [US1] Write `tests/test_orders.py`: test `stage_order()` with mocked IBKR gateway — assert TWS REST payload contains `"transmit": false`, returns non-null order ID
- [X] T010 [P] [US1] Write `tests/test_orders.py`: test DB persistence — after `stage_order()` returns, query `staged_orders` table and assert record exists with status `STAGED`
- [X] T011 [P] [US1] Write `tests/test_orders.py`: test error path — unsupported instrument type raises `ValueError` before any TWS call is made
- [X] T012 [P] [US1] Write `tests/test_orders.py`: test rollback path — when DB write fails after TWS call, assert DB has no partial record (or order is cancelled)

### Implementation for User Story 1

- [X] T013 [US1] Define `OrderRequest` Pydantic model in `core/order_manager.py` (fields: `instrument_type: Literal["STK","FUT"]`, `symbol`, `quantity`, `direction: Literal["BUY","SELL"]`, `limit_price`, optional `expiration`, `strike`)
- [X] T014 [US1] Implement `OrderManager.stage_order(request: OrderRequest, account_id: str) -> str` in `core/order_manager.py` — call Client Portal REST `POST /iserver/order` with `transmit: false`, return `orderId`
- [X] T015 [US1] Add IBKR order-mapping logic in `core/order_manager.py` — translate `OrderRequest.instrument_type` to TWS `secType` (`STK`→`STK`, `FUT`→`FUT`), build correct conid lookup for `/MES` and `/ES`
- [X] T016 [US1] Persist `StagedOrder` record in `database/db_manager.py` — add `insert_staged_order()` async method; call it from `stage_order()` within a try/finally that cancels the TWS order on DB failure
- [X] T017 [US1] Add input validation guard in `stage_order()` — raise `ValueError("Unsupported instrument_type: ...")` for non-STK/FUT before any TWS call

**Checkpoint**: `pytest tests/test_orders.py` passes. `stage_order()` works end-to-end against a running IBKR gateway.

---

## Phase 4: User Story 2 — Automated sentiment score (Priority: P2)

**Goal**: `NewsSentry` fetches news on a 15-minute schedule, generates a sentiment score via LLM, and stores a `SentimentRecord` in `market_intel`. Fully testable without US1 being deployed.

**Independent Test**: `python -m pytest tests/test_news_sentry.py -v` — passes with mocked news provider and mocked LLM.

### Tests for User Story 2 ⚠️ Write first — ensure they FAIL before implementing

- [X] T018 [P] [US2] Write `tests/test_news_sentry.py`: test `NewsSentry.fetch_and_score()` with mocked news API returning 3 headlines — assert LLM called once with all headlines, `SentimentRecord` written to DB with score in [-1, +1] and summary ≤ 50 words
- [X] T019 [P] [US2] Write `tests/test_news_sentry.py`: test `no_news` path — mock news API returning empty list; assert record stored with `sentiment_score=None` and `content="no_news"`
- [X] T020 [P] [US2] Write `tests/test_news_sentry.py`: test resilience — mock news API raising `httpx.HTTPError`; assert exception is caught, nothing written to DB, scheduler tick counter increments normally

### Implementation for User Story 2

- [X] T021 [US2] Create `agents/news_sentry.py` with `NewsSentry` class — `__init__` accepts `symbols: list[str]`, `db`, `interval_seconds` (default 900)
- [X] T022 [US2] Implement `NewsSentry._fetch_news(symbol: str) -> list[str]` in `agents/news_sentry.py` — call Alpaca News API (`GET /v1beta1/news?symbols={symbol}&limit=20`) using `httpx.AsyncClient`; fall back to Finnhub if env `NEWS_PROVIDER=finnhub`
- [X] T023 [US2] Implement `NewsSentry._score_sentiment(headlines: list[str], symbol: str) -> tuple[float | None, str]` in `agents/news_sentry.py` — build LLM prompt requesting a JSON `{"score": float, "summary": str}`; call Copilot SDK chat completion; parse and clamp score to [-1, +1]
- [X] T024 [US2] Implement `NewsSentry.fetch_and_score(symbol: str)` in `agents/news_sentry.py` — orchestrate fetch + score + DB write to `market_intel` via `db.insert_market_intel()`; wrap in `try/except` logging all errors, never re-raising
- [X] T025 [US2] Add `db.insert_market_intel()` async method to `database/db_manager.py`
- [X] T026 [US2] Implement `NewsSentry.start()` in `agents/news_sentry.py` — configure `APScheduler` async interval job calling `fetch_and_score()` for each symbol; expose `stop()` for clean shutdown

**Checkpoint**: `pytest tests/test_news_sentry.py` passes. Running `NewsSentry.start()` for 1 tick against live APIs writes a row to `market_intel`.

---

## Phase 5: User Story 3 — Passive arbitrage detection (Priority: P2)

**Goal**: `ArbHunter` scans the option chain for Box Spread and Put-Call Parity opportunities, writes signals to the `signals` table, and marks stale ones `expired`. Fully testable without US1 or US2.

**Independent Test**: `python -m pytest tests/test_arbitrage.py -v` — passes with mock option chain data.

### Tests for User Story 3 ⚠️ Write first — ensure they FAIL before implementing

- [X] T027 [P] [US3] Write `tests/test_arbitrage.py`: test Put-Call Parity detection — construct mock option chain with violation (call_price - put_price ≠ forward - strike); assert signal written to `signals` table with correct legs
- [X] T028 [P] [US3] Write `tests/test_arbitrage.py`: test Box Spread detection — construct 4-leg mock with positive net value after fees; assert signal written with `signal_type="BOX_SPREAD"`
- [X] T029 [P] [US3] Write `tests/test_arbitrage.py`: test no-opportunity path — all spreads yield negative EV; assert no rows inserted into `signals`
- [X] T030 [P] [US3] Write `tests/test_arbitrage.py`: test expiry — pre-insert an ACTIVE signal; re-run `ArbHunter.scan()` with conditions no longer holding; assert record updated to `expired`

### Implementation for User Story 3

- [X] T031 [US3] Create `agents/arb_hunter.py` with `ArbHunter` class — `__init__` accepts `db`, `fee_per_leg: float` (default from `config/risk_matrix.yaml` key `arb_fee_per_leg`)
- [X] T032 [US3] Implement `ArbHunter._check_put_call_parity(chain: dict) -> list[dict]` in `agents/arb_hunter.py` — for each (strike, expiration) pair verify $C - P = F - K \cdot e^{-rT}$; flag violations where net EV > `fee_per_leg * 2`
- [X] T033 [US3] Implement `ArbHunter._check_box_spread(chain: dict) -> list[dict]` in `agents/arb_hunter.py` — enumerate all 4-leg box combinations; compute net debit/credit; flag where EV - `fee_per_leg * 4` > 0
- [X] T034 [US3] Implement `ArbHunter.scan(chain: dict)` in `agents/arb_hunter.py` — call both checkers, write new signals via `db.insert_signal()`, expire stale ones via `db.expire_stale_signals()`
- [X] T035 [US3] Add `db.insert_signal()` and `db.expire_stale_signals()` async methods to `database/db_manager.py`
- [X] T036 ~~Add `arb_fee_per_leg` key to `config/risk_matrix.yaml`~~ ✅ **ALREADY DONE** — key added at top-level of `config/risk_matrix.yaml` during analysis remediation; read it with `yaml.safe_load()` and access as `config['arb_fee_per_leg']`

**Checkpoint**: `pytest tests/test_arbitrage.py` passes. Running `ArbHunter.scan()` against live option chain data from the existing Tastytrade adapter writes at least one signal or produces an empty-but-correct result.

---

## Phase 6: User Story 4 — Natural-language trade explanation (Priority: P3)

**Goal**: The `explain_performance` skill accepts a `trade_id`, fetches entry thesis from `trade_journal`, current Greeks from the options cache, and recent sentiment from `market_intel`, then returns a human-readable P&L explanation. Fully testable without a live IBKR session by mocking the Greek fetcher.

**Independent Test**: `python -m pytest tests/test_explain_performance.py -v` — passes with mocked DB and mocked Greek fetcher.

### Tests for User Story 4 ⚠️ Write first — ensure they FAIL before implementing

- [X] T037 [P] [US4] Write `tests/test_explain_performance.py`: test happy path — mock `trade_journal` row with thesis + entry Greeks, mock current Greeks, mock sentiment; assert returned string mentions original thesis and at least one Greek by name
- [X] T038 [P] [US4] Write `tests/test_explain_performance.py`: test Vega-drag path — entry Vega = -50, current Vega = -150; assert returned string contains "Vega" (case-insensitive)
- [X] T039 [P] [US4] Write `tests/test_explain_performance.py`: test unknown trade_id — assert returns user-facing message containing "not found", no traceback propagated
- [X] T040 [P] [US4] Write `tests/test_explain_performance.py`: test missing market_intel — `trade_journal` row exists but no `market_intel` rows; assert skill returns explanation without crashing

### Implementation for User Story 4

- [X] T041 [US4] Create `skills/explain_performance.py` with `ExplainPerformanceSkill` class — `__init__` accepts `db`, `options_cache` (from `IBKRClient`), `llm_model: str` (default from env `LLM_MODEL`)
- [X] T042 [US4] Implement `ExplainPerformanceSkill._load_context(trade_id: str) -> dict | None` in `skills/explain_performance.py` — query `trade_journal` + `market_intel` via `db`; return `None` if trade not found
- [X] T043 [US4] Implement `ExplainPerformanceSkill._fetch_current_greeks(symbol: str, ...) -> dict` in `skills/explain_performance.py` — call `options_cache.fetch_and_cache_options_for_underlying()` for the position; return delta, gamma, theta, vega, iv
- [X] T044 [US4] Implement `ExplainPerformanceSkill._build_reflection_prompt(context: dict, current_greeks: dict) -> str` in `skills/explain_performance.py` — construct prompt comparing `entry_greeks_json` to `current_greeks`, embedding thesis and latest sentiment score; instruct LLM to call out Vega/Delta divergence explicitly
- [X] T045 [US4] Implement `ExplainPerformanceSkill.explain(trade_id: str) -> str` in `skills/explain_performance.py` — orchestrate: load context → return "not found" message if None → fetch current Greeks → build prompt → call Copilot SDK → return explanation string
- [X] T046 [US4] Add `db.get_trade_journal_entry()` and `db.get_market_intel_for_trade()` async methods to `database/db_manager.py`

**Checkpoint**: `pytest tests/test_explain_performance.py` passes. Calling `explain(trade_id)` against a seeded DB and live options cache produces a coherent explanation string.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T047 [P] Add `agents/` and `skills/` to `pytest.ini` `testpaths`
- [X] T048 [P] Add environment variable documentation to `README.md` (`NEWS_PROVIDER`, `NEWS_API_KEY`, `LLM_MODEL`, `NEWS_INTERVAL_SECONDS`, `ARB_FEE_PER_LEG`)
- [X] T049 Run `quickstart.md`-style end-to-end validation: stage one `/MES` order, trigger one `NewsSentry` tick, run one `ArbHunter.scan()`, call `explain()` on a seeded trade — confirm no exceptions and all four DB tables populated

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1; BLOCKS all user story phases
- **Phase 3 (US1)**: Depends on Phase 2
- **Phase 4 (US2)**: Depends on Phase 2; can run in parallel with Phase 3
- **Phase 5 (US3)**: Depends on Phase 2; can run in parallel with Phases 3 and 4
- **Phase 6 (US4)**: Depends on Phase 2; logically benefits from US1 (trade journal data) but is independently testable with mocks
- **Phase 7 (Polish)**: Depends on all desired user story phases

### User Story Dependencies

- **US1 (P1)**: Independent after Phase 2
- **US2 (P2)**: Independent after Phase 2 — no hard dependency on US1
- **US3 (P2)**: Independent after Phase 2 — no dependency on US1/US2
- **US4 (P3)**: Independent after Phase 2 — uses `trade_journal` table (seeded via US1 flow in production, but mockable in tests)

### Parallel Opportunities Within Each Story

- All test-writing tasks in a phase marked `[P]` can be written simultaneously
- `T004`/`T005`/`T006`/`T007` (four schema additions) can run in parallel
- `T008`–`T012` (US1 tests) can all be written in parallel
- `T013` and `T015` (model + mapping) can be written in parallel before `T014` wires them together
- `T021` and `T025` (US2 class scaffold + DB method) can be written in parallel before `T024` connects them
- `T031`, `T035`, `T036` (US3 scaffold + DB methods + config) can be written in parallel

---

## Parallel Example: User Story 1

```bash
# Write all tests simultaneously (they all fail — that's expected):
Task T008: OrderRequest validation tests
Task T009: stage_order() TWS mock tests
Task T010: DB persistence tests
Task T011: Invalid instrument type test
Task T012: Rollback on DB failure test

# Then implement in dependency order:
Task T013: OrderRequest model          ← no dependencies
Task T015: IBKR mapping logic          ← no dependencies (parallel with T013)
Task T016: DB insert_staged_order()    ← no dependencies (parallel with T013)
Task T014: stage_order() method        ← depends on T013, T015, T016
Task T017: Input validation guard      ← depends on T014
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (≈30 min)
2. Complete Phase 2: Foundational — DB migrations (≈1 hour)
3. Complete Phase 3: User Story 1 — OrderManager + tests (≈3–4 hours)
4. **STOP and VALIDATE**: Run `pytest tests/test_orders.py` and manually stage a `/MES` order against a running gateway
5. Deploy/demo if ready — this is the complete MVP

### Incremental Delivery

1. Setup + Foundational → DB migrations applied
2. US1 → `stage_order()` working → **MVP demo**
3. US2 → `NewsSentry` running → sentiment in DB
4. US3 → `ArbHunter` scanning → signals in DB
5. US4 → `explain()` returning coherent explanations

### Full Parallel Strategy (3 developers after Phase 2)

- **Dev A**: Phase 3 (US1 — OrderManager)
- **Dev B**: Phase 4 (US2 — NewsSentry)
- **Dev C**: Phase 5 (US3 — ArbHunter)
- Phase 6 (US4) after all three complete

---

## Notes

- `[P]` tasks touch different files and have no inter-task dependency — safe to run concurrently
- Tests marked with "Write first — ensure they FAIL" must be committed before implementation tasks begin
- Each phase ends with a **Checkpoint** that can be validated independently before proceeding
- `stage_order()` must NEVER set `transmit=True` — enforced by the test harness
- Fees for arb calculations default to `$0.65/leg` — override via `config/risk_matrix.yaml`
