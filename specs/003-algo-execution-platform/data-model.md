# Data Model: 003 — Algo Execution & Journaling Platform

**Phase 1 output** | Branch: `003-algo-execution-platform` | Date: 2026-02-19

---

## Overview

This feature adds two new tables to the existing SQLite local store (`database/local_store.py`) and introduces several in-memory data classes used by the execution engine, beta weighter, and AI analyst.

---

## 1. SQLite Tables (Storage)

### 1.1 `trade_journal`

Full-fidelity record of every executed fill. Multi-leg trades stored as a JSON array in `legs_json`. One row = one complete trade event.

```sql
CREATE TABLE IF NOT EXISTS trade_journal (
    -- Identity
    id               TEXT PRIMARY KEY,           -- str(uuid.uuid4())
    created_at       TEXT NOT NULL,              -- ISO-8601 UTC, e.g. "2026-02-19T14:32:00Z"
    broker           TEXT NOT NULL,              -- "IBKR" | "TASTYTRADE"
    account_id       TEXT NOT NULL,
    broker_order_id  TEXT,                       -- broker's fill confirmation / order ref

    -- Instrument summary
    underlying       TEXT NOT NULL,              -- "SPX", "AAPL", "/ES"
    strategy_tag     TEXT,                       -- "iron_condor" | "long_call" | "short_strangle" | etc.
    status           TEXT NOT NULL DEFAULT 'FILLED', -- "FILLED" | "PARTIAL" | "CANCELLED"

    -- Multi-leg payload
    legs_json        TEXT NOT NULL DEFAULT '[]',
    -- Array of leg objects:
    -- [{"symbol": "SPX 260321C05200", "expiry": "2026-03-21", "strike": 5200,
    --   "right": "C" | "P" | null, "action": "SELL" | "BUY",
    --   "qty": 1, "fill_price": 12.50, "conid": "265598"}]

    -- Net cost of the trade (positive = debit paid, negative = credit received)
    net_debit_credit REAL,

    -- Market context at fill time
    vix_at_fill      REAL,
    spx_price_at_fill REAL,
    regime           TEXT,                       -- "LOW_VOL" | "MEDIUM_VOL" | "HIGH_VOL" | "CRISIS"

    -- Full portfolio Greek snapshots (NOT just this trade's contribution)
    pre_greeks_json  TEXT NOT NULL DEFAULT '{}',
    -- {"spx_delta": -120.5, "gamma": -8.2, "theta": 340.0, "vega": -1200.0}
    post_greeks_json TEXT NOT NULL DEFAULT '{}',

    -- Rationale
    user_rationale   TEXT,                       -- Free text entered by the user before submission
    ai_rationale     TEXT,                       -- LLM-generated rationale (if trade from AI suggestion)
    ai_suggestion_id TEXT                        -- FK to signals.id if originated from AI
);

CREATE INDEX IF NOT EXISTS ix_tj_created_at ON trade_journal(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_tj_account    ON trade_journal(account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_tj_underlying ON trade_journal(underlying, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_tj_regime     ON trade_journal(regime, created_at DESC);
```

**Validation rules**:

- `id` must be a valid UUID v4 string
- `created_at` must be ISO-8601 UTC
- `legs_json` must parse as a JSON array with ≥ 1 element
- `status` must be one of `FILLED | PARTIAL | CANCELLED`
- `regime` must be one of `LOW_VOL | MEDIUM_VOL | HIGH_VOL | CRISIS` (matches `regime_detector.py` output)

---

### 1.2 `account_snapshots`

15-minute time-series record of portfolio value and aggregate risk metrics. Used for historical charts.

```sql
CREATE TABLE IF NOT EXISTS account_snapshots (
    id               TEXT PRIMARY KEY,           -- str(uuid.uuid4())
    captured_at      TEXT NOT NULL,              -- ISO-8601 UTC
    account_id       TEXT NOT NULL,
    broker           TEXT NOT NULL,

    -- Account value
    net_liquidation  REAL,                       -- Net Liquidation Value in USD
    cash_balance     REAL,                       -- Cash component

    -- Aggregate portfolio Greeks (SPX beta-weighted, full portfolio)
    spx_delta        REAL,                       -- SPX-equivalent delta
    gamma            REAL,
    theta            REAL,
    vega             REAL,

    -- Delta/Theta efficiency ratio (pre-computed for charting)
    delta_theta_ratio REAL,                      -- theta / delta; NULL if delta == 0

    -- Market context
    vix              REAL,
    spx_price        REAL,
    regime           TEXT
);

-- Primary lookup: full time series for one account, newest first
CREATE INDEX IF NOT EXISTS ix_acct_snap_account_time
    ON account_snapshots(account_id, captured_at DESC);

-- Secondary: across all accounts by time (for admin/debug)
CREATE INDEX IF NOT EXISTS ix_acct_snap_time
    ON account_snapshots(captured_at DESC);
```

**Capacity**: At 15-min intervals × 8 trading hours × 252 trading days = ~8,064 rows/year per account. SQLite trivially handles millions of rows; no partitioning required.

---

## 2. In-Memory Data Classes

Python `dataclasses` (not persisted directly). Located in `models/unified_position.py` (extend) and new file `models/order.py`.

### 2.1 `BetaWeightedPosition` (extends `UnifiedPosition`)

```python
@dataclass
class BetaWeightedPosition:
    symbol: str
    underlying: str
    quantity: int
    raw_delta: float            # position delta, pre-weighting
    underlying_price: float     # live price of the underlying (NOT strike)
    beta: float                 # beta vs SPX (1.0 if unavailable)
    beta_source: str            # "tastytrade" | "yfinance" | "config" | "default"
    beta_unavailable: bool      # True if defaulted to 1.0 due to missing data
    multiplier: float           # 1, 100, 50, 5
    spx_equiv_delta: float      # computed: raw_delta * underlying_price * beta / spx_price
```

### 2.2 `PortfolioGreeks`

```python
@dataclass
class PortfolioGreeks:
    timestamp: datetime
    spx_equiv_delta: float
    gamma: float
    theta: float
    vega: float
    beta_unavailable_count: int  # number of positions that defaulted to beta=1.0
```

### 2.3 `OrderLeg`

```python
@dataclass
class OrderLeg:
    conid: int                  # IBKR conid
    symbol: str                 # human-readable symbol string
    action: str                 # "BUY" | "SELL"
    quantity: int               # always positive; action carries sign
    right: str | None           # "C" | "P" | None (None for stock/futures)
    strike: float | None
    expiry: str | None          # "YYYYMMDD" format
    multiplier: float           # 1, 100, 50, 5
    # Greeks of this leg (fetched from IBKR snapshot endpoint)
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
```

### 2.4 `Order`

```python
@dataclass
class Order:
    id: str                     # UUID
    account_id: str
    legs: list[OrderLeg]        # 1–4 legs
    order_type: str             # "LMT" | "MKT" | "MOC"
    limit_price: float | None   # required for LMT
    tif: str                    # "DAY" | "GTC"
    status: str                 # "DRAFT" | "SIMULATED" | "PENDING" | "FILLED" | "REJECTED" | "CANCELLED"
    simulation_result: SimulationResult | None
    user_rationale: str
    ai_suggestion_id: str | None
    created_at: datetime
    submitted_at: datetime | None
    filled_at: datetime | None
    fill_prices: dict[str, float] | None   # conid → fill price
```

### 2.5 `SimulationResult`

```python
@dataclass
class SimulationResult:
    initial_margin_current: float
    initial_margin_change: float
    initial_margin_after: float
    maintenance_margin_change: float
    equity_change: float
    projected_greeks: PortfolioGreeks
    simulated_at: datetime
    broker_warn: str | None
```

### 2.6 `RiskBreach`

```python
@dataclass
class RiskBreach:
    breach_type: str            # e.g. "VEGA_TOO_LOW" | "DELTA_OVER_LIMIT"
    metric: str                 # "vega" | "spx_delta" | etc.
    threshold: float
    actual_value: float
    regime: str
    detected_at: datetime
    suggestions: list[AITradeSuggestion]  # populated after AI call
```

### 2.7 `AITradeSuggestion`

```python
@dataclass
class AITradeSuggestion:
    id: str                     # UUID — referenced by trade_journal.ai_suggestion_id
    legs: list[OrderLeg]
    projected_delta_change: float
    projected_theta_cost: float
    rationale: str
```

---

## 3. State Transitions

### Order Status Flow

```
DRAFT → SIMULATED → PENDING → FILLED
                  → REJECTED
                  → CANCELLED
```

- `DRAFT`: order built in UI, not yet simulated
- `SIMULATED`: what-if call completed; `simulation_result` populated
- `PENDING`: submitted to broker, awaiting fill
- `FILLED` / `REJECTED` / `CANCELLED`: terminal states; `FILLED` triggers journal write

### RiskBreach Lifecycle

```
(breach detected by regime_detector) → RiskBreach created (suggestions=[])
    → AI query dispatched (async)
    → suggestions populated
    → displayed in UI
    → (archived when user acts or breach clears)
```
