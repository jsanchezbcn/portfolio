# Phase 0 Research: Streaming Greeks and Database

## Decision 1: Use asyncpg pool + batched `executemany` in a singleton DB manager

- Decision: Implement `database/db_manager.py` as a singleton around `asyncpg.create_pool()` with buffered writes flushed every 1 second or 50 records (whichever comes first).
- Rationale: Meets high-frequency write requirement while reducing per-row transaction overhead and connection churn; maps directly to required architecture.
- Alternatives considered:
  - Per-event insert (rejected: too much overhead at burst rates).
  - ORM-based writes (rejected: higher abstraction overhead and not required by constraints).

## Decision 2: Partition-friendly time-series table strategy for `greek_snapshots`

- Decision: Define `greek_snapshots` with append-optimized schema, indexed on `(event_time, broker, account_id, contract_key)`, and use monthly range partitioning by `event_time`.
- Rationale: Supports sustained insert throughput and bounded index size as data grows, while keeping query performance for recent windows.
- Alternatives considered:
  - Single non-partitioned table (rejected: long-term bloat and slower retention operations).
  - External TSDB extension (rejected: out of current scope and deployment complexity).

## Decision 3: IBKR streamer via `websockets` with explicit heartbeat and reconnect loop

- Decision: Implement `streaming/ibkr_ws.py` using `websockets.connect()` to `wss://localhost:5000/v1/api/ws`, send keepalive every 60s, and reconnect with bounded backoff.
- Rationale: Hard requirement mandates `websockets`; explicit heartbeat is required to preserve session continuity.
- Alternatives considered:
  - Existing REST polling endpoints (rejected: does not satisfy real-time requirement).
  - Generic HTTP stream clients (rejected: no direct alignment with WebSocket requirement).

## Decision 4: Tastytrade streaming via DXLink wrapper

- Decision: Implement `streaming/tasty_dxlink.py` as a wrapper around `tastytrade` DXLink streaming that subscribes to all open positions and forwards normalized payload dicts.
- Rationale: Reuses validated SDK capabilities and current account auth flow while centralizing ingestion behavior.
- Alternatives considered:
  - Custom websocket implementation for Tastytrade (rejected: duplicates SDK logic and auth handling).

## Decision 5: Central processor for normalization and buffering

- Decision: Implement `core/processor.py` as the single event hub that maps IBKR/Tastytrade payloads to UnifiedPosition-aligned records and enqueues DB buffer writes.
- Rationale: Enforces one normalization path, keeps broker-specific parsing isolated, and simplifies testing.
- Alternatives considered:
  - Direct streamer-to-DB writes (rejected: duplicates mapping logic and weakens consistency guarantees).

## Decision 6: Latency measurement and SLO enforcement

- Decision: Track per-event timestamps (`received_at`, `persisted_at`) and compute rolling p95 latency for operational logging/alerts.
- Rationale: Required to prove <500ms target and diagnose bottlenecks.
- Alternatives considered:
  - No explicit latency instrumentation (rejected: cannot verify success criterion SC-002).

## Decision 7: Failure isolation and graceful degradation

- Decision: Run IBKR and Tastytrade ingestion loops independently under a supervisor task; one stream failure must not block the other.
- Rationale: Directly required by FR-013 and constitution graceful-degradation rules.
- Alternatives considered:
  - Single combined event loop with shared fatal failure path (rejected: violates isolation requirement).

## Clarification Resolution Summary

- No unresolved `NEEDS CLARIFICATION` items remain.
- Confirmed stack: Python 3.11+, PostgreSQL 16, asyncpg, websockets, tastytrade, python-dotenv.
- Confirmed empty-database bootstrap: schema creation handled at service startup.
