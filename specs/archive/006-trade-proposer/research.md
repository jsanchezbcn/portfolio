# Research: Trade Proposer Agent

## 1. IBKR Margin Simulation (What-If API)

### Decision

Extend `IBKRAdapter` with a `simulate_margin` method that handles both `SOCKET` and `PORTAL` modes.

### Rationale

Accurate margin impact is critical for calculating the "Efficiency Score" (Risk Reduction / Capital Cost).

### Implementation Details

- **Socket Mode (`ib_async`)**:
  - Use `ib.whatIfOrder(Bag(...), MarketOrder(...))`.
  - Margin impact is retrieved from `OrderState.initMarginChange` and `OrderState.maintMarginChange`.
- **Portal Mode (REST)**:
  - Use `POST /v1/api/iserver/account/{accountId}/orders/whatif`.
  - Must use `secType: "BAG"` for multi-leg strategies to ensure offset credit.
  - Requires pre-fetching `conId` for all candidate legs via `/secdef/search` and `/secdef/info`.

## 2. PostgreSQL Schema: `proposed_trades`

### Decision

Store proposals in a table that supports multi-leg JSON blobs and a "Supersede" state.

### Rationale

Keeping all candidates in DB allows for auditability and risk review. The "Supersede" state (Requirement FR-014) ensures the dashboard doesn't display stale hedges.

### Proposed Fields

- `id`: Serial Primary Key
- `account_id`: String (e.g., U123456)
- `strategy_name`: String (e.g., "SPX Bear Put Spread")
- `legs_json`: JSONB (list of leg dicts: conId, action, qty)
- `net_premium`: Float
- `init_margin_impact`: Float
- `maintenance_margin_impact`: Float
- `efficiency_score`: Float
- `risk_reduction_delta`: Float
- `risk_reduction_vega`: Float
- `status`: String ("Pending", "Approved", "Rejected", "Superseded")
- `created_at`: Timestamp (UTC)

## 3. Efficiency Score Formula

### Decision

`Efficiency Score = (Weighted Risk Reduction) / (Max(Initial Margin Impact, 1.0) + Estimated Fees)`

### Rationale

- **Weighted Risk Reduction**: If the portfolio is long delta, reduction in delta is weighted higher. If short vega is breaching, vega reduction is weighted higher.
- **Capital Cost**: Initial margin is the primary cost. We use a floor of 1.0 to avoid division by zero for neutral-margin trades (e.g., some calendars).

## 4. Option C Notification Trigger

### Decision

Only trigger `NotificationDispatcher` and Streamlit `st.toast` if the Efficiency Score exceeds a configurable threshold (e.g., 0.5) OR if the breach is "Severe" (Regime = Crisis).

### Rationale

Reduces noise. User only gets notified when a high-conviction hedge is actually available and ready for review.
