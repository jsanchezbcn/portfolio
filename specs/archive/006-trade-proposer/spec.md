# Feature Specification: Trade Proposer Agent

**Feature Branch**: `006-trade-proposer`  
**Created**: 2026-02-23  
**Status**: Draft  
**Input**: User description: "Build a 'Trade Proposer' agent that monitors portfolio Greeks, detects risk limit breaches, generates candidate option hedges, simulates them using IBKR's What-If API, and writes the most capital-efficient solutions to a PostgreSQL database."

## User Scenarios & Testing _(mandatory)_

### User Story 1 - Risk Breach Detection (Priority: P1)

As a portfolio manager, I want the system to continuously monitor my live Greeks against the dynamic risk matrix so that I am immediately alerted or presented with solutions when my risk limits (Vega, Delta, Gamma, Margin) are exceeded.

**Why this priority**: Correct risk identification is the foundation of the hedging agent. Without accurate detection, no valid hedges can be proposed.

**Independent Test**: Can be **Independeanually injecting a "breach" state into the portfolio data and verifying the agent **Independent Test**: Can be **Indurately based on the active market regime.

**Acceptance Scenarios**:

1. **Given** the market is in a high-volatility state and directional risk is +0.5% (above the 0.3% limit), **When** the agent runs its check, **Then** it must identify the breach and calculate the specific amount of risk reduction needed to return to a neutral state.
2. **Given** a Margin Utilization of 25% during a period where only 20% is allowed, **When** the agent runs its check, **Then** it must identify the breach and restrict new trade proposals that would increase margin usage.

---

### User Story 2 - Capital-Efficient Candidate Generation (Priority: P1)

As a trader, I want the agent to simulate multiple hedging strategies using real-time margin impact data so that I only see trades that provide the maximum risk reduction for the minimum capital requirement.

**Why this priority**: Hedging should reduce risk efficiently without unnecessarily tying up capital that could be used for other trades.

**Independent Test**: Verify that for a given breach, the agent generates multiple candidates using liquid assets, simulates their impact, and correctly ranks them by a calculated efficiency score.

**Acceptance Scenarios**:

1. **Given** a risk breach, **When** the agent evaluates two different hedging structures, **Then** it should rank the one that provides more risk coverage per dollar of margin impact higher.
2. **Given** multiple options for hedging, **When** evaluating time-sensitive risks (Vega), **Then** the agent should focus on assets with specific time-to-expiration windows (30-60 days) to ensure the hedge is effective.

---

### User Story 3 - Trade Approval Queue (Priority: P2)

As a trader, I want to review proposed hedges in a structured queue with clear justifications so that I can make informed decisions before any orders are placed.

**Why this priority\***Why this priority***Why this priority***Why this priority***Why this priority***Why this pri\*: Verify that the most efficient candidates **Why this priority\***Why this priority***Why this priority***Why this priority***Why this priority***Why this pri\*: Verify that the most efficientg candidates, **When** multiple valid solutions are found, **Then** only the most efficient ones should be presented in the review queue. 2. **Given** a proposed trade is viewed, **When** checking the justification, **Then** it must clearly state the specific risk limit 2. **Given** a proposed trade is viewed, **When** checking the justification, **Then** it must clearly state the specific risk limit 2. **Given** a proposed trade is viewed, **When** checking the justification, **reme Market Panic**: How does the system behave when market indicators exceed all standard thresholds? (Default: Revert to most conservative "Crisis" limits).

- **No Efficient Solutions**: What if no trade improves risk without violating margin limits? (Default: Record "No feasible hedge found" and maintain alerts).

## Requirements _(mandatory)_

### Functional Requirements

- **FR-001**: System MUST monitor portfolio risk metrics (Vega, Theta, Delta, Gamma) against a configurable matrix.
- **FR-002**: System MUST dynamically adjust risk limits based on market volatility and term structure indicators.
- **FR-003**: System MUST identify the "Distance to Target" when a breach occurs to determine the exact amount of hedging required.
- **FR-004**: System MUST generate candidate trade combinations using liquid assets (e.g., highly liquid index benchmarks).
- **FR-005**: System MUST use a simulation interface to determine the exact margin impact (initial and maintenance) for each candidate trade.
- **FR-006**: System MUST calculate an Efficiency Score based on the ratio of risk reduction to the total capital cost (margin + fees).
- **FR-007**: System MUST automatically filter out any candidates that would cause the portfolio to exceed maximum margin utilization thresholds.
- **FR-008**: System MUST persist the top 3 candidates to a database for human review.
- **FR-009**: System MUST handle API connection pacing and rate limits with automatic retries.
- **FR-010**: System MUST support multiple market regimes (Neutral, High Volatility, Crisis) with unique limit sets.

### Detailed Constraints

- **FR-011**: System MUST limit hedging assets to the three benchmarks specified (SPX, SPY, /ES) to ensure maximum liquidity.
- **FR-012**: System MUST execute the monitoring loop at a frequency of 5 minutes.
- **FR-013**: System MUST provide trade data in a standard leg object structure (list of dictionaries containing `conId`, `symbol`, `action`, and `quantity`) within the `legs_json` column.
- **FR-014**: System MUST mark all existing "Pending" proposals as "Superseded" for the specific account before persisting a new batch of proposals, ensuring only the most current market-aligned trades are active.

### Key Entities _(include if feature involves data)_

- **ProposedTrade**: A data object containing strategy details, cost, margin impact, efficiency metrics, and a `status` (e.g., "Pending", "Superseded", "Approved", "Rejected").
- **RiskRegime**: A configuration state defining active limits and target baselines based on market conditions.
- **RiskLimitCheck**: A log of a specific detection event including the metrics at the time of breach.

## Success Criteria _(mandatory)_

### Measurable Outcomes

- **SC-001**: Breach detection occurs within 60 seconds of any major change in portfolio risk data.
- **SC-002**: A complete simulation of 5 candidate trades finishes in under 15 seconds.
- **SC-003**: All proposed trades reduce the targeted risk without violating any secondary limits (e.g., margin).
- **SC-004**: 100% of proposals are stored in a non-executable "Pending" state to ensure human oversight.
- **SC-005**: Efficiency scores result in consistent ranking of lower-margin/higher-coverage trades.

## Assumptions

- **A-001**: Up-to-date portfolio risk data (Greeks) is provided by an external data stream.
- **A-002**: The brokerage connection supports "What-If" margin inquiries for all generated candidates.
- **A-003**: The user has configured the necessary database and API access credentials.
