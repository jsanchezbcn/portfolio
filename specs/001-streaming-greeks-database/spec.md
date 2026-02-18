# Feature Specification: Streaming Greeks and Database

**Feature Branch**: `001-streaming-greeks-database`  
**Created**: 2026-02-14  
**Status**: Draft  
**Input**: User description: "Transition the portfolio manager from static polling to real-time streaming with persistent PostgreSQL storage for Greeks and trades."

## User Scenarios & Testing _(mandatory)_

### User Story 1 - Real-time IBKR Greeks Persistence (Priority: P1)

As a portfolio manager, I want live Greeks from IBKR option positions to be streamed and stored immediately so that risk metrics reflect current market conditions instead of stale polled values.

**Why this priority**: This is the highest-impact risk reliability gap and unlocks real-time monitoring for active positions.

**Independent Test**: Can be fully tested by starting the IBKR stream, generating market updates for active option contracts, and verifying snapshots are written to the database with end-to-end latency below the required threshold.

**Acceptance Scenarios**:

1. **Given** IBKR streaming is enabled and active option contracts exist, **When** market Greeks updates arrive, **Then** each update is normalized and written as a Greek snapshot record.
2. **Given** an established IBKR stream, **When** 60 seconds elapse without data-plane activity, **Then** a keepalive heartbeat is sent and the stream remains connected.
3. **Given** the stream disconnects unexpectedly, **When** connectivity returns, **Then** the system reconnects and resumes updates without manual intervention.

---

### User Story 2 - Real-time Tastytrade Greeks Persistence (Priority: P2)

As a portfolio manager, I want live Greeks from Tastytrade open positions to be streamed and persisted so that cross-broker risk exposure is tracked continuously in one datastore.

**Why this priority**: Cross-broker completeness is critical for true portfolio-level risk visibility.

**Independent Test**: Can be fully tested by authenticating the Tastytrade stream, subscribing to all open positions, and verifying continuous snapshot writes for each subscribed contract.

**Acceptance Scenarios**:

1. **Given** valid Tastytrade credentials and open option positions, **When** the streamer initializes, **Then** the system subscribes to Greeks updates for all open positions.
2. **Given** Tastytrade Greeks updates arrive, **When** each update is processed, **Then** a normalized snapshot record is persisted with source and contract identity.

---

### User Story 3 - Unified Stored Risk Timeline (Priority: P3)

As a portfolio manager, I want broker-specific feed payloads normalized into one position schema before persistence so that downstream analytics and reporting use a consistent historical timeline.

**Why this priority**: Normalization prevents broker-specific data shape differences from breaking risk calculations and strategy review workflows.

**Independent Test**: Can be fully tested by replaying mixed broker updates and confirming resulting records share the same required fields and can be queried together by account, symbol, and time range.

**Acceptance Scenarios**:

1. **Given** mixed IBKR and Tastytrade updates for equivalent instrument types, **When** records are stored, **Then** each record conforms to the UnifiedPosition-aligned schema.
2. **Given** the database starts empty, **When** streaming begins, **Then** required tables are created automatically and begin accumulating records without manual setup.

### Edge Cases

- What happens when one broker stream is connected and the other is down? The connected stream must continue writing data independently.
- How does the system handle duplicate or out-of-order updates for the same contract and timestamp window? It must avoid corrupting the time series and preserve deterministic ordering rules.
- What happens when a payload is missing one or more Greeks fields? The record is still stored with available values and missing fields marked consistently.
- What happens when database connectivity is temporarily unavailable? Incoming updates are retried with bounded backoff, and failures are surfaced in operational logs.
- What happens when there are zero active option contracts or open positions? The system remains healthy, keeps stream/session alive, and writes no empty placeholder rows.

## Requirements _(mandatory)_

### Functional Requirements

- **FR-001**: System MUST connect to a local PostgreSQL instance using configured environment values, with defaults aligned to localhost host, port 5432, and user portfolio.
- **FR-002**: System MUST create and maintain a `trades` table for strategy logging records if it does not already exist.
- **FR-003**: System MUST create and maintain a `greek_snapshots` table for time-series Greeks records if it does not already exist.
- **FR-004**: System MUST support sustained high-frequency inserts into `greek_snapshots` without preventing concurrent reads needed for risk views.
- **FR-005**: System MUST replace IBKR polling for Greeks with real-time WebSocket streaming from the configured IBKR endpoint.
- **FR-006**: System MUST send an IBKR heartbeat every 60 seconds to keep the stream connection alive.
- **FR-007**: System MUST subscribe to live Greeks updates for all active IBKR option contracts relevant to the portfolio.
- **FR-008**: System MUST authenticate and initialize Tastytrade streaming and subscribe to Greeks updates for all open positions.
- **FR-009**: System MUST normalize inbound broker updates into a UnifiedPosition-compatible structure before persistence.
- **FR-010**: System MUST write each normalized Greeks event to persistent storage with broker source, contract identity, timestamp, and available Greeks values.
- **FR-011**: System MUST meet a market-tick-to-database-write latency target of less than 500 ms under normal operating conditions.
- **FR-012**: System MUST start successfully from an empty database and begin persisting records without manual table creation.
- **FR-013**: System MUST continue operating when one broker stream fails, while isolating failures and preserving ingestion from healthy sources.

### Key Entities _(include if feature involves data)_

- **Trade**: Strategy logging record capturing account, instrument, action, quantity, price context, and event timestamp.
- **Greek Snapshot**: Time-series point containing broker source, account, contract identifier, normalized symbol metadata, timestamp, and Greeks values (delta, gamma, theta, vega, rho, implied volatility, underlying reference).
- **Unified Position Record**: Canonical normalized representation used to map broker-specific payloads into a single persisted shape for downstream analytics.
- **Stream Session**: Runtime state for a broker feed connection including broker identity, connection status, subscription set, last heartbeat/keepalive time, and recovery state.

### Assumptions & Dependencies

- The local PostgreSQL instance is reachable from the application runtime and uses credentials defined in local environment configuration.
- The target database starts empty for this feature rollout, and historical backfill is out of scope.
- Broker account credentials and entitlements are valid for live Greeks streaming.
- System clocks are synchronized enough to support sub-second latency measurement.
- Normal operating load for acceptance is defined as 50-200 subscribed option contracts and sustained inbound rate between 5-50 ticks/second over a 10-minute run.

## Success Criteria _(mandatory)_

### Measurable Outcomes

- **SC-001**: At least 99% of valid incoming Greeks ticks are persisted successfully without manual retry.
- **SC-002**: End-to-end latency from market tick receipt to durable database write is under 500 ms for at least 95% of ticks during normal market load.
- **SC-003**: From an empty database, first successful persisted snapshot occurs within 60 seconds of starting the service with active market data available.
- **SC-004**: During a single-broker stream outage, ingestion from the healthy broker continues with no more than 5 seconds of interruption.
- **SC-005**: Cross-broker queries over the persisted timeline return a unified schema with 100% required fields present for every stored record.
