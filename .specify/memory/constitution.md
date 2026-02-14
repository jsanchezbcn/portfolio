# Portfolio Risk Manager Constitution

## Core Principles

### I. Test-First Development (MANDATORY)

**Unit Testing Requirements:**

- All business logic MUST have unit tests (models, risk engine, adapters, portfolio tools)
- Tests MUST be written BEFORE or ALONGSIDE implementation
- Minimum 80% code coverage for core modules (models/, risk_engine/, adapters/, agent_tools/)
- Tests MUST use fixtures and mocking for external dependencies

**IBKR Testing Strategy (Authentication Challenge):**

- IBKR Client Portal API requires manual browser authentication → CANNOT be fully automated
- MUST use `.portfolio_snapshot.json` fixture files for adapter unit tests
- MUST mock `IBKRClient` methods when testing `IBKRAdapter`
- Integration tests with live IBKR gateway are OPTIONAL, marked with `@pytest.mark.manual`

**Tastytrade Testing Strategy:**

- Tastytrade SDK supports programmatic authentication → CAN be automated
- Unit tests MUST mock SDK responses using fixtures
- Integration tests with live Tastytrade API are OPTIONAL, marked with `@pytest.mark.integration`
- Integration tests require credentials in `.env` (never commit)

**Test Organization:**

- Unit tests: `tests/test_*.py` (fast, no network, no credentials)
- Integration tests: `tests/integration/test_*_integration.py` (slow, network, credentials)
- Fixtures: `tests/fixtures/*.json` (mock position data, VIX data, etc.)
- Shared fixtures: `tests/conftest.py` (pytest fixtures, mock clients)

**Dashboard Testing:**

- Streamlit dashboard (`dashboard/app.py`) is tested MANUALLY via visual inspection
- Business logic (PortfolioTools, MarketDataTools) extracted and unit tested separately
- NO automated UI tests required for MVP (complex setup, low ROI)

### II. Adapter Pattern (STRICT ENFORCEMENT)

**All broker integrations MUST:**

1. Inherit from `BrokerAdapter` abstract base class
2. Implement `fetch_positions(account_id) -> List[UnifiedPosition]`
3. Implement `fetch_greeks(positions) -> List[UnifiedPosition]`
4. Transform broker-specific data to `UnifiedPosition` format
5. Handle errors gracefully (network failures, missing data)

**Adapter Responsibilities:**

- Position-level Greeks MUST be multiplied by quantity BEFORE creating UnifiedPosition
- SPX-weighted delta calculation MUST use BetaConfig
- Option details (underlying, strike, expiration, type) MUST be extracted consistently
- DTE (days to expiration) MUST be calculated at fetch time

**Extending with New Brokers:**

- Create new adapter class: `adapters/{broker}_adapter.py`
- Implement BrokerAdapter interface
- Add unit tests: `tests/test_{broker}_adapter.py`
- Update dashboard broker selector

### III. Trading Literature Principles (NON-NEGOTIABLE)

These principles from quantitative trading books are CONSTITUTIONAL:

**Natenberg ("Option Volatility and Pricing"):**

- IV vs HV comparison MUST be used for edge analysis (US7)
- If IV > HV → selling edge (overpriced options)
- If IV < HV → buying edge (underpriced options)
- US7 MUST display this analysis with clear recommendations

**Sebastian ("The Option Trader's Hedge Fund"):**

- Theta/Vega ratio target: 1:3 (i.e., |Theta| / |Vega| ≈ 0.33)
- Formula MUST use absolute values: `abs(portfolio_theta) / abs(portfolio_vega)`
- Green zone: 0.25 <= ratio <= 0.40
- Red zone: ratio < 0.20 or ratio > 0.50
- US3 MUST implement this visualization

**Taleb ("Dynamic Hedging"):**

- Gamma risk increases exponentially near expiration
- 0-7 DTE positions MUST be flagged when |net_gamma| > 5.0
- Threshold of 5.0 is HARDCODED for MVP (portfolio-level net gamma per bucket)
- US2 MUST display warning: "⚠️ High gamma in 0-7 DTE bucket. Taleb warns: 'Gamma risk explodes near expiration.'"

**Passarelli ("Trading Options Greeks") — DEFERRED:**

- Synthetic position analysis is OUT OF SCOPE for MVP
- May be added in future as advanced AI agent capability
- If implemented: synthetics to adjust Delta without disrupting Theta
- REMOVED from current requirements (addressed finding G1)

### IV. Security Requirements (MANDATORY)

**Credentials:**

- NO hardcoded credentials anywhere in source code
- ALL API credentials MUST use environment variables
- MUST provide `.env.example` template (no actual secrets)
- `.env` MUST be in `.gitignore`

**Broker Authentication:**

- IBKR: Uses Client Portal Gateway (manual browser login, session-based)
- Tastytrade: Uses SDK programmatic auth (username/password from env)
- Polymarket: Public API (no authentication required)

**Sensitive Data:**

- Position snapshots (`.portfolio_snapshot.json`) MAY contain PII → add to `.gitignore`
- Greeks cache (`.tastytrade_cache.pkl`) is safe to track (no PII)
- Never log credentials or account numbers

### V. Graceful Degradation (REQUIRED)

**Broker API Failures:**

- If IBKR gateway unreachable → load from `.portfolio_snapshot.json` + display warning banner
- If Tastytrade SDK fails → set Greeks to 0.0 + log warning + continue
- If Polymarket API fails → use VIX-only regime detection + display "Macro data unavailable"

**Data Staleness:**

- Greeks cache MUST display last-updated timestamp
- Cache expiry: 5 minutes (configurable)
- Stale data (> 10 minutes) MUST show orange warning indicator

**Error Handling:**

- Never crash on missing data → substitute with safe defaults (0.0 for Greeks, 'N/A' for strings)
- Display user-friendly error messages in dashboard
- Log detailed errors for debugging

## Performance Standards

**Dashboard Load Time:**

- **Definition**: Time from page render to full interactivity (all cards populated, charts drawn)
- **Cached mode**: Using `.portfolio_snapshot.json` + `.tastytrade_cache.pkl` → MUST load in < 3 seconds
- **Fresh mode**: Live IBKR API calls → acceptable up to 10 seconds
- **Measurement**: Use browser DevTools Performance tab or `@st.cache_resource` stats

**Greeks Calculation:**

- < 1 second for portfolio with 100 positions
- Calculation includes: aggregation, DTE bucketing, Theta/Vega ratio

**Risk Checks:**

- Regime detection + limit comparison: < 500ms

## Data Accuracy Standards

**Greeks Accuracy:**

- **Source**: Tastytrade SDK Greeks (NOT IBKR, which returns 'N/A')
- **Target**: Position-level Greeks accurate to 2 decimal places (e.g., delta 0.65)
- **Portfolio aggregation**: Sum of position Greeks accurate to 1 decimal place
- **Staleness**: Greeks cache < 5 minutes old preferred, display timestamp always

**Position Data:**

- Quantity, price, P&L: Match broker values exactly
- SPX-weighted delta: Calculated using beta coefficients from `beta_config.json`

## Development Workflow

**MVP-First Approach:**

1. Implement User Story 1 (P1) completely → validate → demo
2. Iteratively add P2, P3, P4 based on user feedback
3. P5-P7 are optional enhancements

**Dashboard Architecture:**

- MVP: Single-file `dashboard/app.py` (simpler, faster to build)
- Future refactor: Extract to `dashboard/components/*.py` if complexity grows
- Justification: Component files premature for MVP; adds indirection without benefit

**Task Execution:**

- Complete Phase 1 (Setup) + Phase 2 (Foundational) BEFORE any user story
- User stories CAN be parallelized after Phase 2 (if multiple developers)
- Run tests after each phase

**Code Review Gates:**

- All code MUST pass unit tests before merge
- Manual dashboard validation required for UI changes
- Constitution compliance checked in PR review

## Configuration Management

**Regime Configuration** (`config/risk_matrix.yaml`):

- All regime limits MUST be defined in YAML (not hardcoded)
- Polymarket recession probability threshold: configurable per regime
- Gamma threshold per DTE bucket: configurable in future (MVP uses hardcoded 5.0)

**Beta Coefficients** (`beta_config.json`):

- Existing file from ibkr_portfolio_client.py
- Used for SPX-weighted delta calculation
- Maintain backward compatibility

## Governance

This constitution supersedes conflicting guidance in spec.md, plan.md, or tasks.md.

**Amendment Process:**

1. Identify constitutional conflict in implementation
2. Document proposed amendment with rationale
3. Update constitution BEFORE implementing change
4. Update affected spec/plan/tasks documents

**Enforcement:**

- All PRs must reference constitution sections for testing, security, adapter pattern
- Reviewer MUST verify constitution compliance
- Trading principles (Natenberg, Sebastian, Taleb) are NON-NEGOTIABLE

**Version**: 1.0.0 | **Ratified**: February 12, 2026 | **Last Amended**: February 12, 2026
