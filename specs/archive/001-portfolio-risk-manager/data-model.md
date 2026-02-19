# Data Model: Portfolio Risk Management System

**Feature**: Portfolio Risk Management System  
**Date**: February 12, 2026

## Core Entities

### UnifiedPosition

Normalized position representation across all broker integrations.

**Purpose**: Provide a single, consistent format for position data regardless of source (IBKR, Tastytrade, future brokers).

**Fields**:

| Field              | Type           | Required        | Description                                    |
| ------------------ | -------------- | --------------- | ---------------------------------------------- |
| symbol             | str            | Yes             | Ticker symbol or contract description          |
| instrument_type    | InstrumentType | Yes             | Enum: EQUITY, OPTION, FUTURE, FUTURE_OPTION    |
| broker             | str            | Yes             | Source broker: 'ibkr', 'tastytrade', etc.      |
| quantity           | float          | Yes             | Position size (negative for short)             |
| avg_price          | float          | Yes             | Average entry price                            |
| market_value       | float          | Yes             | Current market value                           |
| unrealized_pnl     | float          | Yes             | Unrealized profit/loss                         |
| delta              | float          | Yes (default 0) | Position delta (Greeks × quantity for options) |
| gamma              | float          | Yes (default 0) | Position gamma                                 |
| theta              | float          | Yes (default 0) | Position theta ($/day)                         |
| vega               | float          | Yes (default 0) | Position vega                                  |
| spx_delta          | float          | Yes (default 0) | SPX-weighted delta (beta-adjusted)             |
| underlying         | str            | No              | Underlying symbol (for options)                |
| strike             | float          | No              | Strike price (for options)                     |
| expiration         | date           | No              | Expiration date (for options)                  |
| option_type        | str            | No              | 'call' or 'put' (for options)                  |
| iv                 | float          | No              | Implied volatility (for options)               |
| days_to_expiration | int            | No              | Calculated DTE (for options)                   |
| timestamp          | datetime       | Yes (auto)      | When position was fetched                      |

**Computed Properties**:

- `dte_bucket` → str: Returns "0-7", "8-30", "31-60", "60+", or "N/A"
  - Used for grouping gamma risk by expiration proximity
  - Critical for Taleb's gamma explosion warning

**Validation Rules**:

1. If `instrument_type` is OPTION, must have: underlying, strike, expiration, option_type
2. Greeks default to 0.0 if not provided
3. `spx_delta` calculated externally (not validated internally)
4. `days_to_expiration` computed from expiration date vs current date

**Example**:

```python
UnifiedPosition(
    symbol='AAPL 250320C00180000',
    instrument_type=InstrumentType.OPTION,
    broker='ibkr',
    quantity=10,
    avg_price=5.20,
    market_value=5500.0,
    unrealized_pnl=300.0,
    delta=6.5,  # 0.65 per contract × 10 contracts
    gamma=0.35,
    theta=-8.5,
    vega=12.0,
    spx_delta=45.2,
    underlying='AAPL',
    strike=180.0,
    expiration=date(2025, 3, 20),
    option_type='call',
    iv=0.28,
    days_to_expiration=36
)
# dte_bucket → "31-60"
```

---

### MarketRegime

Represents current market volatility state and associated risk limits.

**Purpose**: Encode regime-specific trading rules and risk constraints based on market conditions.

**Fields**:

| Field       | Type         | Description                                                                                 |
| ----------- | ------------ | ------------------------------------------------------------------------------------------- |
| name        | str          | Regime identifier: 'low_volatility', 'neutral_volatility', 'high_volatility', 'crisis_mode' |
| condition   | str          | Human-readable condition formula (e.g., "VIX > 22")                                         |
| description | str          | Market characterization                                                                     |
| limits      | RegimeLimits | Risk constraints for this regime                                                            |

**Example**:

```python
MarketRegime(
    name='high_volatility',
    condition='VIX > 22 or Polymarket_Recession_Prob > 40%',
    description='Fear. Elevated premiums. Fast moves.',
    limits=RegimeLimits(
        max_beta_delta=100,
        max_negative_vega=-2500,
        min_daily_theta=600,
        max_gamma=20,
        allowed_strategies=['Ratio Backspreads', 'Short Strangles (Managed)', 'Jade Lizards']
    )
)
```

---

### RegimeLimits

Risk constraints for a specific market regime.

**Purpose**: Define portfolio boundaries that should not be exceeded in current conditions.

**Fields**:

| Field              | Type      | Description                                        |
| ------------------ | --------- | -------------------------------------------------- |
| max_beta_delta     | float     | Maximum SPX-weighted delta (directional risk)      |
| max_negative_vega  | float     | Maximum short volatility exposure (negative value) |
| min_daily_theta    | float     | Minimum daily theta collection                     |
| max_gamma          | float     | Maximum absolute gamma exposure                    |
| allowed_strategies | List[str] | Recommended strategies for this regime             |

**Usage**:

- Checked by `PortfolioTools.check_risk_limits()`
- Violations trigger dashboard alerts
- AI agent uses these for recommendation constraints

---

### InstrumentType (Enum)

Classification of financial instruments.

**Values**:

- `EQUITY`: Stock positions
- `OPTION`: Option contracts (calls/puts)
- `FUTURE`: Futures contracts
- `FUTURE_OPTION`: Options on futures

---

## Helper Classes

### PolymarketMarket

Represents a prediction market from Polymarket.

**Fields**:

| Field           | Type  | Description                                    |
| --------------- | ----- | ---------------------------------------------- |
| question        | str   | Market question text                           |
| yes_probability | float | Current probability of "Yes" outcome (0.0-1.0) |
| no_probability  | float | Current probability of "No" outcome            |
| market_id       | str   | Polymarket market identifier                   |

**Example**:

```python
PolymarketMarket(
    question='Will US enter recession in 2026?',
    yes_probability=0.38,
    no_probability=0.62,
    market_id='0x1234...'
)
```

---

### VIXData

Current VIX term structure data.

**Fields**:

| Field            | Type  | Description                                   |
| ---------------- | ----- | --------------------------------------------- |
| vix              | float | Current VIX spot price                        |
| vix3m            | float | VIX 3-month futures price                     |
| term_structure   | float | Ratio: VIX3M / VIX                            |
| is_backwardation | bool  | True if term_structure < 1.0 (bearish signal) |

**Example**:

```python
VIXData(
    vix=18.5,
    vix3m=20.2,
    term_structure=1.092,
    is_backwardation=False
)
```

---

## Data Flows

### Position Fetching Flow

```
User selects account
    ↓
BrokerAdapter.fetch_positions(account_id)
    ↓
Broker API call (IBKR/Tastytrade)
    ↓
Raw position data
    ↓
Transform to UnifiedPosition
    ↓
Fetch Greeks (if not included)
    ↓
Calculate days_to_expiration
    ↓
Calculate spx_delta
    ↓
Return List[UnifiedPosition]
```

### Regime Detection Flow

```
MarketDataTools.get_vix_data()
    ↓
VIXData (vix, vix3m, term_structure)
    ↓
PolymarketAdapter.get_recession_probability()
    ↓
Float (0.0-1.0) or None
    ↓
RegimeDetector.detect_regime(vix, term_structure, recession_prob)
    ↓
Evaluate conditions (Crisis > High > Low > Neutral)
    ↓
Return MarketRegime
```

### Risk Check Flow

```
List[UnifiedPosition] + VIXData
    ↓
PortfolioTools.get_portfolio_summary()
    ↓
Aggregate Greeks (sum)
    ↓
Calculate Theta/Vega ratio
    ↓
RegimeDetector.detect_regime()
    ↓
MarketRegime with limits
    ↓
PortfolioTools.check_risk_limits()
    ↓
Compare: portfolio totals vs regime limits
    ↓
Return: violations list + compliance status
```

---

## Database / Storage

**No database required for MVP.**

**Configuration Storage**:

- `config/risk_matrix.yaml`: Regime definitions and limits
- `config/agent_prompt.txt`: AI agent system prompt (optional)

**Cache Storage** (existing):

- Position snapshots: JSON files via `ibkr_portfolio_client.py`
- Greeks cache: In-memory or file-based via existing clients

**Future Considerations**:

- Historical position snapshots for performance tracking
- Trade history for P&L attribution
- Could use SQLite for simple persistence

---

## Relationships

```
UnifiedPosition ──(many)──> Portfolio Summary
                              │
                              ├─> Total Delta
                              ├─> Total Gamma
                              ├─> Total Theta
                              ├─> Total Vega
                              └─> Theta/Vega Ratio

VIXData + PolymarketMarket ──> RegimeDetector ──> MarketRegime

Portfolio Summary + MarketRegime ──> Risk Check ──> Violations List

UnifiedPosition ──(filter by DTE)──> Gamma Risk by DTE Bucket
```

---

## Validation & Constraints

**UnifiedPosition**:

- `quantity` can be negative (short positions)
- `theta` typically negative for long options, positive for short
- `vega` positive for long vol, negative for short vol
- `spx_delta` accounts for beta; not same as raw delta

**MarketRegime**:

- Conditions are mutually exclusive (prioritized: Crisis > High > Low > Neutral)
- Limits should be progressively tighter as volatility increases
- `max_negative_vega` is a negative number (threshold)

**Risk Checks**:

- `abs(spx_delta)` compared to `max_beta_delta` (not directional)
- `total_vega` can be negative (short vol); compare to `max_negative_vega`
- `total_gamma` absolute value compared to `max_gamma`

---

## Extension Points

**Adding New Broker**:

1. Create new adapter class inheriting from `BrokerAdapter`
2. Implement `fetch_positions()` → return `List[UnifiedPosition]`
3. Map broker-specific fields to UnifiedPosition fields
4. Add to dashboard broker selector

**Adding New Market Data Source**:

1. Create adapter in `adapters/`
2. Add method to `MarketDataTools`
3. Update `RegimeDetector.detect_regime()` to incorporate new signal

**Adding New Greeks**:

1. Add field to `UnifiedPosition` (e.g., `rho`, `vomma`)
2. Update adapters to populate field
3. Add to `PortfolioTools.get_portfolio_summary()`
4. Display in dashboard
