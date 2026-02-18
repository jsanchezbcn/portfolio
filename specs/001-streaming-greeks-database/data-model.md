# Data Model: Streaming Greeks and Database

## 1) Trade

Represents strategy/trade logging entries.

### Fields

- `id` (UUID, PK)
- `created_at` (timestamptz, required, default now)
- `broker` (text, required; enum-like: `ibkr|tastytrade`)
- `account_id` (text, required)
- `symbol` (text, required)
- `contract_key` (text, nullable for non-option records)
- `action` (text, required)
- `quantity` (numeric, required)
- `price` (numeric, nullable)
- `strategy_tag` (text, nullable)
- `metadata` (jsonb, nullable)

### Validation

- `quantity != 0`
- `broker` in supported broker set
- `created_at` must be timezone-aware

## 2) GreekSnapshot

Represents one persisted Greeks event for a contract at a point in time.

### Fields

- `id` (bigserial, PK)
- `event_time` (timestamptz, required) — broker event timestamp when available
- `received_at` (timestamptz, required) — local ingestion timestamp
- `persisted_at` (timestamptz, required, default now)
- `broker` (text, required; `ibkr|tastytrade`)
- `account_id` (text, required)
- `underlying` (text, required)
- `contract_key` (text, required) — canonical key (underlying+expiry+strike+type)
- `expiration` (date, nullable)
- `strike` (numeric, nullable)
- `option_type` (text, nullable; `call|put`)
- `quantity` (numeric, nullable)
- `delta` (double precision, nullable)
- `gamma` (double precision, nullable)
- `theta` (double precision, nullable)
- `vega` (double precision, nullable)
- `rho` (double precision, nullable)
- `implied_volatility` (double precision, nullable)
- `underlying_price` (double precision, nullable)
- `source_payload` (jsonb, required) — raw trace payload for audit/debug

### Indexing / Write Optimization

- Partition key: `event_time` (monthly range partitions)
- Index: `(event_time DESC)`
- Index: `(broker, account_id, contract_key, event_time DESC)`
- Optional unique dedupe key: `(broker, contract_key, event_time, delta, gamma, theta, vega)` where operationally safe

### Validation

- `broker` in supported broker set
- `contract_key` non-empty
- Greeks fields may be null individually, but at least one of `{delta,gamma,theta,vega,rho}` should be present for valid ingest record

## 3) StreamSession

In-memory runtime state (optionally persisted later).

### Fields

- `broker` (`ibkr|tastytrade`)
- `status` (`connecting|connected|degraded|disconnected`)
- `last_heartbeat_at` (datetime, nullable)
- `last_message_at` (datetime, nullable)
- `reconnect_attempt` (int)
- `subscription_count` (int)
- `last_error` (string, nullable)

### State Transitions

- `connecting -> connected` on successful auth/subscription
- `connected -> degraded` on transient read/write errors with retry in progress
- `degraded -> connected` on successful recovery
- `* -> disconnected` on explicit shutdown

## 4) UnifiedPositionRecord (Normalized DTO)

Canonical processor output before DB buffer enqueue.

### Fields

- `broker`
- `account_id`
- `underlying`
- `contract_key`
- `event_time`
- `received_at`
- Greeks metrics (`delta`, `gamma`, `theta`, `vega`, `rho`, `implied_volatility`)
- Option dimensions (`expiration`, `strike`, `option_type`, `quantity`)
- `underlying_price`
- `source_payload`

### Relationships

- One `UnifiedPositionRecord` maps to one `GreekSnapshot` row.
- Many `GreekSnapshot` rows belong to one logical contract (`broker + account_id + contract_key`).
