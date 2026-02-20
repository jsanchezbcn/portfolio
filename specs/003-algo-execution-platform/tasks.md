---
description: "Task list for 003-algo-execution-platform"
---

# Tasks: Read-Write Algorithmic Execution & Journaling Platform

**Input**: Design documents from `specs/003-algo-execution-platform/`
**Branch**: `003-algo-execution-platform`
**Prerequisites**: plan.md ✅ | spec.md ✅ | research.md ✅ | data-model.md ✅ | contracts/ ✅ | quickstart.md ✅

**Tests**: Test tasks are included per the feature specification and Constitution §I (test-first for all business logic).

**Organization**: Tasks are grouped by user story to enable independent implementation and testing.

## Format: `[ID] [P?] [Story?] Description with file path`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks in the same phase)
- **[Story]**: Which user story this task belongs to (US1–US7)
- Exact file paths included in all descriptions

---

## Phase 1: Setup (Project Initialization)

**Purpose**: Dependencies, config fixes, and test fixtures. These are prerequisites for all downstream work.

- [x] T001 ~~Add `yfinance` to requirements.txt~~ — **pre-completed**: `yfinance>=0.2.40` already present in `requirements.txt` from prior commit
- [x] T002 Fix `beta_config.json` — change `"MES": 0.986` to `"MES": 1.0` (economic beta, not statistical). Note: `"/MES": 1.0` is already correct; only the bare `"MES"` key (no slash, as IBKR returns it without prefix) needs updating.
- [x] T003 [P] Create `tests/fixtures/sample_whatif_response.json` — mock IBKR `/orders/whatif` response with `"amount"` (initial margin), `"equity"`, and `"equityWithLoanAfter"` fields
- [x] T004 [P] Add new env vars to `.env.example`: `IBKR_ACCOUNT_ID`, `SNAPSHOT_INTERVAL_SECONDS` (default 900), `THETA_BUDGET_PER_SUGGESTION` (default 0), `COPILOT_TOKEN` (GitHub Copilot SDK token, e.g. `ghp_...`)

**Checkpoint**: Dependencies and config ready — no breaking changes to existing code yet.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before any user story can be implemented. Fixes existing bugs, creates shared data models, and initializes database tables.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [x] T005 Fix `adapters/ibkr_adapter.py` L152–L158 — replace `price = float(position.strike)` fallback with `price = position.underlying_price` when computing SPX-weighted delta for options; requires adding `underlying_price: Optional[float] = None` to `UnifiedPosition` (T008) and populating it from IBKR position data at parse time
- [x] T006 ~~Fix `adapters/ibkr_adapter.py` — hardcode `multiplier = 1.0` for stocks/ETFs when IBKR returns multiplier = 0~~ — **pre-completed**: guard `raw_contract_multiplier if raw_contract_multiplier and raw_contract_multiplier > 0 else 1.0` already implemented at L116 and L141
- [x] T007 [P] Create `models/order.py` — define dataclasses: `OrderLeg`, `Order`, `SimulationResult`, `RiskBreach`, `AITradeSuggestion` with all fields from data-model.md; include `OrderStatus` enum (DRAFT → SIMULATED → PENDING → FILLED / REJECTED / CANCELLED)
- [x] T008 [P] Extend `models/unified_position.py` — add `BetaWeightedPosition` dataclass with fields: `position`, `beta`, `beta_source`, `beta_unavailable: bool`, `spx_equivalent_delta: float`
- [x] T009 Extend `database/local_store.py` — add `trade_journal` table DDL and `_init_trade_journal()` method; call from `__init__`; schema per data-model.md (id UUID, created_at, broker, account_id, broker_order_id, underlying, strategy_tag, status, legs_json, net_debit_credit, vix_at_fill, spx_price_at_fill, regime, pre_greeks_json, post_greeks_json, user_rationale, ai_rationale, ai_suggestion_id; plus 4 indexes)
- [x] T010 Extend `database/local_store.py` — add `account_snapshots` table DDL and `_init_account_snapshots()` method; call from `__init__`; schema per data-model.md (id, captured_at, account_id, broker, net_liquidation, cash_balance, spx_delta, gamma, theta, vega, delta_theta_ratio, vix, spx_price, regime; plus 2 indexes)
- [x] T011 [P] Write `tests/test_order_builder.py` — unit tests for `Order`/`OrderLeg` dataclass validation: max 4 legs enforced, `OrderStatus` FSM transitions valid, `SimulationResult` fields populated; all tests must FAIL before T007 is complete

**Checkpoint**: All shared infrastructure ready; existing tests still passing; three adapter bugs fixed; new models importable; DB tables auto-create on startup.

---

## Phase 3: User Story 1 — Accurate SPX Delta Display (Priority: P1) 🎯 MVP

**Goal**: Dashboard shows a single "Portfolio SPX Equivalent Delta" that matches the IBKR Risk Navigator within ±5 points. Fixes the dollar-beta formula and fetches beta from Tastytrade/yfinance fallback.

**Independent Test**: With live broker connections, load the dashboard and compare the displayed "SPX Equivalent Delta" against the IBKR Risk Navigator. Must match within ±5 SPX delta points. No downstream features required.

### Tests for User Story 1

> **Write these tests FIRST — ensure they FAIL before implementing T015–T020**

- [x] T012 [P] [US1] Write `tests/test_beta_weighter.py` — unit tests for `BetaWeighter`: beta fetch from Tastytrade mock, yfinance fallback when Tastytrade unavailable, static `beta_config.json` fallback, `beta_unavailable=True` when all sources fail, SPX delta formula `(Δ × Q × M × β × P_underlying) / P_SPX` for stock/option/futures, multiplier handling for /ES (50) and /MES (5)

### Implementation for User Story 1

- [x] T013 [US1] Implement `risk_engine/beta_weighter.py` — `BetaWeighter` class with `get_beta(symbol) -> tuple[float, str, bool]` (value, source, unavailable_flag); primary source: `tastytrade.metrics.get_market_metrics()`; fallback: `yfinance.Ticker(sym).info["beta"]`; final fallback: `beta_config.json`; default 1.0 + `beta_unavailable=True` if all fail
- [x] T014 [US1] Implement `BetaWeighter.compute_spx_equivalent_delta(position) -> BetaWeightedPosition` — formula: `(position.delta × position.quantity × position.multiplier × beta × position.underlying_price) / spx_price`; handle multiplier=5 (/MES), multiplier=50 (/ES), multiplier=100 (equity options), multiplier=1 (stocks)
- [x] T015 [US1] Implement `BetaWeighter.compute_portfolio_spx_delta(positions) -> PortfolioGreeks` — aggregate all `BetaWeightedPosition.spx_equivalent_delta` values; return `PortfolioGreeks` with SPX delta, sum of Gamma/Theta/Vega from raw positions, and timestamp
- [x] T016 [US1] Integrate `BetaWeighter` into `adapters/ibkr_adapter.py` — replace manual delta calculation in `get_portfolio_greeks()` with `BetaWeighter.compute_portfolio_spx_delta()`
- [x] T017 [P] [US1] Integrate `BetaWeighter` into `adapters/tastytrade_adapter.py` — replace manual delta calculation in `get_portfolio_greeks()` with `BetaWeighter.compute_portfolio_spx_delta()`
- [x] T018 [US1] Add "Portfolio SPX Equivalent Delta" metric to `dashboard/app.py` header — prominently displayed as a large number, refreshes on each data cycle
- [x] T019 [US1] Add ⚠ "Beta Unavailable" warning badge to position rows in `dashboard/app.py` — shown for any position where `BetaWeightedPosition.beta_unavailable == True`
- [x] T020 [US1] Add "SPX price unavailable" error state to `dashboard/app.py` — halts Greek aggregation and shows a visible banner when SPX price cannot be fetched

**Checkpoint**: SPX Equivalent Delta is live and matches broker within spec. US1 independently verifiable. Run `tests/test_beta_weighter.py` — all pass.

---

## Phase 4: User Story 2 — Pre-Trade Margin Simulation (Priority: P2)

**Goal**: Order builder panel with "Simulate Trade" returns projected Initial Margin and post-trade Greeks from IBKR WhatIf API — no live order transmitted.

**Independent Test**: Build a test SPX iron condor in the order builder, click "Simulate," and receive back Initial Margin and post-trade Greeks within 5 seconds. Submit button remains disabled. Zero orders transmitted.

### Tests for User Story 2

> **Write these tests FIRST — ensure they FAIL before implementing T022–T028**

- [x] T021 [P] [US2] Write `tests/test_execution.py` simulate() section — unit tests using `tests/fixtures/sample_whatif_response.json`: successful multi-leg simulation returns `SimulationResult`; timeout (>10s) returns error without submitting; broker 503 returns error without submitting; Delta breach detected when post-trade delta exceeds `risk_matrix.yaml` limit

### Implementation for User Story 2

- [x] T022 [US2] Create `core/execution.py` — `ExecutionEngine` class skeleton with `__init__(ibkr_gateway_client, local_store, beta_weighter)` and three stub methods: `simulate()`, `submit()`, `flatten_risk()`
- [x] T023 [US2] Implement `ExecutionEngine.simulate(order: Order) -> SimulationResult` — call `POST /iserver/account/{acctId}/orders/whatif`; build `conidex` multi-leg payload; parse `amount` (initial margin), `equity`, `equityWithLoanAfter`; compute post-trade Greeks by adding the order's net Greeks (from `BetaWeighter` applied to each order leg, using the leg's delta × qty × multiplier × beta × underlying*price / SPX_price) to the \_current live* `PortfolioGreeks` snapshot to produce the simulated post-trade state; set 10s timeout; return `SimulationResult` with `margin_requirement`, `post_trade_greeks`, `delta_breach: bool`
- [x] T024 [US2] Add Delta breach detection in `ExecutionEngine.simulate()` — load `max_portfolio_delta` from `config/risk_matrix.yaml`; set `SimulationResult.delta_breach = True` if `post_trade_greeks.spx_delta` exceeds limit
- [x] T025 [US2] Create `dashboard/components/order_builder.py` — Streamlit component with: underlying input, leg builder (action / instrument / qty / type / strike / expiry per leg), order type selector (Limit / Market / MOC), user rationale textarea. **Constraint**: MOC order type selector only enabled when all legs are equities or ETFs (`instrument_type == EQUITY`); display `st.warning("MOC not supported for options — switch to Limit or Market")` if user selects MOC with option legs present
- [x] T026 [US2] Add "Simulate Trade" button + results panel to `dashboard/components/order_builder.py` — displays `SimulationResult.margin_requirement`, post-trade Greeks table; highlights Delta in red when `delta_breach == True`; disables "Submit Order" button until simulation succeeds; disables "Simulate" button while simulation is in flight to prevent duplicate calls
- [x] T027 [US2] Wire `dashboard/components/order_builder.py` into `dashboard/app.py` as a collapsible sidebar panel
- [x] T028 [US2] Handle broker unavailable in `ExecutionEngine.simulate()` — display clear error message in order builder; prevent order submission

**Checkpoint**: Simulation fully functional. US2 independently verifiable. Run `tests/test_execution.py` simulate() section — all pass.

---

## Phase 5: User Story 3 — Live Order Execution (Priority: P3)

**Goal**: Submit single-leg and multi-leg orders from the dashboard with 2-step confirmation. Order fills update positions and trigger journal recording.

**Independent Test**: Submit a 1-share or 1 /MES contract test order from the order builder; verify it appears in the IBKR order blotter, fills, and positions update on next refresh. Zero unconfirmed transmissions.

### Tests for User Story 3

> **Write these tests FIRST — ensure they FAIL before implementing T030–T035**

- [x] T029 [P] [US3] Write `tests/test_execution.py` submit() section — unit tests: confirmed order transmitted to IBKR; unconfirmed order (cancel at modal) NOT transmitted; multi-leg combo uses `conidex` format; broker rejection surfaces rejection reason; connection drop mid-order returns "status unknown"

### Implementation for User Story 3

- [X] T030 [US3] Implement `ExecutionEngine.submit(order: Order) -> Order` — **HUMAN APPROVAL REQUIRED**: guard raises `ValueError` if `order.status != SIMULATED`; call `POST /iserver/account/{acctId}/orders`; build `conidex` multi-leg payload for combos; poll order status until FILLED/REJECTED/CANCELLED or timeout (30s); return updated `Order` with fill details; mark status "unknown" on connection loss. **Safety contract: this method may ONLY be called from a UI path that has completed the 2-step human confirmation modal (T031).**
- [X] T031 [US3] Add mandatory 2-step human approval modal to `dashboard/components/order_builder.py` — Step 1: "Review & Approve" expander previewing all legs, quantities, order type, estimated margin, and post-trade Greeks; Step 2: user must check an explicit "I confirm this order" checkbox AND click "Confirm & Submit — LIVE ORDER"; Step 3: disable all buttons immediately after transmission; the Submit path in `ExecutionEngine.submit()` is the **only** path that may call `_SS_APPROVED = True` in session state; direct bypass is impossible from the UI
- [ ] T032 [US3] Handle multi-leg combo order routing in `ExecutionEngine.submit()` — enforce all legs sent as single linked order via `conidex`; IBKR can still partially fill combo orders; if fill is partial, mark `Order.status = PARTIAL` in the journal, display remaining unfilled legs in the order blotter as PENDING, do not auto-cancel remainder; full cancellation of remainder is a manual user action via the broker platform
- [ ] T033 [US3] Display broker rejection reason in `dashboard/components/order_builder.py` — parse IBKR rejection message; render in red; do NOT record as filled trade
- [ ] T034 [US3] Handle "status unknown" in `dashboard/components/order_builder.py` — surface alert: "Order status unknown — verify in broker platform"; keep order visible in blotter
- [ ] T035 [US3] Trigger position refresh in `dashboard/app.py` after confirmed fill — update portfolio snapshot within one refresh cycle

**Checkpoint**: Live execution working. US3 standalone testable. Run `tests/test_execution.py` submit() section — all pass.

---

## Phase 5b: Real-Time Market Data in Order Builder (New Requirement)

**Goal**: Every leg in the order builder shows live bid/ask/last price from the broker. Options legs show a full strike-chain picker with real-time quotes so the trader can choose a specific contract at a known price before simulating.

**Independent Test**: Open order builder, type "ES" as underlying, select FUTURES — verified bid/ask/last updates within 2 seconds. Type "SPY", select OPTION — verified strike chain table with bid/ask renders within 3 seconds. Selecting a row populates strike/expiry fields automatically.

### Implementation for Phase 5b

- [X] T-RT0 [P] Write `tests/test_market_data.py` — unit tests for `MarketDataService`: `get_quote(symbol)` returns `Quote(bid, ask, last)` for stocks; `resolve_conid(symbol, sec_type)` returns int conid; `get_options_chain(underlying)` returns list of `OptionQuote`; timeout returns `None` without raising; IBKR unavailable returns `None`
- [X] T-RT1 Create `core/market_data.py` — `MarketDataService` class wrapping `IBKRClient`: `resolve_conid(symbol, sec_type="STK") -> int | None` via `/iserver/secdef/search`; `get_quote(symbol, sec_type="STK") -> Quote | None` using `get_market_snapshot` with price fields 31/84/86; `Quote` dataclass with `bid`, `ask`, `last`, `symbol`, `conid`, `fetched_at`
- [X] T-RT2 Extend `MarketDataService` with futures support — `get_futures_quote(root_symbol: str) -> Quote | None`; resolve front-month conid via existing `_lookup_es_conid`-style logic for any root (ES, MES, NQ, RTY, GC, CL); use `secdef/search?secType=FUT` + `secdef/info` to get tradeable conid; return `Quote` with bid/ask/last from snapshot
- [X] T-RT3 Extend `MarketDataService` with options chain — `get_options_chain(underlying: str, expiry: str | None = None) -> list[OptionQuote]`; primary source: Tastytrade `TastytradeOptionsFetcher.simulate_prefetch(underlying)` (already in codebase); fallback: IBKR `/iserver/secdef/strikes` + snapshot for bid/ask; `OptionQuote` dataclass: `symbol`, `underlying`, `expiry`, `strike`, `option_type`, `bid`, `ask`, `last`, `delta`, `iv`, `conid`
- [X] T-RT4 Add real-time price display to `dashboard/components/order_builder.py` — for each leg: after symbol entry, show inline `bid / ask / last` fetched via `MarketDataService.get_quote()` with a refresh button; update whenever symbol or instrument_type changes; show "–" if unavailable (never block order entry)
- [X] T-RT5 Add options chain picker to `dashboard/components/order_builder.py` — when `instrument_type == OPTION`: render `st.dataframe` of strikes with bid/ask/delta/IV for the selected expiry; clicking a row auto-fills strike, call/put, expiry fields in the leg; show expiry dropdown above the chain; chain fetched via `MarketDataService.get_options_chain()`; loading spinner while fetching

**Checkpoint**: Real-time prices live in order builder. Options chain picker working. No impact to simulation or submission paths.

---

**Goal**: Every order fill automatically journaled with full market context (VIX, regime, pre/post Greeks, rationale). Journal viewable and filterable in the dashboard.

**Independent Test**: Execute one trade; open Journal tab; verify a complete entry (timestamp, fill price, VIX, regime, pre/post Greeks, rationale) is stored and retrievable. Standalone value as an audit log.

### Tests for User Story 4

> **Write these tests FIRST — ensure they FAIL before implementing T037–T046**

- [ ] T036 [P] [US4] Write `tests/test_trade_journal.py` — unit tests using in-memory SQLite: `record_fill()` stores all required fields (FR-014 list); `query_journal()` filters by date range, instrument, regime correctly; `export_csv()` produces valid CSV with all columns; journal persists across `LocalStore` reconnect

### Implementation for User Story 4

- [ ] T037 [US4] Implement `database/local_store.py` `record_fill(journal_entry: TradeJournalEntry) -> str` — insert row into `trade_journal`; return generated UUID; handle duplicate broker_order_id gracefully (upsert on conflict)
- [ ] T038 [US4] Implement `database/local_store.py` `query_journal(start_dt, end_dt, instrument, regime, limit) -> list[TradeJournalEntry]` — parameterized SELECT with optional WHERE clauses; ORDER BY created_at DESC
- [ ] T039 [US4] Implement `database/local_store.py` `export_csv(entries: list[TradeJournalEntry]) -> str` — return CSV string with headers matching all FR-014 fields
- [ ] T040 [US4] Hook fill callback in `core/execution.py` — after `ExecutionEngine.submit()` confirms FILLED status (the `FILLED` status path introduced in T030), build `TradeJournalEntry` and call `local_store.record_fill()`; capture `user_rationale` from order builder textarea
- [ ] T041 [US4] Capture VIX + regime at fill time in `core/execution.py` fill handler — fetch VIX from IBKR market data; get current regime from `regime_detector.py`; attach to `TradeJournalEntry`
- [ ] T042 [US4] Capture pre/post portfolio Greeks at fill time — pre-Greeks = `BetaWeighter.compute_portfolio_spx_delta()` called before `submit()`; post-Greeks = recalculated after fill confirmed; serialize both as JSON into `TradeJournalEntry`
- [ ] T043 [US4] Create `dashboard/components/trade_journal_view.py` — Streamlit component: table display with reverse-chronological order; columns: timestamp, underlying, strategy_tag, status, net_debit_credit, vix_at_fill, regime, user_rationale
- [ ] T044 [US4] Add filters to `dashboard/components/trade_journal_view.py` — date range picker, instrument text filter, regime dropdown; filters apply on change
- [ ] T045 [US4] Add "Export CSV" button to `dashboard/components/trade_journal_view.py` — calls `local_store.export_csv()`; triggers `st.download_button()`
- [ ] T046 [US4] Wire `dashboard/components/trade_journal_view.py` into `dashboard/app.py` as a dedicated tab

**Checkpoint**: Every fill auto-journaled. Journal tab functional and filterable. Exports CSV. Run `tests/test_trade_journal.py` — all pass.

---

## Phase 7: User Story 5 — AI Risk Analyst Suggestions (Priority: P4)

**Goal**: On limit breach, automatically invoke AI and display 3 actionable trade suggestion cards within 10 seconds. Clicking a card pre-fills the order builder. AI unavailability never crashes the dashboard.

**Independent Test**: Manually trigger a Vega floor breach (reduce Vega below threshold in config); verify 3 suggestion cards appear within 10 seconds each showing legs, projected Greeks improvement, and Theta cost. Clicking one card pre-fills the order builder.

### Tests for User Story 5

> **Write these tests FIRST — ensure they FAIL before implementing T048–T056**

- [ ] T047 [P] [US5] Write unit tests for `suggest_trades()` in existing test file or new `tests/test_ai_risk_auditor.py` — mock LLM returning 3 structured suggestions; mock LLM timeout → empty list (no exception raised); mock LLM invalid JSON → empty list; suggestion rationale stored in journal when acted upon

### Implementation for User Story 5

- [ ] T048 [US5] Extend `agents/llm_risk_auditor.py` — add `suggest_trades(portfolio_greeks: PortfolioGreeks, vix: float, regime: str, breach: RiskBreach, theta_budget: float) -> list[AITradeSuggestion]` method
- [ ] T049 [US5] Implement `suggest_trades()` prompt — system role: "Quantitative options risk analyst specializing in income strategies"; include: current Greeks, VIX, regime, breach type + threshold + actual value, theta_budget; require response as JSON array of exactly 3 objects with fields: `legs[]`, `projected_delta_change`, `projected_theta_cost`, `rationale`
- [ ] T050 [US5] Parse `suggest_trades()` LLM JSON response into `list[AITradeSuggestion]` dataclasses — validate field presence; on parse error or exception return empty list (never raise per FR-023); log errors to application log
- [ ] T051 [US5] Hook risk breach detection → auto-invoke `suggest_trades()` in `dashboard/app.py` — watch `PortfolioGreeks` each refresh cycle; compare against limits in `config/risk_matrix.yaml`; if breach detected, call `llm_risk_auditor.suggest_trades()` in a background thread; store `list[AITradeSuggestion]` in session state
- [ ] T052 [US5] Create `dashboard/components/ai_suggestions.py` — Streamlit component: displays breach alert banner (always, even if AI unavailable); renders up to 3 suggestion cards (or "AI unavailable — no suggestions" if list empty)
- [ ] T053 [US5] Implement suggestion cards in `dashboard/components/ai_suggestions.py` — each card shows: underlying + legs summary, projected Greeks improvement delta, estimated Theta cost, rationale text; "Use This Trade" button
- [ ] T054 [US5] Wire "Use This Trade" button → auto-fill `dashboard/components/order_builder.py` — populate leg fields from `AITradeSuggestion.legs`; store `ai_suggestion_id` in session state for journaling
- [ ] T055 [US5] Attach `ai_suggestion_id` and `ai_rationale` to `TradeJournalEntry` in `core/execution.py` fill handler — only when trade originated from an AI suggestion (session state `ai_suggestion_id` is set)
- [ ] T056 [US5] Wire `dashboard/components/ai_suggestions.py` into `dashboard/app.py` sidebar/panel

**Checkpoint**: AI risk analyst live. Breach → 3 suggestions within 10s. Card → order builder pre-fill. AI unavailable → dashboard fully functional. Run `tests/test_ai_risk_auditor.py` — all pass.

---

## Phase 8: User Story 6 — Historical Risk & Performance Charts (Priority: P4)

**Goal**: Background logging records portfolio snapshots every 15 minutes. Historical chart panel shows Account Value vs SPX Delta (dual-axis) and Delta/Theta ratio over time with time-range filtering.

**Independent Test**: Run dashboard for 1+ hour; open historical chart panel; verify account value and SPX Delta curves render with correct values and timestamps. Time-range filter updates charts.

### Tests for User Story 6

> **Write these tests FIRST — ensure they FAIL before implementing T058–T065**

- [ ] T057 [P] [US6] Write `tests/test_trade_journal.py` snapshot section — unit tests: `capture_snapshot()` stores all required fields; `query_snapshots()` filters by date range; `delta_theta_ratio` stored as `theta / delta` (or None when delta = 0); background logger mock confirms rows inserted at interval

### Implementation for User Story 6

- [ ] T058 [US6] Implement `database/local_store.py` `capture_snapshot(snapshot: AccountSnapshot) -> str` — insert row into `account_snapshots`; compute `delta_theta_ratio = theta / delta` if `delta != 0` else `None`; return UUID
- [ ] T059 [US6] Implement `database/local_store.py` `query_snapshots(start_dt, end_dt, account_id) -> list[AccountSnapshot]` — ORDER BY captured_at ASC for chart rendering
- [ ] T060 [US6] Implement background snapshot asyncio task in `dashboard/app.py` — use `st.session_state` to store the task handle and only create it once (`if 'snapshot_task' not in st.session_state`), since Streamlit re-runs the full script on every user interaction; use `threading.Thread(target=_snapshot_loop, daemon=True)` as the persistence mechanism (threading survives Streamlit reruns; asyncio tasks do not); `_snapshot_loop()` fetches Net Liquidation + `PortfolioGreeks` + VIX + regime every `SNAPSHOT_INTERVAL_SECONDS` seconds; calls `local_store.capture_snapshot()`; logs errors without crashing
- [ ] T061 [US6] Add last-snapshot timestamp indicator to `dashboard/app.py` — small status text showing "Last snapshot: HH:MM:SS" (or "Snapshot logger error" if last run failed)
- [ ] T062 [US6] Create `dashboard/components/historical_charts.py` — `render_account_vs_delta_chart(snapshots)` using Plotly `make_subplots(specs=[[{"secondary_y": True}]])`; primary y: Net Liquidation Value; secondary y: SPX Equivalent Delta; x-axis: timestamps
- [ ] T063 [US6] Add `render_delta_theta_ratio_chart(snapshots)` to `dashboard/components/historical_charts.py` — Plotly line chart of `delta_theta_ratio` over time; label y-axis "Income-to-Risk Efficiency Ratio (Θ/Δ)"; render "N/A" points as gaps
- [ ] T064 [US6] Add time-range filter to `dashboard/components/historical_charts.py` — `st.selectbox` with options: 1D, 1W, 1M, All; filters `snapshots` list before rendering both charts
- [ ] T065 [US6] Wire `dashboard/components/historical_charts.py` into `dashboard/app.py` as a dedicated tab
- [ ] T065b [US6] Add Sebastian `|Theta|/|Vega|` ratio panel to `dashboard/components/historical_charts.py` **[Constitution §III MANDATORY]** — render as colored gauge or time-series line; green band 0.25–0.40; red band <0.20 or >0.50; label: "Sebastian Ratio (|Θ|/|V|)"; display alongside the Theta/Delta chart; data source: `account_snapshots.theta` and `account_snapshots.vega`

**Checkpoint**: Background snapshot logger running. Historical charts tab functional with dual-axis and ratio charts. Time-range filter works. Run `tests/test_trade_journal.py` snapshot section — all pass.

---

## Phase 9: User Story 7 — Flatten Risk Panic Button (Priority: P5)

**Goal**: A "Flatten Risk" button generates buy-to-close market orders for all short option legs, presents a confirmation dialog, and journals each fill with rationale "Flatten Risk — user-initiated."

**Independent Test**: Click "Flatten Risk" with a portfolio containing short options; verify generated order list shows only buy-to-close for short legs; click Cancel → zero orders sent; click Confirm → orders submitted and journaled.

### Tests for User Story 7

> **Write these tests FIRST — ensure they FAIL before implementing T067–T073**

- [ ] T066 [P] [US7] Write `tests/test_execution.py` flatten_risk() section — unit tests: only short option positions included in buy-to-close list; long options and futures excluded; "no short positions" case returns empty list with message; all orders submitted simultaneously on confirm; each fill journaled with correct rationale string

### Implementation for User Story 7

- [ ] T067 [US7] Implement `ExecutionEngine.flatten_risk(positions: list[Position]) -> list[Order]` in `core/execution.py` — filter positions to short option legs only (qty < 0 and instrument_type in {CALL, PUT}); create one buy-to-close market order per short leg; return order list without transmitting
- [ ] T068 [US7] Handle "no short positions" edge case in `ExecutionEngine.flatten_risk()` — return empty list; caller displays "No short positions to close"
- [ ] T069 [US7] Add "Flatten Risk" button to `dashboard/app.py` — visible in main header or first sidebar section (accessible within 2 clicks); opens confirmation dialog on click
- [ ] T070 [US7] Implement flatten confirmation dialog in `dashboard/app.py` — display all buy-to-close orders in a table; show total estimated margin release; "Confirm Flatten" button and "Cancel" button; Cancel → no orders sent
- [ ] T071 [US7] Implement "Confirm Flatten" action — `ExecutionEngine.submit()` called for all flatten orders simultaneously (asyncio.gather); orders transmitted in one batch
- [ ] T072 [US7] Journal each flatten fill in `core/execution.py` fill handler — same flow as US4 fill journaling; hardcode `user_rationale = "Flatten Risk — user-initiated"`; `strategy_tag = "FLATTEN"`
- [ ] T073 [US7] Add unfilled flatten orders to order blotter in `dashboard/app.py` — remain visible with status PENDING until filled or cancelled

**Checkpoint**: Flatten Risk fully functional. Confirmation-to-submission under 30s (SC-006). All fills journaled. Run `tests/test_execution.py` flatten_risk() section — all pass.

---

## Phase 10: Polish & Cross-Cutting Concerns

**Purpose**: Final validation, documentation, and cleanup across all user stories.

- [ ] T074 [P] Run full pytest suite (`pytest tests/ -v --tb=short`) — fix any regressions; target ≥80% coverage for `risk_engine/`, `core/`, `models/`, `adapters/`; confirm all 7 user story test files pass
- [ ] T075 [P] Update `README.md` — add section for new modules (BetaWeighter, ExecutionEngine, TradeJournal, AI Analyst, Historical Charts, Flatten Risk); update startup instructions with new env vars
- [ ] T076 Update `docs/IMPROVEMENTS.md` — document architecture decisions, known limitations, and next steps for this feature
- [ ] T077 [P] Validate `quickstart.md` test scenarios still accurate — update any changed file paths, env vars, or commands
- [ ] T078 Perform end-to-end manual test per `quickstart.md` — complete all test scenarios in sequence; verify SC-001 through SC-008 met

---

## Dependencies & Execution Order

### Phase Dependencies

```
Phase 1 (Setup)
    └── Phase 2 (Foundational) ← BLOCKS ALL USER STORIES
            ├── Phase 3 (US1 — P1) 🎯 MVP
            │       └── Phase 4 (US2 — P2)  ← use BetaWeighter for post-trade Greeks
            │               └── Phase 5 (US3 — P3)  ← submit() after simulate()
            │                       └── Phase 6 (US4 — P3)  ← journal fill events
            │                               ├── Phase 7 (US5 — P4)  ← ai_suggestion_id in journal
            │                               └── Phase 8 (US6 — P4)  ← snapshot logger
            │                                       └── Phase 9 (US7 — P5)  ← journals flatten fills
            └── Phase 10 (Polish)
```

### User Story Dependencies

| Story    | Depends On                                               | Can Start After       |
| -------- | -------------------------------------------------------- | --------------------- |
| US1 (P1) | Phase 2 only                                             | Foundational complete |
| US2 (P2) | US1 (BetaWeighter used for post-trade Greeks)            | US1 complete          |
| US3 (P3) | US2 (simulate() before submit())                         | US2 complete          |
| US4 (P3) | US3 (fill events trigger journaling)                     | US3 complete          |
| US5 (P4) | US1 (PortfolioGreeks), US4 (ai_suggestion_id in journal) | US4 complete          |
| US6 (P4) | US1 (PortfolioGreeks for snapshots), US4 (same DB)       | US4 complete          |
| US7 (P5) | US3 (submit()), US4 (journal fills)                      | US4 complete          |

### Within Each User Story

1. Write tests FIRST → confirm they FAIL
2. Implement models → services → UI components → integration
3. Tests must PASS before marking story complete
4. Commit at each checkpoint

### Parallel Opportunities Per Phase

**Phase 2 (Foundational)**:

- T007 `models/order.py` and T008 `models/unified_position.py` — different files [P]
- T009 `trade_journal` table and T010 `account_snapshots` table — same file, sequential

**Phase 3 (US1)**:

- T012 `test_beta_weighter.py` can start in parallel with T013–T015
- T016 ibkr_adapter.py and T017 tastytrade_adapter.py integrations [P]

**Phase 6 (US4)**:

- T037 `record_fill()`, T038 `query_journal()`, T039 `export_csv()` — sequential (same file)
- T043 `trade_journal_view.py` component can be built while T040–T042 are in progress

**Phase 8 (US6)**:

- T058 `capture_snapshot()` and T059 `query_snapshots()` sequential (same file)
- T062 account/delta chart and T063 delta/theta chart [P] — different render functions

---

## Parallel Example: Phase 3 (User Story 1)

```bash
# Start tests in parallel with model implementation:
Task T012: "Write tests/test_beta_weighter.py (mock Tastytrade + yfinance)"
Task T013: "Implement risk_engine/beta_weighter.py BetaWeighter class"

# Once BetaWeighter complete, integrate adapters in parallel:
Task T016: "Integrate BetaWeighter into adapters/ibkr_adapter.py"
Task T017: "Integrate BetaWeighter into adapters/tastytrade_adapter.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only — SPX Delta Display)

1. Complete Phase 1: Setup (T001–T004)
2. Complete Phase 2: Foundational (T005–T011) — **CRITICAL, blocks everything**
3. Complete Phase 3: User Story 1 (T012–T020)
4. **STOP and VALIDATE**: Compare dashboard SPX Equivalent Delta against IBKR Risk Navigator — must be within ±5 points (SC-001)
5. Demo/validate with real portfolio data before continuing

### Incremental Delivery

| Milestone     | Stories Complete | Value Delivered                                    |
| ------------- | ---------------- | -------------------------------------------------- |
| MVP           | US1              | Trusted Greek display — stop cross-checking broker |
| Safety Gate   | US1 + US2        | Pre-trade margin simulation — confident sizing     |
| Read-Write    | US1–US3          | Full execution terminal                            |
| Audit Trail   | US1–US4          | Complete trade journal                             |
| AI Copilot    | US1–US5          | Automated risk suggestions                         |
| Analytics     | US1–US6          | Longitudinal risk tracking                         |
| Full Platform | US1–US7          | Emergency flatten added                            |

### Key Risk: US1 accuracy gate

If the SPX Equivalent Delta at US1 checkpoint does not match the broker within ±5 points, **do not proceed to US2**. Debug using `quickstart.md` comparison test. Common causes: positions not yet loaded (stale cache), SPX price stale, multiplier error on new instrument type.

---

## Notes

- [P] tasks = different files or independent functions, no dependencies on incomplete tasks in same phase
- [US*] label maps each task to its user story for traceability and independent testing
- IBKR WhatIf API does NOT return Greeks — post-trade Greeks must be computed manually via `BetaWeighter` on current positions + order legs (Research R-003)
- Tastytrade Session auth uses `Session(provider_secret=TT_SECRET, refresh_token=TT_REFRESH)` — not username/password (Research R-002)
- MES beta must be exactly 1.0 (economic relationship), not 0.986 statistical; this is fixed in T002
- Three existing adapter bugs fixed in Phase 2 (T005, T006) before any story work begins
- Streamlit UI components tested manually per Constitution §I Dashboard Testing — not in automated test suite
- All background tasks use asyncio (not threading) — consistent with existing codebase pattern
- Commit after each checkpoint; do not stack multiple phases in a single commit
