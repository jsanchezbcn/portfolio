# Tasks: Portfolio Risk Management System

**Input**: Design documents from `/specs/001-portfolio-risk-manager/`
**Prerequisites**: plan.md, spec.md, data-model.md, contracts/broker_adapter.md, constitution.md

**Tests**: Unit tests are NOW REQUIRED per constitution and user request. Test tasks are integrated into each phase. See constitution for testing strategy:

- **IBKR**: Cannot automate (requires manual browser login). Use fixture files + mock IBKRClient.
- **Tastytrade**: Can automate with credentials. Mock SDK for unit tests, optional integration tests with `@pytest.mark.integration`.
- **Dashboard**: Manual validation only (no automated UI tests for MVP).
- **Business Logic**: Full unit test coverage required (models, risk_engine, adapters, agent_tools).

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [x] T001 Create directory structure (models/, risk_engine/, adapters/, agent_tools/, dashboard/, dashboard/components/, config/, tests/, tests/fixtures/, tests/integration/)
- [x] T002 [P] Update requirements.txt with new dependencies (streamlit, plotly, pydantic, aiohttp, pyyaml, yfinance, pytest, pytest-asyncio, pytest-mock)
- [x] T003 [P] Create config/risk_matrix.yaml with regime definitions per plan.md (includes configurable Polymarket thresholds and gamma limits)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

### Core Data Models & Risk Engine

- [x] T005 Create InstrumentType enum in models/unified_position.py
- [x] T006 Create UnifiedPosition Pydantic model in models/unified_position.py with all fields per data-model.md (include validation: if instrument_type==OPTION, require underlying/strike/expiration/option_type)
- [x] T007 Implement dte_bucket property in UnifiedPosition (0-7, 8-30, 31-60, 60+ logic)
- [x] T008 Create RegimeLimits dataclass in risk_engine/regime_detector.py
- [x] T009 Create MarketRegime dataclass in risk_engine/regime_detector.py
- [x] T010 Implement RegimeDetector class with YAML config loading in risk_engine/regime_detector.py
- [x] T011 Implement RegimeDetector.detect_regime() method with VIX/Polymarket logic (use thresholds from YAML config)
- [x] T012 Create BrokerAdapter abstract base class in adapters/base_adapter.py per contract (fetch_positions, fetch_greeks methods)

### Package Initialization (merged from T003)

- [x] T013 Create models/**init**.py with exports (UnifiedPosition, InstrumentType)
- [x] T014 Create risk_engine/**init**.py with exports (RegimeDetector, MarketRegime, RegimeLimits)
- [x] T015 Create adapters/**init**.py with exports (BrokerAdapter)
- [x] T016 Create agent_tools/**init**.py (empty for now, exports added in US1)
- [x] T017 Create dashboard/**init**.py (empty, dashboard is standalone script)

### Unit Test Infrastructure (NEW - Required per constitution)

- [x] TT01 [P] Create tests/conftest.py with shared pytest fixtures (mock_ibkr_client, mock_vix_data, mock_regime_config)
- [x] TT02 [P] Create tests/fixtures/mock_ibkr_positions.json from real .portfolio_snapshot.json (anonymize account IDs, use sample data)
- [x] TT03 [P] Create tests/fixtures/mock_tastytrade_positions.json with sample Tastytrade position data
- [x] TT04 [P] Create tests/fixtures/mock_vix_data.json with sample VIX/VIX3M data for regime testing
- [x] TT05 Write tests/test_unified_position.py — test UnifiedPosition validation (required fields, option validation, defaults, dte_bucket property)
- [x] TT06 Write tests/test_regime_detector.py — test all 4 regimes (Low/Neutral/High/Crisis with boundary VIX values, Polymarket override logic)
- [x] TT07 [P] Add pytest.ini or pyproject.toml [tool.pytest.ini_options] with test markers (unit, integration, manual)

**Checkpoint**: Foundation ready - user story implementation can now begin in parallel

---

## Phase 3: User Story 1 - View Portfolio Risk Summary (Priority: P1) 🎯 MVP

**Goal**: Display current portfolio risk metrics (Greeks, regime status, limit compliance) in a dashboard

**Independent Test**: Connect to IBKR, fetch positions, display aggregated Greeks and regime detection. Dashboard shows accurate totals matching broker data.

### Implementation for User Story 1

#### Adapters & Business Logic

- [x] T018 [P] [US1] Create IBKRAdapter class in adapters/ibkr_adapter.py and implement fetch_positions() to wrap existing ibkr_portfolio_client.IBKRClient (merged from old T016+T018)
- [x] T019 [US1] Implement IBKRAdapter position-to-UnifiedPosition transformation logic (handle both equity and option positions)
- [x] T020 [US1] Implement IBKRAdapter option details extraction (underlying, strike, expiration, DTE calculation using \_extract_option_details from IBKRClient)
- [x] T021 [US1] Implement IBKRAdapter.fetch_greeks() using existing get_tastytrade_option_greeks cache (multiply per-contract Greeks × quantity per constitution)
- [x] T022 [US1] Implement IBKRAdapter SPX delta calculation using existing calculate_spx_weighted_delta method
- [x] T023 [P] [US1] Create PortfolioTools class in agent_tools/portfolio_tools.py with **init**
- [x] T024 [US1] Implement PortfolioTools.get_portfolio_summary() method (aggregate Greeks using abs() for Theta/Vega ratio per constitution)
- [x] T025 [US1] Implement PortfolioTools.check_risk_limits() method (compare portfolio vs regime limits, return violations list)
- [x] T026 [P] [US1] Create MarketDataTools class in agent_tools/market_data_tools.py
- [x] T027 [US1] Implement MarketDataTools.get_vix_data() using yfinance (^VIX, ^VIX3M, calculate term structure)
- [x] T028 [US1] Implement MarketDataTools.get_spx_data() using yfinance (^GSPC, 30-day realized volatility)

#### Dashboard Implementation (Single-File Approach per Constitution)

- [x] T029 [P] [US1] Create Streamlit app structure in dashboard/app.py with imports and page config
- [x] T030 [US1] Implement dashboard sidebar with account selector and refresh button in dashboard/app.py
- [x] T031 [US1] Implement position fetching logic with IBKRAdapter in dashboard/app.py (with error handling and cached initialization)
- [x] T032 [US1] Implement regime detection display (banner with color coding: green/blue/orange/red) in dashboard/app.py
- [x] T033 [US1] Implement portfolio summary cards (Delta, Theta, Vega, Gamma, Theta/Vega ratio with zone indicators) in dashboard/app.py
- [x] T034 [US1] Implement risk compliance table showing limit violations in dashboard/app.py
- [x] T035 [US1] Add VIX data display (current VIX, term structure, backwardation status) in dashboard/app.py
- [x] T036 [US1] Add error handling, loading spinners, and cached component initialization (@st.cache_resource) in dashboard/app.py
- [x] T037 [US1] Add data staleness indicators (timestamp display, orange warning if Greeks > 10 mins old) in dashboard/app.py

#### Unit Tests for US1 (NEW)

- [x] TT08 [US1] Write tests/test_ibkr_adapter.py — mock IBKRClient, test position transformation, option extraction, Greeks mapping, SPX delta calculation
- [x] TT09 [US1] Write tests/test_portfolio_tools.py — test summary aggregation (Greeks sum, Theta/Vega ratio with abs()), risk limit checks, violation detection
- [x] TT10 [US1] Write tests/test_market_data_tools.py — mock yfinance Ticker objects, test VIX data parsing, term structure calculation

**Checkpoint**: At this point, User Story 1 should be fully functional - dashboard displays portfolio risk with regime-based limits

---

## Phase 4: User Story 2 - Monitor Gamma Risk by Expiration (Priority: P2)

**Goal**: Display gamma exposure grouped by DTE buckets (0-7, 8-30, 31-60, 60+) with Taleb's warnings

**Independent Test**: Create positions with various DTEs, verify bucketing accuracy, confirm 0-7 DTE bucket flagged red when |gamma| > 5.0 (portfolio-level threshold per constitution)

### Implementation for User Story 2

- [x] T038 [US2] Implement PortfolioTools.get_gamma_risk_by_dte() method in agent_tools/portfolio_tools.py (group by dte_bucket, sum gamma per bucket)
- [x] T039 [P] [US2] Create gamma heatmap chart component in dashboard/app.py (Plotly bar chart)
- [x] T040 [US2] Implement Plotly bar chart with DTE buckets on x-axis, gamma on y-axis
- [x] T041 [US2] Add color coding logic (red for 0-7 DTE if |gamma| > 5.0, orange for 8-30, green for 31+)
- [x] T042 [US2] Add Taleb warning message when 0-7 DTE gamma exceeds threshold: "⚠️ High gamma in 0-7 DTE bucket. Taleb warns: 'Gamma risk explodes near expiration.'"
- [x] T043 [US2] Integrate gamma heatmap into dashboard/app.py below risk compliance table

**Checkpoint**: Gamma risk visualization complete and independently functional

---

## Phase 5: User Story 3 - Track Theta/Vega Ratio (Priority: P3)

**Goal**: Visualize Theta/Vega ratio on scatter chart with target zones per Sebastian's Insurance Model

**Independent Test**: Plot portfolio on chart, verify green zone (0.25 <= |Theta|/|Vega| <= 0.40), red zone visible, current position marked

### Implementation for User Story 3

- [x] T044 [P] [US3] Create Theta/Vega scatter plot component in dashboard/app.py
- [x] T045 [US3] Implement Plotly scatter chart with Vega on x-axis, Theta on y-axis
- [x] T046 [US3] Add green zone overlay (rectangle where 0.25 <= abs(Theta) / abs(Vega) <= 0.40 per Sebastian's 1:3 ratio)
- [x] T047 [US3] Add red zone overlay (rectangle where ratio < 0.20 or > 0.50 - poor premium collection)
- [x] T048 [US3] Plot current portfolio position as blue marker with label showing actual ratio
- [x] T049 [US3] Add target ratio reference line (1:3 dashed line representing abs(Theta) / abs(Vega) = 0.33)
- [x] T050 [US3] Integrate Theta/Vega chart into dashboard/app.py below gamma heatmap

**Checkpoint**: Theta/Vega visualization complete and showing Sebastian's framework zones correctly

---

## Phase 6: User Story 4 - Regime-Based Position Recommendations (Priority: P4)

**Goal**: AI-powered suggestions for portfolio adjustments using GitHub Copilot integration

**Independent Test**: Ask AI questions, verify responses include regime context, risk metrics, and actionable recommendations

### Implementation for User Story 4

- [x] T051 [P] [US4] Create agent_config.py with AGENT_SYSTEM_PROMPT explaining Natenberg, Sebastian, Taleb principles and tool schemas (JSON format) for GitHub Copilot function calling (merged from old T050+T060)
- [x] T052 [US4] Add get_portfolio_summary tool schema in agent_config.py
- [x] T053 [US4] Add check_risk_limits tool schema in agent_config.py
- [x] T054 [US4] Add get_gamma_risk_by_dte tool schema in agent_config.py
- [x] T055 [US4] Add get_vix_data tool schema in agent_config.py
- [x] T056 [US4] Add suggest_adjustment tool schema in agent_config.py
- [x] T057 [US4] Implement AI assistant chat interface (text input) in dashboard/app.py
- [x] T058 [US4] Add placeholder AI response logic showing available context (regime, risk status) in dashboard/app.py
- [x] T059 [US4] Document GitHub Copilot integration approach in agent_config.py comments
- [x] T060 [US4] Implement proactive regime-change alerting mechanism: detect regime transitions between dashboard refreshes and display prominent alert banner (NEW - addresses finding I2)

**Checkpoint**: AI agent interface complete with tool schemas ready for Copilot integration, plus proactive alerts

### Unit Tests for US5 (NEW)

- [x] TT11 [US5] Write tests/test_tastytrade_adapter.py — mock Tastytrade SDK Account/Session, test position transformation, Greeks extraction
- [x] TT12 [US5] Write tests/integration/test_tastytrade_integration.py marked @pytest.mark.integration — real SDK call with credentials from .env (optional, requires Tastytrade account)

**Checkpoint**: Multi-broker aggregation functional, totals accurate across both sources

---

## Phase 8: User Story 6 - Polymarket Macro Integration (Priority: P6)

**Goal**: Incorporate forward-looking macro probabilities from Polymarket into regime detection

**Independent Test**: Fetch Polymarket data, verify High Volatility regime triggered when recession prob > 40% even if VIX moderate

### Implementation for User Story 6

- [x] T069 [P] [US6] Create PolymarketAdapter class in adapters/polymarket_adapter.py
- [x] T070 [US6] Implement PolymarketAdapter.get_recession_probability() with async HTTP client (aiohttp session)
- [x] T071 [US6] Implement Polymarket CLOB API search for recession markets (search endpoint, filter by year)
- [x] T072 [US6] Parse Polymarket response and extract "Yes" probability from market outcomes
- [x] T073 [US6] Add graceful fallback handling if Polymarket API unavailable (return None, log warning)
- [x] T074 [US6] Update MarketDataTools.get_macro_indicators() to call PolymarketAdapter
- [x] T075 [US6] Update dashboard regime detection to pass Polymarket data to RegimeDetector
- [x] T076 [US6] Display macro indicators (recession probability, source) in regime banner with timestamp

### Unit Tests for US6 (NEW)

- [x] TT13 [US6] Write tests/test_polymarket_adapter.py — mock aiohttp responses, test probability parsing, fallback handling, expired market filtering

**Checkpoint**: Polymarket integration complete, forward-looking signals influence regime detection (T077 removed - logic already in foundational T011)

---

## Phase 9: User Story 7 - IV vs HV Analysis (Priority: P7)

**Goal**: Compare implied volatility to historical volatility following Natenberg's principles (IV > HV ≥ 15% = premium selling edge, IV < HV = premium buying edge)

**Independent Test**: Calculate HV from price history, compare to IV, identify selling/buying edges with color coding

### Implementation for User Story 7

- [x] T078 [P] [US7] Implement PortfolioTools.get_iv_analysis() method in agent_tools/portfolio_tools.py
- [x] T079 [US7] Implement historical volatility calculation (30-day log returns, ±2 trading days tolerance) using yfinance in MarketDataTools
- [x] T080 [US7] Create IV vs HV comparison logic (flag positions where IV > HV ≥ 0.15 [green], IV > HV ≥ 0.10 [light blue], IV < HV [blue])
- [x] T081 [US7] Add IV/HV analysis display section in dashboard/app.py (below risk summary table)
- [x] T082 [US7] Create table showing positions with IV vs HV comparison, color-coded per Natenberg principle
- [x] T083 [US7] Add Natenberg principle explanation text: "IV > HV = sell edge (overpriced premium), IV < HV = buy edge (underpriced premium)"
- [x] T084 [US7] Add summary metric: "Positions with IV > HV: X of Y" at top of IV/HV section

### Unit Tests for US7 (NEW)

- [x] TT14 [US7] Write tests/test_iv_hv_calculator.py — test HV calculation with known price series (mock yfinance), test IV-HV spread thresholds (0.10, 0.15), test missing IV handling (return None gracefully)

### Integration Tests for US7 (NEW)

- [x] TT15 [US7] Test end-to-end IV/HV calculation with real yfinance data for SPX (recorded fixture for reproducibility), verify 30-day window alignment with option expiry

**Checkpoint**: IV vs HV analysis complete, showing Natenberg's volatility mean-reversion framework with quantified thresholds (15% premium selloff edge, 10% moderate edge)

---

## Phase 10: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories, ensure production readiness

- [x] T085 [P] Add comprehensive docstrings to all classes and methods (per constitution: "Every public method MUST have docstring explaining purpose, parameters, return type")
- [x] T086 [P] Create README.md with setup instructions, architecture overview, and testing strategy (IBKR fixture-based, Tastytrade integration tests)
- [x] T087 [P] Create example configuration file (config/risk_matrix.example.yaml) documenting all regime thresholds
- [x] T088 Add error handling for broker API failures across all adapters (per constitution: graceful degradation with user-friendly messages)
- [x] T089 Add timestamp displays for all data refreshes in dashboard (per A1 resolution: show cache age, orange warning if > 10 minutes)
- [x] T090 Optimize dashboard load time with better caching strategy (per constitution: < 3 seconds cached mode measured from render to interactivity)
- [x] T091 [P] Add logging configuration (risk_engine, adapters, dashboard) with log rotation and configurable levels
- [x] T092 [P] Create .env.example file for environment variable configuration (per constitution: document IBKR_USER, IBKR_PASSWORD, TASTYTRADE_USERNAME, TASTYTRADE_PASSWORD, log file paths)
- [x] T093 Add dashboard page title ("Portfolio Risk Manager - {Regime Name}") and favicon configuration
- [x] T094 Code review and refactoring for consistency (naming conventions, type hints, error messages)
- [x] T095 Performance testing with 200 positions (measure load time, Greeks calculation time, verify < 3s cached, < 1s Greeks calc)
- [x] T096 Create quickstart guide showing how to run dashboard (prerequisites, environment setup, first run instructions)
- [x] T097 [NEW] Verify .env in .gitignore and .portfolio_snapshot.json NOT in git (per constitution: credentials and PII exclusion)
- [x] T098 [NEW] Add pre-commit hook or CI check for secret scanning (detect hardcoded credentials, warn on sensitive file commits)

### Final Tests (NEW)

- [x] TT16 [Polish] Write tests/test_end_to_end.py — full dashboard load simulation with mocked IBKR and Tastytrade adapters, verify all metrics calculated, verify performance < 3s threshold

**Checkpoint**: Production-ready codebase with comprehensive documentation, security validation, and performance verification

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **User Stories (Phase 3-9)**: All depend on Foundational phase completion
  - US1 (P1): Can start immediately after Foundational - **MVP PRIORITY**
  - US2 (P2): Can start after Foundational - Independent from US1 but builds on dashboard
  - US3 (P3): Can start after Foundational - Independent from US1/US2
  - US4 (P4): Depends on US1-US3 complete (needs portfolio tools available)
  - US5 (P5): Can start in parallel with US1 (just adds adapter)
  - US6 (P6): Can start after Foundational - Enhances US1 regime detection
  - US7 (P7): Depends on US1 complete + historical data integration
- **Polish (Phase 10)**: Depends on desired user stories being complete

### User Story Dependencies

```
Foundational (Phase 2) ─┬─> US1 (P1) ─┬─> US4 (P4: AI Agent)
                        │              │
                        ├─> US2 (P2) ──┤
                        │              │
                        ├─> US3 (P3) ──┘
                        │
                        ├─> US5 (P5: Multi-broker)
                        │
                        ├─> US6 (P6: Polymarket)
                        │
                        └─> US7 (P7) ──> (needs US1)
```

**Critical Path**: Setup → Foundational → US1 → US4
**MVP Path**: Setup → Foundational → US1 (STOP HERE for initial demo)

### Within Each User Story

- Models/data structures before business logic
- Adapters before dashboard integration
- Core functionality before polish
- Independent validation before moving to next priority

### Parallel Opportunities

**Phase 1 (Setup)**: T002, T003, T004 can run in parallel

**Phase 2 (Foundational)**: Several tasks can be parallelized:

- T005-T007 (UnifiedPosition) parallel with T008-T011 (RegimeDetector)
- T012 (BrokerAdapter) parallel with above
- T013-T015 (init files) can be done at end in parallel

**Phase 3 (US1)**: Major parallel opportunities:

- T016 (IBKRAdapter) parallel with T017 (PortfolioTools) parallel with T025 (MarketDataTools)
- T028 (Dashboard structure) can start while T023-T027 being completed

**Phase 4-9 (US2-US7)**: Each user story is independently implementable after Foundational phase completes

**Phase 10 (Polish)**: T085, T086, T087, T091, T092 can all run in parallel

---

## Parallel Example: User Story 1

```bash
# Launch foundational data structures in parallel:
Task T016: "Create IBKRAdapter class in adapters/ibkr_adapter.py"
Task T017: "Create PortfolioTools class in agent_tools/portfolio_tools.py"
Task T025: "Create MarketDataTools class in agent_tools/market_data_tools.py"
Task T028: "Create Streamlit app structure in dashboard/app.py"

# Then once those are done, implementation tasks can proceed sequentially within each area
```

---

## Implementation Strategy

### MVP First (User Story 1 Only) - RECOMMENDED

1. Complete Phase 1: Setup (~1 hour)
2. Complete Phase 2: Foundational (~4-6 hours) **CRITICAL BLOCKER**
3. Complete Phase 3: User Story 1 (~8-10 hours)
4. **STOP and VALIDATE**: Test dashboard with real IBKR data
5. Demo to user, gather feedback
6. Decision point: continue to US2 or iterate on US1

**MVP Deliverable**: Working dashboard showing portfolio Greeks, regime detection, and risk limit compliance

### Incremental Delivery (Recommended Production Approach)

1. Complete Setup + Foundational → Foundation ready (~4-7 hours)
2. **Milestone 1**: Add User Story 1 (P1) → MVP Dashboard functional (~8-10 hours)
   - Test independently with real data
   - Deploy and use for 1 week
3. **Milestone 2**: Add User Story 2 (P2) → Gamma risk monitoring (~3-4 hours)
   - Test independently
   - Enhances existing dashboard
4. **Milestone 3**: Add User Story 3 (P3) → Theta/Vega analysis (~3-4 hours)
   - Test independently
   - Completes core risk metrics
5. **Milestone 4**: Add User Story 4 (P4) → AI agent integration (~4-6 hours)
   - Requires US1-US3 tools available
   - Adds intelligence layer
6. **Optional**: Add US5-US7 based on user needs

**Total MVP Time**: ~15-20 hours
**Full P1-P4 Time**: ~25-35 hours
**Full P1-P7 Time**: ~40-50 hours

### Parallel Team Strategy

With 2-3 developers after Foundational phase completes:

**Team A (Backend/Data)**:

- US1: IBKRAdapter + PortfolioTools + MarketDataTools
- US5: TastytradeAdapter
- US6: PolymarketAdapter

**Team B (Frontend/Dashboard)**:

- US1: Streamlit dashboard structure + regime banner
- US2: Gamma heatmap component
- US3: Theta/Vega chart component

**Team C (AI/Tools)**:

- US4: Agent configuration + tool schemas
- US7: IV/HV analysis

**Integration Points**: Teams sync after US1 core is complete, then independently proceed on US2-US7

---

## Task Estimation Summary

| Phase                 | Task Count   | Est. Hours | Priority    |
| --------------------- | ------------ | ---------- | ----------- |
| Phase 1: Setup        | 4 tasks      | 1h         | Critical    |
| Phase 2: Foundational | 11 tasks     | 5-6h       | **BLOCKER** |
| Phase 3: US1 (MVP)    | 21 tasks     | 8-10h      | P1          |
| Phase 4: US2          | 6 tasks      | 3-4h       | P2          |
| Phase 5: US3          | 7 tasks      | 3-4h       | P3          |
| Phase 6: US4          | 11 tasks     | 4-6h       | P4          |
| Phase 7: US5          | 8 tasks      | 4-5h       | P5          |
| Phase 8: US6          | 9 tasks      | 3-4h       | P6          |
| Phase 9: US7          | 7 tasks      | 4-5h       | P7          |
| Phase 10: Polish      | 12 tasks     | 4-6h       | Final       |
| **TOTAL**             | **96 tasks** | **40-50h** | -           |

**Recommended MVP** (Setup + Foundation + US1): 32 tasks, ~15-20 hours
**Core Product** (+ US2 + US3 + US4): 53 tasks, ~25-35 hours

---

## Notes

- **[P] marker**: Task can run in parallel with other [P] tasks in same phase (different files, no blocking dependencies)
- **[Story] marker**: Maps task to user story for traceability (US1-US7)
- **Tests excluded**: No test tasks included since not explicitly requested in spec
- **Existing code reuse**: Many tasks wrap existing IBKR/Tastytrade clients (not rebuilding from scratch)
- **Independent user stories**: Each US2-US7 can be tested independently after US1 complete
- **Commit strategy**: Commit after each task or logical group of 2-3 related tasks
- **Validation checkpoints**: Stop after each phase to validate before proceeding
- **MVP-first recommended**: Focus on US1 (P1) first for fastest time-to-value
