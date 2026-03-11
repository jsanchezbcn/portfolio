# Portfolio Risk Manager — Complete Feature Guide

**Updated**: March 5, 2026  
**App Version**: Desktop (PySide6 + ib_async + PostgreSQL)  
**Architecture**: Event-driven with background agents and real-time IB Gateway connectivity

---

## Screen Breakdown

### 🏠 Main Window

**Components:**

- **Toolbar**: Connection control, refresh-all button, Copilot profile picker with token status
- **Tab Interface**: 8 main tabs for different workflows
- **Right Dock**: Order entry panel (collapsible/floatable)
- **Status Bar**: Connection status, running agents indicator

**Key Features:**

- Real-time IB Gateway connection management
- Auto-refresh on 60-second interval (configurable)
- Persistent window state and geometry
- Signal-based architecture for all updates

---

## 📊 Portfolio Tab

**Purpose**: Central dashboard for position overview, PnL tracking, and position management

### Account Summary Cards

- **Net Liquidation Value (NLV)** — Total account value
- **Cash Balance** — Available cash
- **Buying Power** — Margin buying power
- **Margin Used %** — Margin utilization
- **Unrealized P&L** — Floating gains/losses
- **Realized P&L** — Closed trade gains/losses

### Data Quality Banner

- ⚠️ Shows count of options missing Greeks (delta field is None)
- ⚠️ Shows count of stocks missing SPX delta
- Light amber background (#fff3cd) for visibility
- Auto-refreshes during position updates

### Positions Table (Dual View)

**Raw View** (default):

- All positions with full Greeks and risk metrics
- Symbol, Qty, Avg Cost, Market Price, Market Value
- Delta, Gamma, Theta, Vega, IV (for options)
- SPX Delta (for stocks)
- P&L (realized + unrealized)

**Trades View**:

- Grouped by underlying
- Entry price vs current price
- Win/loss indicators
- Right-click context menu: Buy, Sell, Roll (options only)

### Missing Greeks Highlighting

- Options with delta=None appear with amber background (#fff3cd)
- Amber text (#856404) for contrast
- Indicates data fetched but Greeks not yet available (usually market hours only)

### Refresh Button

- 🔄 Refresh Positions: Manually fetch latest data from IB
- Triggers batched Greek requests with automatic retry
- Retries up to 120 contracts on second pass if needed
- Uses cached Greeks from database for after-hours display

### Greeks Caching Strategy

- **During market hours**: Fetches live Greeks via IB reqMktData streams
- **After market close**: Falls back to last known Greeks from previous sessions
- **Persistence**: Greeks stored in PostgreSQL positions table
- **Cache loading**: Automatic on app startup from database

---

## 📈 Options Chain Tab

**Purpose**: Browse and trade options with real-time bid/ask and Greeks

### Chain Parameters

- **Underlying**: Dropdown of common underlyings (ES, MES, SPY, QQQ, NQ)
- **SecType**: FOP (futures options) or OPT (equity options)
- **Exchange**: CME, SMART, or CBOE
- **Expiry**: Editable combo for expiration selection
- **Fetch Chain**: Populate live options data
- **Clear & Reload**: Reset cache and fetch fresh data with Greeks

### Options Matrix

- Left half: Call options with Greeks
- Center: Strike price column
- Right half: Put options with Greeks
- Live bid/ask prices updated every 60 seconds
- Color-coded Greeks: positive/negative highlighting
- Click cells to add to order entry or view details

### Chain Features

- Double-click rows to send to order entry (creates a single-leg order)
- Click bid/ask cells to immediately add as multi-leg cart item
- "Send Combo" button routes all cart legs to order entry at once
- Greeks fetched with batching to avoid IB rate limits

---

## ⚠ Risk Tab

**Purpose**: Aggregate risk metrics and position exposure monitoring

### Portfolio Greeks

- **SPX Delta** — Index delta-weighted exposure to SPY/ES
- **Total Delta** — Sum of all position deltas
- **Total Gamma** — Convexity sensitivity
- **Total Theta** — Time decay value ($/day)
- **Total Vega** — IV sensitivity
- **Theta/Vega Ratio** — Time decay vs volatility cost

### Exposure Metrics

- **Position Count** — Total open positions
- **Options Count** — Number of option positions
- **Stock Count** — Number of stock positions
- **Gross Exposure** — Sum of abs(qty × price)
- **Net Exposure** — Sum of (qty × price)
- **Total Portfolio Value** — NLV equivalent

---

## 🧠 Strategies Tab

**Purpose**: Configure and monitor agent-managed strategy sessions with explicit stop-loss and profit-taking controls.

### Configuration Controls

- **Strategy Type** — choose initial 0DTE strategy template
- **Underlying** — target symbol for the strategy monitor
- **Stop-Loss %** — strategy-level loss cutoff
- **Take-Profit %** — strategy-level profit target
- **Gamma Threshold** — configurable Taleb warning trigger for 0-7 DTE exposure

### Risk Overlays

- **Taleb Gamma Warning** — flags elevated near-expiry absolute gamma (`0-7 DTE`) when threshold is breached
- **Sebastian Θ/V State** — computes theta/vega ratio and marks whether it is inside or outside the configured target band

### Risk Limits (YAML-Driven)

- Load `risk_matrix.yaml` to set position limits
- Display warnings when limits are breached
- Shows current value vs limit per Greek
- Color-coded: green (safe), yellow (warning), red (breach)

### Manual Refresh

- 🔄 Refresh Risk Metrics: Re-calculate all Greeks and exposure
- Status label shows last update time
- Auto-updates on position refresh

---

## 📋 Orders Tab

**Purpose**: View, modify, and cancel active orders

### Orders Table

- Order ID, Symbol, Type, Side, Status
- Quantity, Limit Price, Average Fill Price
- Order time and last update
- Sortable by any column
- Single-row selection mode

### Order Actions

- **🔄 Refresh Orders**: Fetch latest order status from IB
- **❌ Cancel Selected**: Cancel the selected order (confirmation dialog)
- **🚨 Cancel All Orders**: Bulk cancel all open orders (safety confirmation)
- **✏️ Modify Price**: Edit limit price of selected order

### Order Status Tracking

- DRAFT, PENDING, SUBMITTED, FILLED, CANCELED, ERROR
- Real-time status updates via IB event stream
- Order fills auto-sync to database

---

## 📓 Journal Tab

**Purpose**: Trade documentation and execution history

### Strategy Journal Sub-Tab

- Free-form text notes for trade setups
- Record macro themes, opportunity identification
- Entry/exit reasoning
- Mark-to-market reviews
- Searchable and persistent

### Order Log Sub-Tab

- Complete execution history
- Order ID, Symbol, Quantity, Fill Price, Commission
- Execution time and order status
- Real-time updates as fills arrive
- Group by symbol or date
- Export for performance analysis

### Integration

- Order fills from IB auto-populate order log
- Links to corresponding journal entries
- Performance tracking per trade

---

## 🤖 AI / Risk Tab

**Purpose**: Intelligent risk management and AI-suggested hedges

### Model & Scenario Selection

- **Model Dropdown**: Available LLM models (GPT-5-mini, GPT-4.1, GPT-4o, etc.)
- **Scenario**: Market condition context (Auto, Low Vol, Neutral, High Vol, Crisis)
- **🔄 Refresh Models**: Update available model list from Copilot SDK

### AI Tools

- **📡 Refresh Context**: Load latest portfolio Greeks and market data for AI analysis
- **🛡 Run Risk Audit**: AI analyzes portfolio for concentration, sector imbalance, tail risk
- **✨ Suggest Trades**: AI proposes hedges or tactical adjustments based on scenario

### Conversation Interface

- Chat history displays AI responses
- Ask follow-up questions about risk, Greeks, margin, positioning, hedges
- 💬 Ask AI: Send user question to LLM with portfolio context

### AI Trade Suggestions Table

- Legs: Comma-separated contract specs
- Δ Change: Expected delta impact
- Θ Cost: Time decay cost ($/day)
- Rationale: Why the AI recommends this trade
- Select and authorize suggested trades
- **WhatIf Margin**: Click to simulate margin impact before submitting
- Margin impact displayed: init margin change, maintenance margin change
- Uses `whatIfOrderAsync()` for accurate IB margin simulation

**Margin Impact Display** (WhatIf):

- Initial Margin Change: $ impact on required margin
- Maintenance Margin Change: $ impact on maint margin requirement
- Equity with Loan Change: $ impact on portfolio equity
- Real-time: Based on current portfolio + proposed trade
- 15-second timeout: If IB Gateway is slow, shows timeout error

### LLM Backend

- **Primary**: GitHub Copilot SDK via `copilot` CLI (no API key needed)
- **Fallback**: Copilot SDK + BYOK (if OPENAI_API_KEY set)
- **Emergency**: Direct OpenAI API (last resort only)
- All LLM calls include live portfolio context (Greeks, VIX, margin)

---

## 💹 Market Data Tab

**Purpose**: Real-time quote lookup and watchlist tracking

### Quick Quote Lookup

- **Symbol**: Enter symbol (SPY, ES, QQQ, etc.)
- **Type**: Select security type (STK, FUT)
- **Exchange**: Choose exchange (SMART, CME, CBOE, GLOBEX)
- **📊 Get Quote**: Fetch real-time price and Greeks
- **➕ Load Defaults**: Pre-populate SPY, QQQ, IWM
- **⭐ Add Favorite**: Save symbol to persistent watchlist
- **🔄 Refresh Favorites**: Update all saved watchlist symbols

### Watchlist Table

- Symbol, Type, Exchange, Last Price, Bid, Ask
- Change, % Change (color-coded)
- Greeks for options (Delta, Gamma, Theta)
- 60-second auto-refresh timer (running in background)
- Search/filter by symbol

### Market Context

- VIX level for volatility regime
- ES/SPY prices as market indices
- Economic calendar integration (optional)
- Persistent favorites saved to SQLite

---

## 💰 Order Entry Panel (Right Dock)

**Purpose**: Build and submit multi-leg orders with real-time bid/ask

### Add-Leg Form

- **Symbol**: Contract symbol (ES, SPY, QQQ, etc.)
- **SecType**: Dropdown (FOP, OPT, STK, FUT)
- **Exchange**: Dropdown (CME, SMART, CBOE, GLOBEX)
- **Quantity**: Spin box (1–999)
- **Action**: Buy or Sell
- **Strike** (Options only): Spin box with $5 increments
- **Right** (Options only): Call (C) or Put (P)
- **Expiry** (Options only): YYYYMMDD format
- **➕ Add Leg**: Add contract to staged order

### Staged Legs Table

- Leg number, symbol, SecType, action, qty, strike, right, expiry
- Real-time bid/ask from market data stream
- Select leg to modify or remove
- 🗑 Remove: Delete selected leg
- 🧹 Clear All: Delete all legs and reset

### Bid/Ask Streaming

- **Auto-refresh every 5 seconds** (while legs are staged)
- Display best bid and ask prices
- Shows bid/ask quality (national best bid/offer)
- Price slider: Adjust limit price between bid and ask
- Bid/ask greensill out after market close (last known values)

### Order Rationale

- Free-form text field for trade reasoning
- Persisted with order for audit trail
- Visible in order log

### Order Types

- LIMIT (default, with adjustable limit price)
- MARKET (market order)
- STOP (stop-loss)
- Transmit immediately or stage as "DRAFT"

### Submission

- 📤 Submit Order: Send order to IB
- Risk checks before submission (margin, Greeks limits)
- Confirmation dialog shows order preview
- Order ID returned and auto-logged

---

## 🔧 Copilot Profile Picker (Toolbar)

**Purpose**: Switch between personal and work GitHub Copilot tokens

### Token Status Indicators

- **✅** = Token configured and ready
- **❌** = Token missing or invalid
- Click status to view configuration details

### Features

- **Profile Selection**: Personal / Work dropdown
- **ℹ Info Button**: Show token environment variable details
- **Profile Change Signal**: Automatically updates AI tab to use selected token
- **Token Validation**: Checks token availability on profile switch

### Token Configuration

- Environment variables: `GITHUB_COPILOT_TOKEN_PERSONAL`, `GITHUB_COPILOT_TOKEN_WORK`
- Tokens stored in `.env` file
- Read on app startup and persisted in `desktop/config/prefs.json`
- Profile state saved between sessions

---

## 🖥️ Background Processing (Agents)

### AgentRunner (Main Worker)

- Starts on app load, runs 3 concurrent tasks
- **Task 1**: Risk auditor (evaluates portfolio every N seconds)
- **Task 2**: Market data poller (updates watchlist)
- **Task 3**: Order status monitor (polls order fills)
- Graceful shutdown on app close

### LLM Risk Auditor Agent

- **Frequency**: Every 30–60 seconds
- **Input**: Live portfolio Greeks, exposed positions, VIX
- **Action**: Runs risk audit via AI (Copilot SDK)
- **Output**: Risk violations, suggested hedges
- **Fallback**: Deterministic rules if LLM fails
- **Storage**: Risk audit results persisted to database

### Market Intelligence Agent (Optional)

- Monitors news feeds, earnings calendar
- Integrates with TradingView or Finnhub API
- Alerts on major market events
- Feeds context into AI risk analysis

---

## 💾 Data Persistence

### PostgreSQL Database Tables

- **positions**: Current portfolio with Greeks, P&L, synced timestamp
- **orders**: Order history with status, price, margin impact
- **fills**: Execution records with commission, realized P&L
- **account_snapshots**: Historical NLV, cash, margin, unrealized PnL
- **risk_snapshots**: Historical Greeks and portfolio metrics
- **portfolio_greeks** (legacy): Alternative greeks storage

### SQLite Local Storage

- **Watchlist favorites**: Symbol, type, exchange
- **Preferences**: Active Copilot profile, window geometry
- **Trade notes**: Journal entries and strategy ideas

### Greeks Caching

- Automatically loaded from positions table on app startup
- Persisted whenever positions are synced (every ~30 seconds)
- Fallback to last known Greeks when live data unavailable
- Configurable batch sizes (default: 40 contracts per batch)

---

## 🌐 API Integrations

### Interactive Brokers (IB Gateway)

- **Connection**: ib_async library (async wrapper)
- **Data**: Live positions, orders, account values, Greeks
- **Greek Sources**:
  - Generic ticks 100, 101, 104, 106 (delta, gamma, iv, vega, theta)
  - Batched requests to avoid rate limits
  - Automatic retry with fallback batch size
- **Order Management**: Submit, modify, cancel orders

### GitHub Copilot SDK

- **Authentication**: Via `copilot` CLI + system keyring (gh auth login)
- **Models**: Dynamically loaded from SDK
- **Features**:
  - Session-based streaming
  - BYOK (Bring-Your-Own-Key) with OPENAI_API_KEY
  - No API key required (uses GitHub subscription)

### TradingView / Finnhub (Optional)

- News sentiment analysis
- Option implied volatility quotes
- Earnings calendar

---

## ⚙️ Configuration Files

### `.env` (Secrets)

```bash
GITHUB_COPILOT_TOKEN_PERSONAL=gho_xxxxxxxx...
GITHUB_COPILOT_TOKEN_WORK=gho_yyyyyyyy...
OPENAI_API_KEY=sk_...  # Optional; for BYOK or fallback
IB_HOST=127.0.0.1
IB_PORT=4001
IB_CLIENT_ID=30
DB_DSN=postgresql://portfoliouser:pass@localhost:5432/portfolio_engine
```

### `risk_matrix.yaml` (Risk Limits)

```yaml
delta:
  limit: 500
  warning: 400
gamma:
  limit: 150
  warning: 120
theta:
  target: 100 # daily theta income goal
vega:
  limit: -5000
  warning: -4000
```

### `desktop/config/prefs.json` (UI State)

```json
{
  "copilot_active_profile": "personal",
  "window_geometry": {...},
  "favorites": [...]
}
```

---

## 🔄 Refresh Intervals

| Component               | Interval               | Trigger                |
| ----------------------- | ---------------------- | ---------------------- |
| Portfolio positions     | On-demand + 60s auto   | Manual or timer        |
| Options chain           | On-demand + 60s auto   | Manual or timer        |
| Market data (watchlist) | 1–5 seconds            | Auto while tab open    |
| Order status            | 10 seconds             | Polling + IB events    |
| Risk metrics            | 30 seconds             | Agent task             |
| Greeks fetch            | Batched 1.8s per batch | Position refresh       |
| Greeks retry            | 1.2s per batch         | Auto retry for missing |

---

## 🛡️ Risk Management Features

### Position Limits

- Delta, gamma, vega limits enforced before order submission
- Real-time breach detection and alerts
- Color-coded dashboard: green → yellow → red

### Greeks Monitoring

- All positions calculated with per-leg Greeks scaling (qty × multiplier)
- SPX delta weighting for stock positions (beta-adjusted)
- Missing Greeks highlighted in portfolio table
- Auto-fetch retry with priority for liquid contracts

### Margin Monitoring

- Pre-submission margin impact calculation
- Real-time margin utilization %
- Buying power display
- Orders blocked if margin insufficient

### Order Confirmation

- Order preview before submission
- Greeks impact summary
- Margin impact preview
- Manual authorization required

---

## 🔌 Command-Line Interface (Optional)

**Location:** `scripts/portfolio_cli.py`

### Commands

```bash
python scripts/portfolio_cli.py positions       # Show all positions
python scripts/portfolio_cli.py greeks          # Full Greeks pipeline
python scripts/portfolio_cli.py chain SPY       # Options chain snapshot
python scripts/portfolio_cli.py orders          # Active orders
python scripts/portfolio_cli.py audit           # Deterministic risk audit
```

---

## 📚 Skill Ecosystem (Optional AI-Driven Tools)

- **ExplainPerformance**: Analyze trade via AI with Greeks comparison
- **RiskAuditor**: Deterministic + AI-driven risk checks
- **AgentRunner**: Background workers for polling
- **TokenManager**: Copilot profile persistence

---

## 🚀 Quick Start Checklist

- [ ] Install GitHub Copilot CLI: `gh copilot` (requires GitHub account)
- [ ] Authenticate: `gh auth login`
- [ ] Set `.env` with `GITHUB_COPILOT_TOKEN_PERSONAL` and `GITHUB_COPILOT_TOKEN_WORK`
- [ ] Start IB Gateway on localhost:4001
- [ ] Run `./start_desktop.sh` to launch app
- [ ] Portfolio tab should connect and load positions within 10–20 seconds
- [ ] Select Copilot profile in toolbar
- [ ] Check AI / Risk tab to run portfolio audit

---

## Known Limitations

- **After-hours Greeks**: Display cached values; refresh not available after market close
- **Stock options**: May show 0 Greeks — use TradingView or Finnhub as supplement
- **Greek data gaps**: Market halts or data farm outages may cause temporary missing Greeks
- **LLM timeouts**: Copilot SDK may timeout if API is slow (defaults to 45s + 20s grace)
- **IB Gateway crashes**: App requires manual reconnect (auto-reconnect not implemented)

---

## Performance Notes

- **Memory**: ~200–400 MB for typical portfolio (100–300 positions)
- **CPU**: Minimal (~5% idle); spikes during Greeks fetch (batched to avoid 100% CPU)
- **Network**: 1–2 Mbps downstream for live data; async I/O prevents blocking
- **Database**: PostgreSQL 12+ recommended; SQLite alternative for small portfolios
- **LLM latency**: 5–30 seconds for risk audit (depends on Copilot SDK + GPT model)

---

End of Feature Guide
