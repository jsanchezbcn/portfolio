# Portfolio Manager v2: Streaming & Intelligence Engine

## 1. System Identity

**Goal:** A high-frequency, event-driven portfolio manager that streams real-time Greeks, logs sophisticated signals to Postgres, and manages risk dynamically.
**Stack:** Python 3.11, PostgreSQL 16 (TimescaleDB preferred), Streamlit (UI), Docker.

## 2. Data Sources & Adapters (Streaming First)

### A. IBKR Client Portal API (CPAPI)

- **Endpoint:** `wss://localhost:5000/v1/api/ws`
- **Requirement:** Do NOT use polling for price data.
- **Implementation:** \* Authenticate via REST.
  - Open WebSocket.
  - Send `smd+{conid}+{"fields":["83","84","86"]}` to subscribe to tick data.
  - **Critical:** Handle the "heartbeat" every minute to keep connection alive.

### B. Tastytrade (DxLink)

- **Protocol:** DxLink (Complicated handshake).
- **Flow:** 1. Get `streamer-token` from REST API. 2. Connect to `wss://tasty-openapi-streamer...` 3. Subscribe to `Greeks` channel.
- **Target:** Latency under 200ms for portfolio-wide Delta updates.

### C. Signal Intelligence (The "Brain")

- **Gamma Exposure (GEX):** Calculate the "Zero Gamma" level daily.
  - _Signal:_ If SPX drops below Zero Gamma, switch Regime to "High Vol".
- **Vanna Flows:** Track how Market Maker Delta changes as time passes (Charm) and Vol changes (Vanna).
  - _Signal:_ "Approaching OpEx + High Open Interest Strike" = Pinning likely.

## 3. Database Schema (PostgreSQL)

**Table: `active_strategies`**

- `id` (UUID)
- `type` (Enum: RATIO_CALENDAR, STRANGLE, HEDGE)
- `thesis` (Text: "Selling vol in low VIX environment")
- `target_theta` (Float)
- `max_drawdown` (Float)

**Table: `trade_logs`**

- `id` (UUID)
- `strategy_id` (FK)
- `symbol`
- `entry_price`, `entry_iv`, `entry_delta`
- `exit_price`, `exit_iv`, `exit_delta`
- `pnl_realized`

**Table: `risk_snapshots` (Timeseries)**

- `timestamp` (PK)
- `net_portfolio_delta`
- `net_portfolio_vega`
- `spx_price`
- `vix_index`
- `regime_state` (Enum: LOW_VOL, HIGH_VOL, CRASH)

## 4. Sophisticated Guardrails

- **The "Correlation Break" Guard:**
  - _Logic:_ If `SPX` goes DOWN and `VIX` goes DOWN (unusual), disable all new short-vol entries immediately.
- **The "Liquidity" Guard:**
  - _Logic:_ If Bid/Ask spread on an option > 10% of price, mark price as "stale" and do not use for P&L calc.

## 5. UI Dashboard (Streamlit)

- **Page 1:** Real-time "Greek Speedometer" (Streaming updates).
- **Page 2:** "The Journal" (Postgres view of past trades vs. VIX at that time).
- **Page 3:** "Signal Lab" (Chart GEX levels vs. current price).
