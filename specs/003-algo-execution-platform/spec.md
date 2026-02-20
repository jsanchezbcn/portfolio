# Feature Specification: Read-Write Algorithmic Execution & Journaling Platform

**Feature Branch**: `003-algo-execution-platform`  
**Created**: 2026-02-19  
**Status**: Draft

## Overview

Upgrade the existing read-only Portfolio Risk Management Dashboard into a fully operational read-write algorithmic execution and journaling platform. The system must accurately reflect true SPX-equivalent portfolio Greeks (matching broker displays), support multi-leg order entry with pre-trade margin simulation, persist a complete trade journal, provide AI-generated risk remediation suggestions, and present performance/risk analytics over time.

This feature builds directly on the foundation established in:

- `001-portfolio-risk-manager` — risk matrix, regime detection, unified position model
- `001-streaming-greeks-database` — live Greeks streaming and storage
- `002-ai-trading-agent` — LLM integration, news sentry, arb hunter

## User Scenarios & Testing _(mandatory)_

### User Story 1 - Accurate SPX Delta Display (Priority: P1)

As a portfolio manager, I want the dashboard to show a single "SPX Equivalent Delta" number that matches what I see on my IBKR and Tastytrade broker platforms, so I can trust the dashboard as the primary risk interface and stop cross-checking manually.

**Why this priority**: The entire platform's credibility depends on Greeks accuracy. All downstream features (order sizing, AI suggestions, risk limits) are invalid if aggregate Delta is wrong. This is the foundational fix.

**Independent Test**: With API connections live, load the dashboard and compare the displayed "SPX Equivalent Delta" value against the same number visible on the IBKR Risk Navigator. They must match within 5 SPX delta points. This story delivers standalone value as an accurate real-time risk gauge.

**Acceptance Scenarios**:

1. **Given** a portfolio containing SPY, QQQ, and SPX options, **When** the dashboard loads, **Then** SPX Equivalent Delta is computed by converting each position's raw delta through its beta to SPX, and the total matches the broker-displayed figure within ±5 SPX delta points.
2. **Given** a position in an underlying where IBKR does not return a beta, **When** the system computes beta-weighted delta, **Then** it fetches beta from the Tastytrade data source and uses that value; if neither source provides beta, it defaults to 1.0 and flags the position as "beta unavailable."
3. **Given** a futures position (e.g., /ES or /MES), **When** beta-weighting is applied, **Then** the system uses the notional multiplier to express the position in SPX-equivalent terms, rather than a 1:1 share count.
4. **Given** SPX price changes intraday, **When** the dashboard refreshes, **Then** SPX Equivalent Delta recalculates using the latest SPX price automatically.

---

### User Story 2 - Pre-Trade Margin Simulation (Priority: P2)

As a trader, I want to simulate any multi-leg options or futures order before sending it live, so I know the exact margin impact and how my portfolio Greeks will change before committing capital.

**Why this priority**: Prevents margin calls and over-sizing. This is the critical safety gate between a read-only and a read-write system, and enables confident order entry.

**Independent Test**: Build a test order (e.g., a SPX iron condor) in the order builder, click "Simulate," and receive back the projected initial margin requirement and post-trade Greeks without any live order being transmitted. Delivers full standalone value as a risk preview tool.

**Acceptance Scenarios**:

1. **Given** a user has defined a multi-leg options combination (two or more legs), **When** they click "Simulate Trade," **Then** the system returns the broker-calculated Initial Margin requirement and the projected post-trade portfolio Greeks (Delta, Gamma, Theta, Vega) within 5 seconds, with no order transmitted to the market.
2. **Given** a single-leg stock or futures order, **When** simulated, **Then** the system returns margin impact and post-trade Delta, and the result clearly labels whether the order routes to equities, futures, or options.
3. **Given** the broker API is unavailable during simulation, **When** the user clicks "Simulate," **Then** the system displays a clear error message and does not allow order submission until connectivity is restored.
4. **Given** a simulation shows the order would exceed the user-configured maximum portfolio Delta, **When** the result is displayed, **Then** the UI highlights the Delta breach in red and prompts the user to adjust position size.

---

### User Story 3 - Live Order Execution (Priority: P3)

As a trader, I want to submit stock, options, and futures orders directly from the dashboard (including multi-leg combos), so I can act on risk signals without switching to a separate broker platform.

**Why this priority**: Completes the read-write upgrade. Once simulation is trusted, live execution follows the same flow, adding the "confirm and send" step.

**Independent Test**: Submit a small test order (1 share or 1 micro-futures contract) from the order builder and verify it appears in the IBKR order blotter and fills. Delivers standalone value as a direct execution terminal.

**Acceptance Scenarios**:

1. **Given** a user has reviewed a simulation result and clicks "Submit Order," **Then** a confirmation modal shows the full order details and requires explicit user confirmation before transmitting.
2. **Given** a confirmed order is submitted, **When** it fills, **Then** the position appears updated in the dashboard within one refresh cycle and the trade is recorded in the journal.
3. **Given** a multi-leg combo order (e.g., a vertical spread or iron condor), **When** submitted, **Then** all legs are sent as a single linked order so partial fills do not leave unintended exposure.
4. **Given** a submitted order is rejected by the broker, **When** the rejection arrives, **Then** the dashboard displays the rejection reason clearly and does not record it in the journal as a filled trade.

---

### User Story 4 - Trade Journal with Context Capture (Priority: P3)

As a systematic trader, I want every executed trade to be automatically logged with the market context at the time of entry (VIX level, portfolio Greeks, regime), plus space for my rationale and any AI suggestion, so I can review and learn from my decisions over time.

**Why this priority**: Journaling is a discipline requirement and audit trail. It enables the performance analytics in later stories and ties the AI suggestions to real outcomes.

**Independent Test**: Execute a single trade and verify a complete journal entry (timestamp, order details, fill price, VIX, pre/post Greeks, and rationale fields) is stored and retrievable. Standalone value as a trade log.

**Acceptance Scenarios**:

1. **Given** an order fills, **When** the fill is confirmed, **Then** the system automatically captures and stores: timestamp, instrument(s), quantity, fill price, VIX level at fill time, market regime label, pre-trade portfolio Greeks, post-trade portfolio Greeks, and AI rationale if the trade originated from an AI suggestion.
2. **Given** the user types a rationale note before submitting an order, **When** the trade is journaled, **Then** the user's text is stored alongside the automated context.
3. **Given** the user wants to review past trades, **When** they open the journal view, **Then** entries are displayed in reverse chronological order and filterable by date range, instrument, and regime.

---

### User Story 5 - AI Risk Analyst Suggestions (Priority: P4)

As a risk-conscious trader, I want the system to automatically detect when my portfolio breaches a risk limit (e.g., insufficient Vega in a high-VIX environment) and present 3 specific, actionable multi-leg trade suggestions that would fix the breach, so I can respond to risk events quickly and systematically.

**Why this priority**: This is the "copilot" value proposition — automated intelligence acting on structured risk data. Dependent on Greeks accuracy (P1) and journaling (P3) being in place.

**Independent Test**: Manually trigger a risk breach condition (e.g., set a Vega floor in config and reduce Vega below it) and verify 3 trade suggestion cards appear, each showing legs, quantities, and projected Greeks impact, within 10 seconds. Standalone value as an automated risk alert system.

**Acceptance Scenarios**:

1. **Given** the risk engine detects a portfolio Vega breach in a High VIX regime, **When** the breach is confirmed, **Then** within 10 seconds the dashboard displays exactly 3 multi-leg trade suggestions, each labeled with: legs, target strikes/expirations, projected Greeks improvement, and estimated Theta cost.
2. **Given** the AI suggestions are displayed, **When** the user clicks on a suggestion card, **Then** the order builder is pre-filled with the suggested legs and quantities, ready for the user to simulate or submit.
3. **Given** the AI is queried, **When** the response arrives, **Then** the suggestion rationale is stored in the journal so if the user acts on it, the AI reasoning is preserved.
4. **Given** the LLM service is unavailable, **When** a breach occurs, **Then** the system displays the breach alert without AI suggestions and does not crash or block other dashboard functionality.

---

### User Story 6 - Historical Risk & Performance Charts (Priority: P4)

As a portfolio manager, I want to see a time-series chart of my account value and SPX Equivalent Delta over the past days/weeks, and a Delta/Theta efficiency chart, so I can evaluate how my risk posture has evolved and whether my income-generation strategy is working.

**Why this priority**: Transforms the platform from a point-in-time snapshot to a longitudinal risk management tool. Requires journaling (P3) and accurate Greeks (P1) to be meaningful.

**Independent Test**: Let the background logging run for at least one hour (4 data points), then open the historical chart panel and verify the account value and SPX Delta curves render with correct values and timestamps. Standalone value as a portfolio performance tracker.

**Acceptance Scenarios**:

1. **Given** the dashboard is running, **When** 15 minutes elapse, **Then** a snapshot of Net Liquidation Value and SPX Equivalent Delta is stored in the database automatically.
2. **Given** historical snapshots exist, **When** the user opens the historical chart panel, **Then** a dual-axis chart displays Account Value and SPX Equivalent Delta over time, with the x-axis showing timestamps.
3. **Given** at least two historical snapshots exist, **When** the Delta/Theta ratio chart is rendered, **Then** it plots Portfolio Theta divided by Portfolio Delta over time, labeled as "Income-to-Risk Efficiency Ratio."
4. **Given** the user selects a time range filter, **When** applied, **Then** both charts update to show only data within the selected period.

---

### User Story 7 - Flatten Risk Panic Button (Priority: P5)

As a trader in a fast-moving market, I want a single "Flatten Risk" button that generates a set of market orders to close all short option legs (leaving long protection intact), so I can rapidly reduce tail risk exposure with minimal manual steps.

**Why this priority**: An emergency risk-reduction tool. Lower priority because it requires execution (P3) and is not a daily-use feature, but critical to have when needed.

**Independent Test**: Click "Flatten Risk" with a portfolio containing short options, verify the generated order list shows only buy-to-close for each short leg, confirm the list is displayed before any transmission, and verify clicking Cancel results in zero orders being sent. Standalone value as an emergency de-risking tool.

**Acceptance Scenarios**:

1. **Given** the portfolio contains one or more short option positions, **When** the user clicks "Flatten Risk," **Then** the system generates a list of buy-to-close market orders for every short option leg and displays them in a confirmation dialog — no orders are transmitted at this point.
2. **Given** the generated order list is displayed, **When** the user clicks "Confirm Flatten," **Then** all buy-to-close orders are submitted simultaneously as market orders.
3. **Given** the generated order list is displayed, **When** the user clicks "Cancel," **Then** no orders are submitted and the portfolio is unchanged.
4. **Given** the portfolio has no short option positions, **When** "Flatten Risk" is clicked, **Then** the system displays "No short positions to close" and takes no action.
5. **Given** some flatten orders partially fill and some do not, **When** fills arrive, **Then** each fill is journaled individually and unfilled orders remain visible in the order blotter.

---

### Edge Cases

- What happens when a position's beta cannot be fetched from either IBKR or Tastytrade? → Default to beta = 1.0 and display a warning badge on the position row.
- What happens when SPX price is unavailable for beta-weighting calculation? → Display "SPX price unavailable" and halt Greek aggregation with a visible error state.
- What happens when the margin simulation call times out (>10 seconds)? → Show a timeout error and prevent order submission until a successful simulation is completed.
- What happens if the 15-minute background logger fails silently? → Log the error to the application log; the dashboard continues operating; a status indicator shows the last successful snapshot timestamp.
- What happens when the AI produces a suggestion that involves instruments the user's account is not approved to trade? → Display the suggestion with a warning that broker approval may be required; the user is responsible for submission eligibility.
- What happens when a user submits an order and the connection to the broker drops mid-order? → The system marks the order as "status unknown," surfaces an alert, and the user must verify directly in the broker platform.
- What if Portfolio Delta is zero when computing Delta/Theta ratio? → Display the ratio as "N/A" to avoid division by zero errors.

## Requirements _(mandatory)_

### Functional Requirements

#### Module 1: Beta-Weighted Greek Aggregation

- **FR-001**: The system MUST compute SPX Equivalent Delta for each position using the formula: `(Position Delta × Underlying Price × Beta) / SPX Price`.
- **FR-002**: The system MUST source beta values from the primary broker connection; if unavailable, it MUST fall back to the secondary broker data source.
- **FR-003**: If beta cannot be obtained from any source, the system MUST default to beta = 1.0 and visually flag the affected position with a "Beta Unavailable" indicator.
- **FR-004**: The system MUST sum all position-level SPX Equivalent Deltas into a single "Portfolio SPX Equivalent Delta" displayed prominently in the dashboard header.
- **FR-005**: Beta-weighted aggregation MUST handle stocks, ETFs, equity options, index options, and futures options correctly, accounting for contract multipliers in all cases.
- **FR-006**: SPX Equivalent Delta MUST refresh automatically whenever underlying prices or positions change.

#### Module 2: Order Execution Engine & Margin Simulator

- **FR-007**: The system MUST support order construction for: single-leg equities, single-leg equity/index options, multi-leg option combos (2 to 4 legs), and futures and futures options.
- **FR-008**: The system MUST support a "Simulate Trade" action that queries the broker's what-if/margin endpoint and returns, without transmitting any order: projected Initial Margin requirement, projected post-trade portfolio Delta, Gamma, Theta, and Vega.
- **FR-009**: Simulation results MUST be displayed to the user before the "Submit Order" button is enabled.
- **FR-010**: The system MUST prevent order submission if simulation has not been successfully completed for the current order configuration.
- **FR-011**: Multi-leg combo orders MUST be transmitted as a single linked/combo order to prevent leg mismatches.
- **FR-012**: The system MUST display a two-step confirmation (preview → confirm) before transmitting any live order.
- **FR-013**: The order builder MUST support order types: limit, market, and market-on-close.

#### Module 3: Trade Journal

- **FR-014**: On every trade fill, the system MUST automatically record: timestamp (UTC), instrument(s), action (buy/sell), quantity, fill price, order type, VIX level at fill time, detected market regime, pre-trade portfolio Greeks (Delta, Gamma, Theta, Vega), post-trade portfolio Greeks, user-supplied rationale text (optional), and AI rationale (if applicable).
- **FR-015**: Journal entries MUST be stored in a local persistent database requiring no external server or cloud service.
- **FR-016**: The journal MUST be readable and filterable from within the dashboard UI (date range, instrument, regime).
- **FR-017**: Journal entries MUST be exportable to CSV format.
- **FR-018**: Journal data MUST persist across application restarts.

#### Module 4: AI Risk Analyst

- **FR-019**: When the risk engine flags a limit breach, the system MUST automatically invoke the AI analyst without requiring user action.
- **FR-020**: The AI analyst MUST be provided with: current portfolio Greeks, current VIX level, detected regime, the specific limit that was breached, and the configured Theta budget constraint.
- **FR-021**: The AI analyst MUST return exactly 3 multi-leg trade suggestions, each specifying: legs (instrument, action, quantity, expiration, strike), projected Greeks improvement, and estimated Theta cost.
- **FR-022**: Each AI suggestion card in the UI MUST be clickable and auto-populate the order builder with the suggested legs.
- **FR-023**: If the AI service is unavailable, the risk breach alert MUST still be displayed without suggestions, and the dashboard MUST remain fully functional.

#### Module 5: Historical Logging & Charting

- **FR-024**: A background process MUST record Net Liquidation Value and Portfolio SPX Equivalent Delta every 15 minutes while the application is running.
- **FR-025**: The dashboard MUST include a historical chart panel showing Account Value and SPX Equivalent Delta on a dual-axis time-series chart.
- **FR-026**: The dashboard MUST include a Delta/Theta ratio chart showing Portfolio Theta divided by Portfolio Delta over time.
- **FR-027**: Both charts MUST support time range filtering (e.g., 1 day, 1 week, 1 month, all).
- **FR-028**: Historical data MUST be stored in the same local persistent database as the trade journal.

#### Module 6: Flatten Risk (Panic Button)

- **FR-029**: The dashboard MUST display a "Flatten Risk" button that is accessible within 2 clicks from any dashboard view.
- **FR-030**: When activated, the system MUST identify all short option positions and generate buy-to-close market orders for each leg.
- **FR-031**: The generated order list MUST be displayed in a confirmation dialog before any orders are submitted; long positions and futures MUST NOT be included in the flatten list.
- **FR-032**: The user MUST be able to cancel the flatten action without any orders being transmitted.
- **FR-033**: Each filled flatten order MUST be recorded in the trade journal with rationale "Flatten Risk — user-initiated."

### Key Entities

- **Position**: A single holding in the portfolio (stock, option, or futures). Attributes: instrument identifier, quantity, current market price, raw delta, underlying price, beta, SPX equivalent delta, contract multiplier.
- **PortfolioGreeks**: Aggregate risk numbers for the full portfolio at a point in time. Attributes: SPX equivalent delta, gamma, theta, vega, timestamp.
- **Order**: A pending or completed trade instruction. Attributes: legs (1–4), order type, status (simulated / pending / filled / rejected / cancelled), simulation result, submission timestamp.
- **OrderLeg**: One component of a multi-leg order. Attributes: instrument, action (buy/sell), quantity, option type (if applicable), strike, expiration.
- **TradeJournalEntry**: A record of a completed fill with full market context. Attributes: see FR-014 for full attribute list.
- **HistoricalSnapshot**: A time-stamped record of portfolio value and risk metrics. Attributes: timestamp, net liquidation value, SPX equivalent delta, theta, vega, VIX, regime.
- **RiskBreach**: A detected violation of a risk limit that triggers the AI analyst. Attributes: breach type, threshold value, actual value, timestamp, suggested trades.
- **AITradeSuggestion**: One AI-generated remediation trade. Attributes: legs, projected delta change, projected theta cost, rationale text.

## Assumptions

- The existing IBKR (ib_insync / TWS API) and Tastytrade (tastytrade-sdk-python) connections established in prior work are available and functional; this feature extends them rather than replacing them.
- SPX index price is always fetchable from the connected broker API; if not, the feature degrades gracefully (see FR-006 edge case).
- The background historical logger runs as part of the same application process as the dashboard, not as a separate daemon; 15-minute logging frequency is sufficient given the target use case.
- Multi-leg orders are limited to 4 legs maximum, consistent with standard broker combo order support.
- The local persistent database used for journaling and historical logging is the same database instance introduced in `001-streaming-greeks-database`, extended with new tables.
- Risk limits (e.g., minimum Vega in High VIX regime, maximum Delta) are configurable via the existing `config/risk_matrix.yaml` file.
- The Theta budget constraint passed to the AI is a user-configurable parameter; default is no constraint (unlimited Theta spend per AI suggestion cycle).
- "Market regime" for journaling purposes is the label produced by the existing `regime_detector.py` (e.g., Low VIX, Medium VIX, High VIX, Crisis).

## Success Criteria _(mandatory)_

### Measurable Outcomes

- **SC-001**: Portfolio SPX Equivalent Delta displayed in the dashboard matches the broker platform's displayed delta within ±5 SPX delta points for 95% of observations when both systems are live.
- **SC-002**: Simulation results (margin and post-trade Greeks) are returned and displayed within 5 seconds of the user clicking "Simulate Trade" under normal market-hours conditions.
- **SC-003**: Every order fill results in a complete journal entry — no fills go unrecorded. Journal completeness rate target: 100%.
- **SC-004**: AI risk suggestions are generated and displayed within 10 seconds of a confirmed risk limit breach, under normal service availability.
- **SC-005**: Historical portfolio snapshots are logged at the 15-minute target interval with no more than a 2-minute variance; no more than 2 consecutive snapshots missed during normal operation.
- **SC-006**: The "Flatten Risk" confirmation-to-order-submission flow completes in under 30 seconds from button click to all orders transmitted.
- **SC-007**: The full platform (dashboard + all modules) loads and is interactive within 10 seconds of startup when broker APIs are connected.
- **SC-008**: Zero live orders are transmitted without explicit user confirmation through the two-step order flow.
