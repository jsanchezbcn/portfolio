# Portfolio Manager & Volatility Engine: Project Specification

## 1. Project Overview

**Goal:** Build a "Risk-First" Portfolio Management System that aggregates multi-broker positions (IBKR, Tastytrade, Crypto) and calculates dynamic risk metrics based on the current Volatility Regime.
**Core Philosophy:** Risk limits (Delta, Gamma, Vega) must adapt to the market environment. A -500 Delta is safe in a 10 VIX market but dangerous in a 35 VIX market.

## 2. System Architecture

### A. Data Ingestion Layer (The "Senses")

The system must ingest data from multiple disparate sources and normalize it into a single format.

1.  **Interactive Brokers (IBKR):**
    - **Source:** `ib_insync` library or TWS API.
    - **Data:** Positions, Real-time Greeks, Account Net Liquidation.
2.  **Tastytrade:**
    - **Source:** `tastytrade` Python package.
    - **Data:** Options chains, specific "high theta" positions.
3.  **Alternative / Macro Data:**
    - **Polymarket:** Use GraphQL/HTTP API to fetch probabilities for "Recession", "Rate Cuts", or "Inflation".
    - **Fear & Greed Index:** Scrape or fetch via API (CNN/Alternative.me) to gauge retail sentiment.
    - **Crypto Volatility:** Fetch BTC/ETH Volatility Index (DVOL) via Deribit API as a leading indicator for 24/7 global risk.
4.  **Market Regime Data:**
    - **VIX Index:** Real-time feed.
    - **VVIX (Vol of Vol):** For detecting regime changes.
    - **Term Structure:** Contango vs. Backwardation (VIX vs VIX3M).

### B. Normalization Layer (The "Translator")

All incoming data must be converted into a `UnifiedPosition` object to avoid "vendor lock-in" logic in the risk engine.

- **Class:** `UnifiedPosition`
- **Fields:**
  - `symbol` (str): E.g., "SPX", "AAPL".
  - `instrument_type` (enum): Equity, Option, Future, FutureOption.
  - `quantity` (float): +/- size.
  - `delta` (float): Beta-weighted to SPY.
  - `gamma` (float): Beta-weighted.
  - `vega` (float): Aggregated portfolio impact.
  - `theta` (float): Daily decay.
  - `expiration` (date): For "Gamma Risk" calculation (0-7 DTE vs 60 DTE).

### C. The Risk Engine (The "Brain")

This module calculates "Allowable Risk" based on the "Current Regime."

**Logic Flow:**

1.  Fetch `MarketData` (VIX, Skew, Term Structure).
2.  Determine `Regime` (Low, Neutral, High, Crisis).
3.  Load `RiskConfig` for that specific regime.
4.  Compare `PortfolioAggregates` vs `RiskConfig`.
5.  Alert/Suggest Hedges if limits are breached.

---

## 3. Dynamic Risk Configuration (YAML Structure)

The system must load its rules from `config/risk_matrix.yaml`.

```yaml
regimes:
  low_volatility:
    condition: "VIX < 15 and TermStructure > 1.10"
    description: "Complacency. Market grinds up. Cheap hedges available."
    limits:
      max_beta_delta: 600 # Allow more directional risk
      max_negative_vega: -500 # Don't sell cheap vol
      min_daily_theta: 100
      max_gamma: 50
      allowed_strategies:
        ["Long Calendars", "Debit Spreads", "Reverse Iron Condors"]

  neutral_volatility:
    condition: "15 <= VIX <= 22"
    description: "Standard operating environment."
    limits:
      max_beta_delta: 300
      max_negative_vega: -1200
      min_daily_theta: 300
      max_gamma: 35
      allowed_strategies: ["Iron Condors", "Strangles", "Credit Spreads"]

  high_volatility:
    condition: "VIX > 22 or Polymarket_Recession_Prob > 40%"
    description: "Fear. Elevated premiums. Fast moves."
    limits:
      max_beta_delta: 100 # Tighten direction
      max_negative_vega: -2500 # Lean into short vol (sell the fear)
      min_daily_theta: 600 # Demand higher payment for risk
      max_gamma: 20 # WATCH OUT. Gamma kills here.
      allowed_strategies:
        ["Ratio Backspreads", "Short Strangles (Managed)", "Jade Lizards"]

  crisis_mode:
    condition: "VIX > 35 or VVIX > 150"
    description: "Panic. Liquidity drying up."
    limits:
      max_beta_delta: 0 # Force Delta Neutrality
      max_negative_vega: 0 # Stop selling vol; market could gap
      min_daily_theta: 0 # Survival mode
      max_gamma: 10
      allowed_strategies: ["Long Puts", "Cash", "Long Volatility"]
```
