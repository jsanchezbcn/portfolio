# Feature Specification: Portfolio Risk Management System

**Feature Branch**: `001-portfolio-risk-manager`  
**Created**: February 12, 2026  
**Status**: Draft  
**Input**: AI-assisted portfolio management using existing IBKR, Tastytrade access, with future Polymarket integration. Risk-first approach based on Natenberg, Sebastian, Taleb, and Passarelli principles.

## User Scenarios & Testing

### User Story 1 - View Portfolio Risk Summary (Priority: P1)

As a portfolio manager, I need to see my current portfolio risk metrics (Greeks, regime status, and limit compliance) in a single dashboard so I can quickly assess if my portfolio is within acceptable risk parameters.

**Why this priority**: This is the foundational MVP - without visibility into current risk, no other risk management actions are possible. This delivers immediate value by consolidating data from multiple brokers into one unified view.

**Independent Test**: Can be fully tested by connecting to IBKR, fetching positions, and displaying aggregated Greeks (Delta, Gamma, Theta, Vega) along with regime detection (based on VIX). Success criteria: Dashboard shows accurate totals matching broker data.

**Acceptance Scenarios**:

1. **Given** user has positions in IBKR, **When** dashboard loads, **Then** display total Delta, Gamma, Theta, Vega, SPX-weighted Delta, and current market regime
2. **Given** positions contain options, **When** calculating Greeks, **Then** aggregate position-level Greeks correctly (quantity × per-contract Greek)
3. **Given** current VIX level, **When** regime detection runs, **Then** correctly identify regime (Low/Neutral/High/Crisis) and display appropriate limits
4. **Given** portfolio exceeds regime limits, **When** risk check runs, **Then** highlight violations with red alerts

---

### User Story 2 - Monitor Gamma Risk by Expiration (Priority: P2)

As a portfolio manager following Taleb's principles, I need to see my gamma exposure grouped by days-to-expiration (0-7, 8-30, 31-60, 60+) so I can identify dangerous short-dated gamma concentration before it explodes.

**Why this priority**: Gamma risk is the most time-sensitive risk in options portfolios. After achieving visibility (P1), managing gamma is the next critical safety check.

**Independent Test**: Can be tested by creating positions with various DTEs and verifying they are bucketed correctly. Success criteria: Positions grouped accurately, 0-7 DTE bucket flagged red when gamma > 5.

**Acceptance Scenarios**:

1. **Given** option positions with various expirations, **When** gamma heatmap renders, **Then** group positions into correct DTE buckets
2. **Given** high gamma in 0-7 DTE bucket, **When** threshold exceeded (|gamma| > 5), **Then** display bucket in red with Taleb warning
3. **Given** positions approaching expiration, **When** DTE calculation runs, **Then** update bucket assignment dynamically as time passes

---

### User Story 3 - Track Theta/Vega Ratio (Priority: P3)

As a portfolio manager following Sebastian's Insurance Model, I need to visualize my Theta/Vega ratio on a chart with target zones so I can ensure I'm collecting enough premium relative to my volatility risk.

**Why this priority**: This is an optimization metric for existing positions. Important for profitability, but not critical for risk management like P1 and P2.

**Independent Test**: Can be tested by plotting portfolio on Theta/Vega scatter chart. Success criteria: Chart shows green zone (0.25 <= |Theta|/|Vega| <= 0.40), red zone (ratio < 0.20 or > 0.50), and current portfolio position.

**Acceptance Scenarios**:

1. **Given** portfolio with Theta and Vega values, **When** chart renders, **Then** plot current position on Theta/Vega axes
2. **Given** target ratio of 1:3 (i.e., |Theta| / |Vega| ≈ 0.33), **When** calculating zones, **Then** draw green zone where 0.25 <= abs(Theta) / abs(Vega) <= 0.40
3. **Given** portfolio outside target zone, **When** position plotted, **Then** show in context with recommended adjustment direction

**Note on Sign Convention**: Short premium portfolios typically have positive Theta (time decay profit) and negative Vega (short volatility). The ratio MUST use absolute values to ensure correct calculation.

---

### User Story 4 - Regime-Based Position Recommendations (Priority: P4)

As a portfolio manager, I need AI-powered suggestions for portfolio adjustments based on current regime and risk violations so I can take corrective action without manual calculations.

**Why this priority**: This adds intelligence on top of the monitoring features. Valuable but requires P1-P3 infrastructure first.

**Independent Test**: Can be tested by asking AI agent questions and verifying responses include regime context, current risk metrics, and specific recommendations. Success criteria: Agent provides actionable suggestions with rationale.

**Acceptance Scenarios**:

1. **Given** portfolio exceeds delta limits, **When** user asks for adjustment, **Then** AI suggests specific delta-reducing trades
2. **Given** high Theta/Vega ratio, **When** user requests analysis, **Then** AI explains variance from Sebastian's 1:3 target and suggests corrections
3. **Given** regime change from Low to High volatility, **When** detected, **Then** AI proactively alerts user to adjust positioning per new limits

---

### User Story 5 - Multi-Broker Position Aggregation (Priority: P5)

As a portfolio manager with accounts at IBKR and Tastytrade, I need to see combined risk metrics across both brokers so I understand my total exposure across all accounts.

**Why this priority**: Important for users with multiple brokers, but MVP can work with single broker. Can be added incrementally.

**Independent Test**: Can be tested by loading positions from both IBKR and Tastytrade adapters and verifying unified Greeks calculations. Success criteria: Total Greeks match sum of individual broker Greeks.

**Acceptance Scenarios**:

1. **Given** positions in IBKR and Tastytrade, **When** both adapters fetch data, **Then** normalize to UnifiedPosition format
2. **Given** normalized positions, **When** aggregating Greeks, **Then** calculate portfolio-level totals correctly
3. **Given** multiple accounts, **When** displaying breakdown, **Then** allow filtering by broker source

---

### User Story 6 - Polymarket Macro Integration (Priority: P6)

As a portfolio manager, I need to incorporate forward-looking macro probabilities from Polymarket (e.g., recession odds) into regime detection so I can adjust risk before market volatility spikes.

**Why this priority**: Advanced feature that enhances regime detection. Not critical for core functionality.

**Independent Test**: Can be tested by fetching Polymarket recession probability and verifying it triggers High Volatility regime when >40% even if VIX is moderate. Success criteria: Macro data overrides pure VIX-based regime detection.

**Acceptance Scenarios**:

1. **Given** Polymarket recession probability > 40%, **When** regime detection runs, **Then** override to High Volatility regime regardless of current VIX
2. **Given** Polymarket API unavailable, **When** macro check fails, **Then** fall back to VIX-only regime detection with warning
3. **Given** macro probability data, **When** displaying regime, **Then** show contributing factors (VIX + macro indicators)

---

### User Story 7 - IV vs HV Analysis (Priority: P7)

As a portfolio manager following Natenberg's principles, I need to compare implied volatility to historical volatility for my positions so I can identify overpriced (selling opportunities) or underpriced (buying opportunities) options.

**Why this priority**: Strategic analysis for trade selection. Important but not critical for risk management.

**Independent Test**: Can be tested by calculating HV from price history and comparing to IV from options chain. Success criteria: Clearly shows which positions have IV > HV (sell candidates) and IV < HV (buy candidates).

**Acceptance Scenarios**:

1. **Given** option positions with IV data, **When** fetching historical prices, **Then** calculate 30-day HV for underlying
2. **Given** IV and HV values, **When** comparing, **Then** flag positions where IV > HV (selling edge) or IV < HV (buying edge)
3. **Given** volatility analysis, **When** displaying results, **Then** show Natenberg principle application for each position

---

## Technical Requirements

### Data Model

**UnifiedPosition Class**: Normalized position format across all brokers

- Identity: symbol, instrument_type, broker
- Sizing: quantity, avg_price, market_value, unrealized_pnl
- Greeks: delta, gamma, theta, vega, spx_delta
- Options: underlying, strike, expiration, option_type, iv, days_to_expiration
- DTE bucketing: 0-7, 8-30, 31-60, 60+ for gamma risk monitoring

### Risk Engine

**RegimeDetector**: Market state detection based on VIX, term structure, and macro data

- Regimes: Low Volatility (VIX < 15), Neutral (15-22), High (> 22), Crisis (> 35)
- Per-regime limits: max_beta_delta, max_negative_vega, min_daily_theta, max_gamma
- Configuration: YAML-based rules (config/risk_matrix.yaml)

### Data Sources

1. **IBKR**: Existing integration via ibkr_portfolio_client.py
2. **Tastytrade**: Existing integration via tastytrade_options_fetcher.py
3. **Market Data**: VIX, VIX3M via yfinance
4. **Polymarket**: Recession probabilities via REST API (future)

### AI Agent Tools

Functions exposed to GitHub Copilot for portfolio analysis:

- `get_portfolio_summary()`: Aggregated Greeks and Theta/Vega ratio
- `check_risk_limits(vix, term_structure)`: Regime compliance check
- `get_gamma_risk_by_dte()`: Gamma exposure by expiration buckets
- `get_vix_data()`: Current VIX and term structure
- `get_macro_indicators()`: Polymarket probabilities

### Visualization

Streamlit dashboard with:

- Regime status banner with VIX data
- Portfolio summary cards (Delta, Theta, Vega, Gamma)
- Gamma risk heatmap by DTE (Taleb framework)
- Theta/Vega scatter plot (Sebastian framework)
- Risk compliance table with violations
- AI assistant chat interface

---

## Non-Functional Requirements

- **Performance**: Dashboard load < 3 seconds from render to interactivity when using cached data (`.portfolio_snapshot.json` + `.tastytrade_cache.pkl`). Fresh API calls may take up to 10 seconds.
- **Accuracy**: Position-level Greeks from Tastytrade SDK accurate to 2 decimal places. Portfolio aggregations accurate to 1 decimal place. Note: IBKR API returns 'N/A' for Greeks; all Greeks sourced from Tastytrade cache.
- **Data Staleness**: Greeks cache timestamp MUST be displayed. Cache expiry: 5 minutes. Data > 10 minutes old shows orange warning.
- **Reliability**: Graceful degradation if broker API unavailable (load from snapshot + display warning banner)
- **Extensibility**: Easy to add new broker adapters via BrokerAdapter ABC
- **Security**: No credentials stored in code; use environment variables. `.env` in `.gitignore`. Position snapshots MAY contain PII, also in `.gitignore`.

---

## Out of Scope

- Automated trade execution (view-only system)
- Backtesting historical strategies
- Real-time streaming quotes (snapshot-based)
- Multi-user/multi-portfolio support
- Mobile app (web dashboard only)
- Passarelli's synthetic position analysis (advanced feature, deferred to future releases)
- Automated UI testing for Streamlit dashboard (manual validation only for MVP)
