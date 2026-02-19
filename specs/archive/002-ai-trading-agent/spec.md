# Feature Specification: AI Trading Agent — Order Manager, Sentiment Sentry & Trade Explainer

**Feature Branch**: `002-ai-trading-agent`  
**Created**: 2026-02-17  
**Status**: Draft

## Overview

Build a three-stage autonomous trading support system for the IBKR portfolio application.

- **Stage 1** introduces a unified `OrderManager` that stages orders (STK and FUT) in TWS with `transmit=False`, plus a persistent store for order records.
- **Stage 2** adds a `NewsSentry` agent that fetches news every 15 minutes and generates a sentiment score via an LLM, and an `ArbHunter` agent that scans for Box Spread and Put-Call Parity opportunities.
- **Stage 3** enables a Copilot SDK-backed "Explain Trade" skill that compares entry Greeks to current Greeks and produces a natural-language P&L explanation for any trade.

---

## User Scenarios & Testing _(mandatory)_

### User Story 1 — Stage an order without transmitting (Priority: P1)

A trader identifies a position to add (e.g., a `/MES` futures contract). They want to construct the order in TWS in a "parked" state so they can review and confirm it before any capital is at risk.

**Why this priority**: This is the fundamental safety gate for all execution. Without staged orders the downstream stages have nothing to annotate or explain.

**Independent Test**: Call `stage_order()` with a `/MES` Future request; verify the order appears in TWS with `transmit=False`, the order ID is returned, and a record is persisted to the database.

**Acceptance Scenarios**:

1. **Given** a valid `OrderRequest` for `/MES` (quantity 1, direction BUY, limit price), **When** `stage_order()` is called, **Then** a TWS order is created with `transmit=False` and the returned order ID is non-null.
2. **Given** a staged order exists, **When** the database is queried, **Then** the record shows status `STAGED`, symbol, quantity, side, and timestamp.
3. **Given** an invalid instrument type is supplied, **When** `stage_order()` is called, **Then** a descriptive error is raised and no order is created in TWS.
4. **Given** the TWS gateway is unreachable, **When** `stage_order()` is called, **Then** the operation fails gracefully with a clear error message and no partial records are written.

---

### User Story 2 — View automated sentiment score before trading (Priority: P2)

Before placing an order, a trader wants to see a current 30-minute market sentiment score derived from recent news so they can validate or reject a thesis quickly.

**Why this priority**: Sentiment context is the primary decision signal feeding into both order staging and arbitrage scanning.

**Independent Test**: Trigger the `NewsSentry` manually; verify it produces a structured sentiment record (symbol, score, summary, timestamp) that is readable from the database.

**Acceptance Scenarios**:

1. **Given** the `NewsSentry` runs on its 15-minute schedule, **When** news is fetched, **Then** an LLM-generated sentiment score between -1.0 and +1.0 is stored with a timestamp and a ≤50-word summary.
2. **Given** no recent news exists for a symbol, **When** `NewsSentry` runs, **Then** the score is stored as `null` with reason `"no_news"` instead of erroring.
3. **Given** the news API returns an error, **When** `NewsSentry` runs, **Then** the error is logged, no record is written, and the scheduler continues to the next tick.

---

### User Story 3 — Discover arbitrage opportunities passively (Priority: P2)

The `ArbHunter` continuously monitors the option chain for risk-free or near-risk-free spreads (Box Spreads, Put-Call Parity violations) and surfaces them so the trader can act.

**Why this priority**: Arbitrage signals are time-sensitive and must be captured automatically; manual scanning is too slow.

**Independent Test**: Feed a mock option chain with a known Put-Call Parity violation into `ArbHunter`; verify a signal record is written to the `signals` table with correct spread details and expected profit.

**Acceptance Scenarios**:

1. **Given** a current option chain contains a Box Spread with positive expected value after fees, **When** `ArbHunter` evaluates it, **Then** an `Opportunity` record is written to the `signals` table with spread legs, net credit/debit, and confidence level.
2. **Given** all opportunities carry negative expected value, **When** `ArbHunter` runs, **Then** no records are written and the scan completes silently.
3. **Given** a previously written opportunity still exists and conditions no longer hold, **When** `ArbHunter` runs again, **Then** the existing record is marked `expired`.

---

### User Story 4 — Get a natural-language explanation of a trade's P&L (Priority: P3)

After a trade has been live for some time, a trader highlights it in their terminal and asks the Copilot agent "explain this trade." The agent fetches the entry thesis, current Greeks, and recent news and produces a concise explanation.

**Why this priority**: Human-readable P&L attribution closes the feedback loop and is only valuable after Stages 1 and 2 are operational.

**Independent Test**: Invoke the `explain_performance` skill with a known `trade_id`; verify the response references both the original thesis from `trade_journal` and at least one current Greek comparison.

**Acceptance Scenarios**:

1. **Given** a `trade_id` with a stored thesis and current Greeks, **When** the "Explain Trade" skill is invoked, **Then** the agent returns a natural-language explanation that mentions the original thesis, at least one Greek that has changed significantly, and current sentiment.
2. **Given** a `trade_id` that does not exist in the database, **When** the skill is invoked, **Then** a user-friendly "trade not found" message is returned without exposing internal errors.
3. **Given** the trade's thesis is still valid but Vega loss is outweighing price gain, **When** the skill is invoked, **Then** the explanation explicitly calls out the Vega drag even if the directional thesis remains correct.

---

### Edge Cases

- What happens when TWS disconnects mid-order-staging? (Partial write must be rolled back.)
- How does `ArbHunter` handle stale quotes with a bid/ask spread too wide to compute reliable parity?
- What if the LLM rate-limits or returns a malformed sentiment score?
- How does the Copilot agent behave when both `trade_journal` and `market_intel` records are absent for a trade ID?
- What if the same arbitrage opportunity is detected on consecutive scans without expiring?

---

## Requirements _(mandatory)_

### Functional Requirements

**Order Management (Stage 1)**

- **FR-001**: The system MUST accept an `OrderRequest` describing instrument type (STK or FUT), symbol, quantity, direction, and limit price.
- **FR-002**: `stage_order()` MUST create an order in TWS with `transmit=False` so no capital is committed until explicit human confirmation.
- **FR-003**: Every staged order MUST be persisted with its TWS order ID, instrument details, status (`STAGED`), and creation timestamp before `stage_order()` returns.
- **FR-004**: The order persistence layer MUST use an async connection pool to the configured database host and port.
- **FR-005**: The system MUST reject order requests for unsupported instrument types with a descriptive error; no TWS call is made for rejected requests.

**News Sentiment (Stage 2)**

- **FR-006**: `NewsSentry` MUST fetch news headlines for monitored symbols from a configured news provider on a 15-minute schedule.
- **FR-007**: Each news batch MUST be passed to an LLM to produce a sentiment score in [-1.0, +1.0] and a summary of 50 words or fewer.
- **FR-008**: Sentiment records MUST be stored with symbol, score, summary, source, and timestamp.
- **FR-009**: `NewsSentry` MUST continue scheduling even when a single news fetch or LLM call fails; failures are logged and do not crash the scheduler.

**Arbitrage Hunter (Stage 2)**

- **FR-010**: `ArbHunter` MUST evaluate Box Spread and Put-Call Parity conditions against available option chain data.
- **FR-011**: Opportunities with positive expected value after estimated fees MUST be written to a `signals` table.
- **FR-012**: Each signal MUST include spread legs (symbols, strikes, expirations), net value, and a confidence or quality score.
- **FR-013**: Stale signals MUST be marked `expired` when conditions no longer hold on the next evaluation pass.

**Trade Explainer (Stage 3)**

- **FR-014**: The "Explain Trade" skill MUST accept a `trade_id` and retrieve the entry thesis from `trade_journal` and any associated `market_intel` records.
- **FR-015**: The skill MUST retrieve current Greeks for the position and compare them to the Greeks stored at entry.
- **FR-016**: The skill MUST produce a natural-language explanation via the Copilot SDK that references thesis validity, meaningful Greek changes, and current sentiment.
- **FR-017**: The skill MUST return a graceful user-facing message when `trade_id` is not found; no internal stack trace is exposed.

### Key Entities

- **OrderRequest**: Instrument type (STK/FUT), symbol, quantity, direction (BUY/SELL), limit price; optional expiration and strike for derivatives.
- **StagedOrder**: TWS order ID, `OrderRequest` data, status (STAGED / TRANSMITTED / CANCELLED), creation timestamp, account ID.
- **SentimentRecord**: Symbol, score (-1.0 to +1.0), summary text, news source, generation timestamp.
- **ArbitrageSignal**: Signal ID, type (BOX_SPREAD / PUT_CALL_PARITY), spread legs, net value, confidence score, status (ACTIVE / EXPIRED), detected timestamp.
- **TradeJournalEntry**: Trade ID, symbol, entry Greeks snapshot, thesis text, entry timestamp.
- **MarketIntel**: Trade ID, source (news/sentiment), content, timestamp.

---

## Success Criteria _(mandatory)_

### Measurable Outcomes

- **SC-001**: A trader can stage a `/MES` futures order and confirm TWS receipt with `transmit=False` in under 5 seconds from the `stage_order()` call.
- **SC-002**: 100% of staged orders are persisted to the database before the function returns; no order is staged without a corresponding database record.
- **SC-003**: `NewsSentry` produces a sentiment score within 60 seconds of its scheduled trigger under normal API and LLM availability.
- **SC-004**: `ArbHunter` identifies all seeded arbitrage opportunities in a mock option chain with zero false negatives.
- **SC-005**: The "Explain Trade" skill returns a response for a valid `trade_id` in under 10 seconds end-to-end.
- **SC-006**: The explanation for a trade where Vega change exceeds the defined threshold mentions Vega in 100% of invocations.
- **SC-007**: All three stages are independently deployable and testable — Stage 1 passes its full test suite without Stages 2 or 3 being present.

### Scope Boundaries

**In scope**: `OrderManager` for STK and FUT; `NewsSentry` with LLM sentiment; `ArbHunter` for Box Spread and Put-Call Parity; Copilot SDK "Explain Trade" skill; database tables and tests for all three stages.

**Out of scope**: Automatic order transmission (orders remain staged until human confirmation); instruments beyond STK/FUT; real-time streaming Greeks for the Explainer (polling at invocation time is acceptable); a graphical UI.

---

## Assumptions

- The IBKR Client Portal Gateway is running and authenticated before any stage is invoked (same pattern as `ibkr_gateway_client.py`).
- The news provider (Alpaca or Finnhub) is configured with valid credentials in environment variables.
- The Copilot SDK is available in Python or via a thin Python wrapper; the agent manifest setup is part of Stage 3 scope.
- "Fees" for arbitrage calculations use a configurable flat per-leg estimate; real-time fee lookup is out of scope.
- LLM calls for sentiment use a configurable model (defaulting to `gpt-4o-mini`).
- New database tables extend the existing schema managed by `database/db_manager.py`.
