# Mini Hedge Fund: System Improvement Plan

**Date**: February 25, 2026
**Vision**: Transform the current portfolio risk manager into a personal mini hedge fund — an end-to-end automated alpha system that uses options as the primary instrument, multi-agent AI for signal generation and execution, and institutional-grade risk management.

**Inspired by**: Chris Camillo's social arbitrage, TradingAgents (arXiv 2412.20138), FinGPT, and the multi-agent architecture outlined in `ideas.txt`.

---

## Table of Contents

1. [Current System Assessment](#1-current-system-assessment)
2. [Gap Analysis: Where We Are vs Where We Need to Be](#2-gap-analysis)
3. [Phase 0: Stabilization & Bug Fixes](#phase-0-stabilization--bug-fixes-1-2-weeks)
4. [Phase 1: Analytics Foundation — P&L, VaR, Sharpe](#phase-1-analytics-foundation-3-4-weeks)
5. [Phase 2: Options-First Alpha Engine](#phase-2-options-first-alpha-engine-4-6-weeks)
6. [Phase 3: Social Arbitrage & Sentiment Pipeline](#phase-3-social-arbitrage--sentiment-pipeline-4-5-weeks)
7. [Phase 4: Multi-Agent Synthesis & Decision Layer](#phase-4-multi-agent-synthesis--decision-layer-3-4-weeks)
8. [Phase 5: Automated Execution & Position Lifecycle](#phase-5-automated-execution--position-lifecycle-3-4-weeks)
9. [Phase 6: Backtesting & Paper Trading Validation](#phase-6-backtesting--paper-trading-validation-3-4-weeks)
10. [Phase 7: Production Hardening & Live Trading](#phase-7-production-hardening--live-trading-2-3-weeks)
11. [Architecture Target State](#architecture-target-state)
12. [Risk Controls Throughout](#risk-controls-throughout)

---

## 1. Current System Assessment

### What Exists and Works

| Component | Status | Maturity |
|---|---|---|
| IBKR adapter (positions, Greeks, options chain) | Working | High |
| Tastytrade adapter (positions, Greeks via DxLink) | Working | Medium |
| Regime detection (VIX/term-structure/Polymarket) | Working (3 divergent implementations) | Medium |
| Risk matrix (YAML-based per-regime limits) | Working | High |
| Portfolio Greeks aggregation | Working | High |
| Beta-weighted SPX delta | Working | High |
| Dashboard (Streamlit, ~2,500 lines) | Working | High |
| News sentiment (Alpaca/Finnhub/NewsAPI → LLM scoring) | Working | Medium |
| Arbitrage hunter (box spread, put-call parity) | Working | Medium |
| Trade proposer (breach → candidate → persist) | Working | Medium |
| Execution engine (multi-leg BAG combos, WhatIf sim) | Working | High |
| Trade journal (SQLite, fills + VIX/regime at fill) | Working | Medium |
| Telegram bot (regime, Greeks, positions, analyze) | Working | Medium |
| Event bus (PostgreSQL NOTIFY/LISTEN) | Working | Low |
| Streaming (IBKR WebSocket + Tastytrade DxLink) | Working | Medium |
| Capital allocator (half-Kelly stub) | Stub only | Low |
| Execution agent (TWAP/VWAP stubs) | Stub only | Low |
| Market intelligence agent | Partially working | Low |

### What's Missing for a Mini Hedge Fund

1. **No P&L time series** — cannot calculate Sharpe, Sortino, VaR, or any performance metric
2. **No strategy-level tracking** — positions exist individually, not grouped into strategies
3. **No backtesting** — no way to validate signals before deploying capital
4. **No social data pipeline** — Reddit, Google Trends, TikTok/Instagram not integrated
5. **No options flow analysis** — GEX, Vanna, unusual activity, dark pool data absent
6. **No bull/bear debate synthesis** — agents don't converge on a unified thesis
7. **No position lifecycle management** — no automated exit rules or profit-taking
8. **No stress testing** — cannot model "what if VIX hits 40?"
9. **Capital allocator disconnected** — Kelly criterion not wired to live win rates
10. **11 critical bugs** in active code paths (see AUDIT_AND_ROADMAP.md)
11. **3 divergent regime detectors** — inconsistent risk signals across system

---

## 2. Gap Analysis

### ideas.txt Vision vs Current Reality

| Vision (from ideas.txt) | Current State | Gap Severity |
|---|---|---|
| **Social Scraper Agent** (Reddit, Instagram, Google Trends) | Not implemented | HIGH |
| **Camillo Sentiment Engine** (velocity + context scoring) | Basic news→LLM scoring exists (NewsSentry) | MEDIUM |
| **Macro & Volatility Agent** (VIX regime, term structure) | RegimeDetector exists but 3 divergent versions | MEDIUM |
| **Quant & Options Strategist** (IV rank, spread selection) | ProposerEngine generates spreads from breaches only | HIGH |
| **Risk & Execution Manager** (Kelly sizing, smart limits) | Execution engine works; capital allocator is a stub | HIGH |
| **Monitoring & Logging** (P&L tracking, anomaly detection) | Greeks snapshots only; no P&L series | HIGH |
| **Debate/Synthesis Agent** (bull vs bear convergence) | Not implemented | HIGH |
| **Technical Agent** (price action, RSI, MACD, patterns) | Not implemented | MEDIUM |
| **Options Flow Agent** (dark pool, unusual volume) | Not implemented | MEDIUM |
| **Fundamentals Agent** (earnings, revenue, short interest) | Not implemented | LOW |
| **Backtesting framework** | Not implemented | HIGH |
| **LangGraph orchestration** | Not implemented (event bus exists) | MEDIUM |
| **FinBERT/FinLlama** for fast sentiment | Using generic GPT-4o-mini | MEDIUM |
| **pgvector embedding memory** (agents remember past analysis) | Not implemented | LOW |

---

## Phase 0: Stabilization & Bug Fixes (1-2 weeks)

**Goal**: Fix all 11 critical bugs and 6 security vulnerabilities. Zero new features. The foundation must be solid before building on top.

### Tasks

| ID | Task | Effort |
|---|---|---|
| P0-01 | Fix strike double-division in `proposer_engine.py` (BUG-001) | 15 min |
| P0-02 | Add `vix` field to `BreachEvent`, wire through proposer (BUG-002) | 30 min |
| P0-03 | Deduplicate email send in `notification_dispatcher.py` (BUG-003) | 15 min |
| P0-04 | Fix VIX monotonic growth in `market_intelligence.py` (BUG-004) | 30 min |
| P0-05 | Fix `LLMRiskAuditor` constructor call in `portfolio_worker.py` (BUG-005) | 10 min |
| P0-06 | Consolidate `OrderStatus` enum duplicates (BUG-006 + BUG-007) | 1.5 hr |
| P0-07 | Fix asyncio `__main__` in 4 agent files (BUG-008) | 30 min |
| P0-08 | Add `aiosqlite` to `requirements.txt` (BUG-009) | 5 min |
| P0-09 | Fix `NewsSentry()` missing args in dashboard (BUG-010) | 15 min |
| P0-10 | Apply `contract_multiplier` in `tastytrade_adapter.fetch_greeks` (BUG-011) | 30 min |
| P0-11 | Parameterize EventBus NOTIFY payload (SEC-001) | 30 min |
| P0-12 | Whitelist columns in `circuit_breaker._db_insert` (SEC-002) | 30 min |
| P0-13 | Surface IBKR reply challenges to user (SEC-004) | 1 hr |
| P0-14 | Consolidate 3 regime detectors into single `RegimeService` (ARCH-002) | 1 day |
| P0-15 | Replace all `datetime.utcnow()` with `datetime.now(timezone.utc)` | 1 hr |

### Testing at End of Phase 0

- [ ] All existing tests pass: `pytest tests/ -x`
- [ ] No `CRITICAL` or `HIGH` bugs remain in AUDIT_AND_ROADMAP.md
- [ ] `RegimeDetector` returns consistent results when called from dashboard, proposer, and market intelligence agent
- [ ] Trade proposer generates correct OTM strikes (not ATM due to double-division)
- [ ] Telegram `/regime` and dashboard regime panel show matching regime names
- [ ] `ruff check .` passes with zero errors

---

## Phase 1: Analytics Foundation (3-4 weeks)

**Goal**: Build the P&L infrastructure that every hedge fund needs. Without daily P&L, you cannot compute risk-adjusted returns, VaR, or strategy attribution. This unlocks all downstream analytics.

### Why This Phase Is Critical for Options

Options P&L is non-linear — you need Greeks decomposition to understand whether you're making money from theta decay (desired) or random delta exposure (undesired). The Sebastian |Θ|/|V| ratio already in the dashboard is useless without historical tracking.

### Tasks

| ID | Task | Description | Effort |
|---|---|---|---|
| P1-01 | `portfolio_returns` table | PostgreSQL table: `(date, account_id, nlv, daily_return, margin_used, regime_name)` | 2 hr |
| P1-02 | EOD Snapshot Worker | New scheduler job: capture NLV, Greeks, regime at market close (4:15 PM ET); handle options after-hours pricing | 1 day |
| P1-03 | Greeks P&L Decomposition | `analytics/greeks_attribution.py`: Decompose daily P&L into Δ, Γ, Θ, V, ρ, and "unexplained" components using Taylor expansion | 2 days |
| P1-04 | VaR/CVaR Calculator | `analytics/risk_metrics.py`: Historical VaR (95%/99%), parametric VaR, Cornish-Fisher VaR for fat tails (options portfolios have extreme kurtosis) | 2 days |
| P1-05 | Sharpe / Sortino / Calmar | Rolling 63-day and 252-day risk-adjusted return metrics | 1 day |
| P1-06 | Max Drawdown Tracker | Real-time watermark tracking with duration analysis and underwater chart | 1 day |
| P1-07 | Theta Decay Efficiency | Track actual theta collected vs theoretical: `theta_efficiency = actual_pnl_from_theta / expected_theta_pnl` | 1 day |
| P1-08 | Options-Specific Metrics | IV Rank, IV Percentile, HV/IV ratio tracking per underlying (not just point-in-time) | 1 day |
| P1-09 | Analytics Dashboard Tab | New "Performance" page in Streamlit: equity curve, drawdown chart, rolling Sharpe, Greeks attribution waterfall, VaR gauge | 2 days |
| P1-10 | Margin Utilization Series | Historical margin usage chart — critical for options (margin expands in vol spikes) | 1 day |
| P1-11 | Comprehensive Tests | 40+ tests covering all metrics, edge cases (empty series, single day, negative returns), and the EOD snapshot worker | 2 days |

### Testing at End of Phase 1

- [ ] `pytest tests/test_analytics.py` passes with 40+ tests
- [ ] EOD worker correctly captures NLV snapshots for paper trading account
- [ ] Dashboard "Performance" tab renders: equity curve, rolling Sharpe (63d), VaR gauge, Greeks attribution waterfall
- [ ] Greeks decomposition: `Δ_pnl + Γ_pnl + Θ_pnl + V_pnl + unexplained ≈ actual_daily_pnl` (within 5% tolerance)
- [ ] VaR/CVaR numbers are reasonable: 1-day 95% VaR should be ~1-3% of NLV for a typical options portfolio
- [ ] Historical IV rank/percentile matches known sources (check against Market Chameleon for 2-3 symbols)
- [ ] Theta decay efficiency shows meaningful signal (>1.0 means outperforming expected theta)

---

## Phase 2: Options-First Alpha Engine (4-6 weeks)

**Goal**: Build the agents that generate options-specific alpha signals — IV edge detection, volatility surface analysis, options flow intelligence, and strategy-aware trade generation. This is the core of the mini hedge fund's "brain" for options.

### New Agents

#### 2A. Volatility Surface Analyst

Scans for structural edges in the vol surface that options market makers exploit.

| ID | Task | Description | Effort |
|---|---|---|---|
| P2-01 | IV Rank / IV Percentile Scanner | For each watchlist underlying: compute 52-week IV rank, flag when <15 (cheap) or >85 (expensive). Use IBKR historical vol data or Barchart API | 2 days |
| P2-02 | Skew Monitor | Track 25-delta put/call skew for SPX, individual names. Alert when skew is historically extreme (tail hedging opportunity or overpriced puts to sell) | 2 days |
| P2-03 | Term Structure Scanner | Compare front-month IV vs back-month IV. Contango = sell calendars; backwardation = buy calendars. Cross-reference with VIX term structure | 1 day |
| P2-04 | Vol Surface Model | Build a simple parameterized vol surface (SABR or SVI) to identify mispriced strikes/expirations vs the fitted model | 3 days |
| P2-05 | Realized vs Implied Divergence | Track 10/20/30-day realized vol vs current IV. When IV >> HV, premium selling edge. When IV << HV, premium buying edge. The existing IV/HV analysis is point-in-time — need time series | 2 days |

#### 2B. Options Flow Intelligence Agent

Detect smart money positioning from published flow data.

| ID | Task | Description | Effort |
|---|---|---|---|
| P2-06 | Unusual Whales / Barchart Integration | Fetch unusual options activity data: large block trades, unusual volume (>2x average OI) | 2 days |
| P2-07 | Put/Call Volume Ratio Tracker | Track aggregate and per-symbol P/C ratio. Extreme readings are contrarian signals | 1 day |
| P2-08 | GEX (Gamma Exposure) Calculator | Compute dealer gamma exposure from open interest data. Zero-gamma level is a key support/resistance level | 2 days |
| P2-09 | Vanna/Charm Flow Model | Estimate how MM hedging flows create directional pressure as time passes (charm) and vol moves (vanna) | 2 days |
| P2-10 | OpEx Pinning Detector | Identify high-OI strikes near expiration — price tends to gravitate toward max-pain/max-gamma levels | 1 day |

#### 2C. Strategy-Aware Trade Generator

Replace the current "breach → hedge" proposer with a strategy-aware options trader.

| ID | Task | Description | Effort |
|---|---|---|---|
| P2-11 | Strategy Model & Grouping | `models/strategy.py`: define strategy types (Iron Condor, Calendar, Diagonal, Jade Lizard, etc.) with expected Greeks profiles | 1 day |
| P2-12 | Strategy Auto-Tagger | Analyze existing positions' leg structure to auto-classify into named strategies | 2 days |
| P2-13 | Strategy-Level P&L | Track realized + unrealized P&L per strategy group, including partial fills | 2 days |
| P2-14 | Regime-Aware Strategy Selector | Given current regime + vol surface signals, select optimal strategy type. E.g., low IV rank + neutral regime → Iron Condor; high IV rank + high VIX → ratio backspread | 2 days |
| P2-15 | Leg Optimizer | For the selected strategy type, optimize strikes/expirations for max risk-adjusted expected value using the vol surface model (not just ATM ± fixed wing width) | 3 days |
| P2-16 | Multi-Underlying Alpha Scorer | Score underlyings by composite signal: IV edge + flow signal + sentiment + technical. Rank and select top N for trade generation | 2 days |

### Testing at End of Phase 2

- [ ] `pytest tests/test_vol_surface.py` — IV rank matches Market Chameleon within 5 percentile points for SPY, QQQ, IWM
- [ ] `pytest tests/test_options_flow.py` — GEX calculation produces reasonable zero-gamma levels (within 1% of SPX spot)
- [ ] `pytest tests/test_strategy_model.py` — auto-tagger correctly identifies iron condors, calendars, and verticals from test positions
- [ ] Strategy-level P&L sums to portfolio P&L (no leakage)
- [ ] Regime-aware strategy selector outputs different strategy types for different regimes (low_vol → calendars/IC, high_vol → ratio backspreads)
- [ ] Vol surface model fits historical data with RMSE < 2 vol points
- [ ] Dashboard shows new "Options Intelligence" panel with IV rank heatmap, GEX chart, skew chart
- [ ] Paper trade: generate 3 strategy candidates using the new engine and verify legs/strikes are reasonable

---

## Phase 3: Social Arbitrage & Sentiment Pipeline (4-5 weeks)

**Goal**: Implement the Chris Camillo-style social arbitrage pipeline — detect consumer/cultural trends on social media before they're priced into equities, then express the thesis through options for leveraged, defined-risk exposure.

### New Agents

#### 3A. Social Data Scraper

| ID | Task | Description | Effort |
|---|---|---|---|
| P3-01 | Reddit Scraper (PRAW) | Monitor `r/wallstreetbets`, `r/stocks`, `r/investing`, and niche consumer subs. Track mention velocity (mentions/hour vs 7-day average). Store raw data in PostgreSQL | 2 days |
| P3-02 | Google Trends Integration (pytrends) | Track search interest velocity for watchlist tickers + key consumer brands. Spike detection: >2σ above 30-day mean | 1 day |
| P3-03 | News Feed Expansion | Enhance existing `NewsSentry` to add Benzinga Pro API and GNews — broader coverage than Alpaca/Finnhub alone | 1 day |
| P3-04 | Social Data Schema | PostgreSQL tables: `social_mentions` (source, symbol, mention_count, sentiment, timestamp), `social_velocity` (symbol, velocity_score, z_score, timestamp) | 1 day |

#### 3B. Camillo Sentiment Engine (Enhanced)

| ID | Task | Description | Effort |
|---|---|---|---|
| P3-05 | FinBERT Integration | Replace GPT-4o-mini for sentiment scoring with FinBERT (HuggingFace `ProsusAI/finbert`). Faster, cheaper, purpose-built for financial text. Keep GPT-4o-mini for complex synthesis | 2 days |
| P3-06 | Velocity Scorer | For each ticker: compute `velocity = current_mentions / avg_7day_mentions`. Score: velocity > 3x AND sentiment > 0.3 = strong bullish signal | 1 day |
| P3-07 | Context Classifier | Use LLM to classify mention context: "product quality", "sold out everywhere", "earnings speculation", "meme momentum", "negative recall". Different contexts have different alpha decay rates | 2 days |
| P3-08 | Signal Deduplication | Same story across Reddit + News + Google Trends should count as 1 signal with higher conviction, not 3 separate signals | 1 day |
| P3-09 | Camillo Signal Scorer | Composite score: `alpha_signal = f(velocity_z_score, sentiment, context_quality, cross_source_confirmation)`. Output: ticker, direction, conviction (0-1), estimated alpha decay (days) | 2 days |

#### 3C. Technical Analysis Agent

| ID | Task | Description | Effort |
|---|---|---|---|
| P3-10 | TA-Lib Integration | Install TA-Lib. Compute RSI, MACD, Bollinger Bands, ATR for watchlist underlyings | 1 day |
| P3-11 | Chart Pattern Detection | Simple pattern recognition: higher highs/lows (trend), support/resistance levels, volume confirmation | 2 days |
| P3-12 | Technical Signal Scorer | Score each underlying: `tech_score = f(trend_strength, momentum, volume_confirmation, support_proximity)`. Range: -1 (bearish) to +1 (bullish) | 1 day |

### Testing at End of Phase 3

- [ ] `pytest tests/test_social_scraper.py` — Reddit scraper returns mention counts for known tickers; Google Trends returns interest data
- [ ] `pytest tests/test_sentiment_engine.py` — FinBERT scores known positive/negative financial headlines correctly (>80% accuracy on test set)
- [ ] Velocity scorer correctly identifies historical spikes (backtest: check if GME Jan 2021, NVDA pre-earnings 2024 would have triggered)
- [ ] Signal deduplication works: same AAPL story from Reddit + NewsAPI produces 1 signal, not 2
- [ ] Camillo signal scorer generates scores for at least 5 tickers from live data
- [ ] TA-Lib indicators produce values matching TradingView for the same ticker/timeframe
- [ ] Dashboard shows new "Social Intelligence" panel: mention velocity heatmap, signal list sorted by conviction
- [ ] End-to-end: social signal with conviction > 0.7 triggers a notification on Telegram
- [ ] `NewsSentry` upgraded: fetches from at least 3 providers without errors over a 24-hour test run

---

## Phase 4: Multi-Agent Synthesis & Decision Layer (3-4 weeks)

**Goal**: Wire all specialist agents into a unified decision-making framework. This is where the bull/bear debate happens and the final trade thesis converges. This transforms separate signals into actionable options trades.

### Architecture

```
┌──────────────────────────────────────────────────────┐
│                  SYNTHESIS LAYER                      │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐          │
│  │ Vol       │  │ Social   │  │Technical │          │
│  │ Surface   │  │ Sentiment│  │ Agent    │          │
│  │ Agent     │  │ Agent    │  │          │          │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘          │
│       │              │              │                │
│       └──────┬───────┴──────┬───────┘                │
│              ▼              ▼                         │
│   ┌────────────────────────────────────┐             │
│   │    BULL / BEAR DEBATE AGENT        │             │
│   │  (Weighs all signals, finds        │             │
│   │   consensus or flags conflict)     │             │
│   └──────────────┬─────────────────────┘             │
│                  ▼                                    │
│   ┌────────────────────────────────────┐             │
│   │    RISK GATE                       │             │
│   │  Regime check, VaR budget,         │             │
│   │  correlation guard, margin check   │             │
│   └──────────────┬─────────────────────┘             │
│                  ▼                                    │
│   ┌────────────────────────────────────┐             │
│   │    OPTIONS STRATEGY SELECTOR       │             │
│   │  Pick strategy type + optimize     │             │
│   │  legs from vol surface             │             │
│   └──────────────┬─────────────────────┘             │
│                  ▼                                    │
│   ┌────────────────────────────────────┐             │
│   │    CAPITAL ALLOCATOR               │             │
│   │  Position size via Kelly/Risk      │             │
│   │  Parity connected to live P&L      │             │
│   └──────────────┬─────────────────────┘             │
│                  ▼                                    │
│              STAGED ORDER                            │
└──────────────────────────────────────────────────────┘
```

### Tasks

| ID | Task | Description | Effort |
|---|---|---|---|
| P4-01 | Signal Bus Schema | Standardize signal format: `{source, ticker, direction, conviction, edge_type, timestamp, metadata}` — all agents publish to the same schema | 1 day |
| P4-02 | Signal Aggregator | `agents/signal_aggregator.py`: Collect latest signals from all agents per ticker. Compute composite conviction using configurable weights | 2 days |
| P4-03 | Bull/Bear Debate Agent | LLM-powered synthesis: given all signals for a ticker, generate a bull thesis and bear thesis, then declare a verdict with confidence. Store the debate transcript for journaling | 3 days |
| P4-04 | Risk Gate | Pre-trade risk check: verify regime allows the strategy, VaR budget available (Phase 1 VaR), margin headroom, no correlation concentration (>3 correlated positions). Veto if any check fails | 2 days |
| P4-05 | Capital Allocator Upgrade | Wire Kelly criterion to live strategy win rate (from Phase 2 strategy P&L). Use half-Kelly with per-regime max-position caps. Factor in correlation penalty | 2 days |
| P4-06 | Decision Pipeline Orchestrator | `agents/decision_pipeline.py`: Orchestrate signal collection → debate → risk gate → strategy selection → sizing → staging. Runs on configurable schedule (e.g., 9:35 AM, 12:00 PM, 3:30 PM ET) | 2 days |
| P4-07 | Decision Audit Trail | Log every decision step (signals, debate, risk check result, trade or veto reason) to `decision_log` table. Essential for debugging and improving the system | 1 day |
| P4-08 | Human-in-the-Loop Gate | Configurable: `AUTO_EXECUTE=false` → stage orders for human review (current behavior). `AUTO_EXECUTE=true` → transmit automatically (future, earned after paper trading validation) | 1 day |

### Testing at End of Phase 4

- [ ] `pytest tests/test_signal_aggregator.py` — composite signal correctly weighs multiple sources; conviction stays in [0, 1]
- [ ] `pytest tests/test_debate_agent.py` — given mock signals (3 bullish, 1 bearish), agent produces a bull verdict; given mixed conflicting signals, agent flags "inconclusive"
- [ ] `pytest tests/test_risk_gate.py` — risk gate vetoes trade when: (a) regime disallows strategy, (b) VaR budget exceeded, (c) margin < 20% headroom, (d) >3 correlated positions
- [ ] `pytest tests/test_capital_allocator.py` — Kelly sizing outputs smaller positions for lower win-rate strategies; position size decreases when regime is high_vol or crisis_mode
- [ ] Full pipeline integration test: inject synthetic signals → pipeline produces a staged order with correct strategy, sizing, and audit trail
- [ ] Decision audit trail: `decision_log` table contains every stage of the pipeline for the test run
- [ ] Dashboard shows "Decision Pipeline" panel: last run timestamp, outcome (trade/veto/inconclusive), and link to full audit trail
- [ ] No auto-execution occurs when `AUTO_EXECUTE=false` — all trades staged with `transmit=False`

---

## Phase 5: Automated Execution & Position Lifecycle (3-4 weeks)

**Goal**: Upgrade execution beyond simple market/limit orders. Build smart execution for options (where bid/ask spreads eat alpha) and automated position management rules.

### Tasks

| ID | Task | Description | Effort |
|---|---|---|---|
| P5-01 | Smart Limit Order Engine | For options: start at mid-price, walk toward natural side in 0.05 increments every 30s. Cancel after 5 min if not filled. Critical for options where spreads are wide | 2 days |
| P5-02 | Multi-Leg Execution Optimizer | For spreads: try native combo order first (better fill). If no fill in 2 min, leg into the position starting with the harder-to-fill side | 2 days |
| P5-03 | Fill Monitor & Partial Fill Handler | Track partial fills. If one leg fills and the other doesn't within 60s, auto-hedge with the unfilled leg at market (prevents naked exposure) | 2 days |
| P5-04 | Position Aging Tracker | Track: days-in-trade, % of max profit reached, % of max loss reached, current P&L vs theta expected | 1 day |
| P5-05 | Exit Rules Engine | Configurable YAML rules: `close_at_profit_pct: 50`, `close_at_dte: 21`, `close_at_loss_multiplier: 2.0`, `roll_at_dte: 7`. Different rules per strategy type | 2 days |
| P5-06 | Roll Manager | Auto-detect positions approaching exit rules. Generate roll candidates: same strategy, next expiration cycle, re-optimized strikes from vol surface | 2 days |
| P5-07 | Position Health Dashboard | Traffic-light indicators: green (profitable, no exit signals), yellow (approaching exit rule), red (at or beyond max loss / DTE threshold) | 1 day |
| P5-08 | Exit/Roll Notification | Telegram alerts when position hits an exit or roll trigger, with suggested action and one-click approval | 1 day |
| P5-09 | TWAP/VWAP for Size | For larger positions: split execution over 15-30 min window to reduce market impact (complete the existing stubs in `execution_agent.py`) | 2 days |

### Testing at End of Phase 5

- [ ] `pytest tests/test_smart_execution.py` — mid-price walking logic: price starts at mid, increments correctly, times out after 5 min
- [ ] `pytest tests/test_position_lifecycle.py` — position aging correctly computes days-in-trade, % max profit; exit rules trigger at correct thresholds
- [ ] `pytest tests/test_roll_manager.py` — roll candidates use next monthly expiration, same strategy type, optimized strikes
- [ ] Paper trading validation: execute 5 spread orders using smart limit engine. Measure fill rate and slippage vs mid-price
- [ ] Exit rules fire correctly: create a position at 50% max profit → system triggers close signal
- [ ] Dashboard "Position Health" widget shows green/yellow/red correctly based on test positions
- [ ] Telegram notification arrives within 30s of exit trigger, includes suggested action text
- [ ] Partial fill handler: simulate partial fill on leg 1, verify leg 2 hedges within 60s

---

## Phase 6: Backtesting & Paper Trading Validation (3-4 weeks)

**Goal**: Before risking real capital, validate every signal and strategy against historical data and IBKR paper trading. This phase is non-negotiable — no live trading without evidence.

### Tasks

| ID | Task | Description | Effort |
|---|---|---|---|
| P6-01 | Historical Data Pipeline | Fetch and store: daily OHLCV, VIX, options IV (via Polygon.io or Barchart API), and historical Reddit mention data (where available). Store in PostgreSQL with proper indexing | 3 days |
| P6-02 | Backtesting Framework | `analytics/backtester.py`: Event-driven backtester that replays historical data through the signal pipeline. Supports options positions with proper Greeks evolution. Use vectorbt for the numerical core | 3 days |
| P6-03 | Signal Backtest Runner | For each signal type (vol surface, social velocity, technical), run 2-year backtests. Report: hit rate, average return, Sharpe, max drawdown, time-in-trade distribution | 2 days |
| P6-04 | Strategy Backtest | Backtest full strategies (e.g., "sell iron condors when IV rank > 60 in neutral regime") with realistic fill assumptions (mid + 0.05 slippage per leg) | 2 days |
| P6-05 | Stress Testing Module | `analytics/stress_test.py`: Historical scenarios (March 2020, Feb 2018 Volmageddon, Oct 2023 treasury spike) + Monte Carlo with regime-aware volatility | 2 days |
| P6-06 | Correlation Stress Test | Test portfolio under elevated cross-asset correlation (ρ → 0.8) simulating crisis conditions | 1 day |
| P6-07 | Paper Trading Journal | Run full system against IBKR paper account for 30 days. Log every decision, trade, and outcome. Track Sharpe, win rate, max drawdown, and theta efficiency daily | Ongoing |
| P6-08 | Signal Quality Dashboard | New "Backtesting" tab: signal hit rates over time, strategy equity curves, stress test P&L impact matrix | 2 days |
| P6-09 | Walk-Forward Validation | Implement walk-forward optimization: train on 12 months, test on 3 months, roll forward. Prevents overfitting | 2 days |

### Testing at End of Phase 6

- [ ] Backtester reproduces known historical trade outcomes (±5% tolerance for P&L on 10 reference trades)
- [ ] Signal backtest results for at least 3 signal types show Sharpe > 0.5 (moderate edge) over 2-year period
- [ ] Stress test: March 2020 scenario produces losses < 15% of NLV for the proposed portfolio (if not, tighten risk limits)
- [ ] Stress test: Feb 2018 Volmageddon scenario — system detects regime shift and reduces exposure within 1 hour
- [ ] Paper trading: 30-day track record with > 10 completed trades. Document Sharpe, win rate, max drawdown
- [ ] Walk-forward: out-of-sample Sharpe is within 50% of in-sample Sharpe (no severe overfitting)
- [ ] Correlation stress: portfolio drawdown under ρ=0.8 is < 2x the normal-correlation drawdown
- [ ] Backtest results dashboard renders correctly with interactive charts

---

## Phase 7: Production Hardening & Live Trading (2-3 weeks)

**Goal**: Harden the system for live deployment. Add circuit breakers, monitoring, containerization, and graduate from paper to live with tiny positions.

### Tasks

| ID | Task | Description | Effort |
|---|---|---|---|
| P7-01 | Docker Compose | Single-command deployment: PostgreSQL + IBKR Client Portal + Streamlit + Workers + Agents. Use secrets management, not .env files | 2 days |
| P7-02 | Health Check Server | HTTP endpoint at `/health` reporting status of all services: IBKR connection, DB, streaming, each agent. Alert if any component is down | 1 day |
| P7-03 | Global Circuit Breaker | If daily loss > X% of NLV, halt all new trades, flatten delta to neutral, and send Telegram/email alert. Configurable per regime | 1 day |
| P7-04 | Structured JSON Logging | Replace print/f-string logging with structured JSON. Feed to centralized viewer (Loki/Grafana or simple log aggregation) | 1 day |
| P7-05 | GitHub Actions CI/CD | Run `pytest`, `ruff`, `mypy` on every push. Deploy to server on merge to `main` | 4 hours |
| P7-06 | Daily Summary Report | Automated EOD report: P&L, new positions, closed positions, Greeks changes, regime status, active signals, VaR utilization. Sent to Telegram | 1 day |
| P7-07 | Weekly Performance Digest | Sharpe, Sortino, strategy attribution, drawdown analysis, signal hit rate. Formatted Telegram message + optional email PDF | 1 day |
| P7-08 | Live Trading Graduation | Switch from paper to live with minimum position sizes. First month: 1 contract per trade. Ramp up only if Sharpe > 1.0 and max drawdown < 5% | Ongoing |
| P7-09 | Monitoring Alerts | Alert on: agent crash, streaming disconnect > 5 min, regime change, VaR limit approach (>80%), position exit trigger, unusual P&L move | 1 day |

### Testing at End of Phase 7

- [ ] `docker-compose up` starts all services; `curl localhost:PORT/health` returns all-green status
- [ ] CI pipeline: push a failing test → pipeline fails. Push a fix → pipeline succeeds and deploys
- [ ] Global circuit breaker: simulate 3% daily loss → system halts trades and sends alert within 2 min
- [ ] Daily summary report: check Telegram for EOD summary at 4:30 PM ET for 5 consecutive trading days
- [ ] Weekly digest: check Telegram on Saturday morning for weekly performance summary
- [ ] Live deployment: first 5 trades execute correctly with minimum position sizes (1 contract)
- [ ] Health check detects simulated IBKR disconnect; alert fires within 60 seconds
- [ ] All logs are structured JSON and parseable by jq

---

## Architecture Target State

```
┌─────────────────────────────────────────────────────────────┐
│                     DATA INGESTION LAYER                     │
│  ┌──────┐  ┌──────────┐  ┌────────┐  ┌──────┐  ┌────────┐ │
│  │ IBKR │  │Tastytrade│  │ Reddit │  │ News │  │ Google │ │
│  │  WS  │  │ DxLink   │  │ (PRAW) │  │  API │  │ Trends │ │
│  └──┬───┘  └────┬─────┘  └───┬────┘  └──┬───┘  └───┬────┘ │
│     │           │             │          │          │       │
│     └───────────┴──────┬──────┴──────────┴──────────┘       │
│                        ▼                                     │
│              ┌─────────────────┐                             │
│              │   PostgreSQL    │  (positions, Greeks, P&L,   │
│              │  + TimescaleDB  │   social data, signals,     │
│              │  + pgvector     │   decision logs, journal)   │
│              └────────┬────────┘                             │
└───────────────────────┼─────────────────────────────────────┘
                        │
┌───────────────────────┼─────────────────────────────────────┐
│                ANALYST AGENTS (parallel)                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │ Vol      │  │ Social   │  │Technical │  │ Options  │    │
│  │ Surface  │  │ Sentiment│  │ Analysis │  │  Flow    │    │
│  │ Analyst  │  │ (Camillo)│  │ Agent    │  │  Intel   │    │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘    │
│       │              │              │              │          │
│       └──────┬───────┴──────┬───────┴──────────────┘          │
│              ▼              ▼                                  │
│   ┌────────────────────────────────────┐                     │
│   │    SIGNAL AGGREGATOR               │                     │
│   │    (Standardized signal bus)       │                     │
│   └──────────────┬─────────────────────┘                     │
│                  ▼                                            │
│   ┌────────────────────────────────────┐                     │
│   │    BULL / BEAR DEBATE AGENT        │                     │
│   │    (LLM-powered thesis synthesis)  │                     │
│   └──────────────┬─────────────────────┘                     │
└──────────────────┼──────────────────────────────────────────┘
                   │
┌──────────────────┼──────────────────────────────────────────┐
│           RISK & EXECUTION LAYER                             │
│                  ▼                                            │
│   ┌────────────────────────────────────┐                     │
│   │    RISK GATE                       │                     │
│   │  VaR budget, regime check,         │                     │
│   │  correlation guard, margin guard   │                     │
│   └──────────────┬─────────────────────┘                     │
│                  ▼                                            │
│   ┌────────────────────────────────────┐                     │
│   │    OPTIONS STRATEGY SELECTOR       │                     │
│   │    + LEG OPTIMIZER                 │                     │
│   └──────────────┬─────────────────────┘                     │
│                  ▼                                            │
│   ┌────────────────────────────────────┐                     │
│   │    CAPITAL ALLOCATOR               │                     │
│   │    (Kelly + risk parity + per-     │                     │
│   │     regime limits)                 │                     │
│   └──────────────┬─────────────────────┘                     │
│                  ▼                                            │
│   ┌────────────────────────────────────┐                     │
│   │    SMART EXECUTION ENGINE          │                     │
│   │    Mid-price walking, partial      │                     │
│   │    fill protection, combo orders   │                     │
│   └──────────────┬─────────────────────┘                     │
│                  ▼                                            │
│   ┌────────────────────────────────────┐                     │
│   │    POSITION LIFECYCLE MANAGER      │                     │
│   │    Exit rules, roll manager,       │                     │
│   │    aging tracker, health monitor   │                     │
│   └──────────────┬─────────────────────┘                     │
└──────────────────┼──────────────────────────────────────────┘
                   │
┌──────────────────┼──────────────────────────────────────────┐
│           MONITORING & REPORTING                             │
│                  ▼                                            │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│   │  Streamlit   │  │  Telegram    │  │   Health     │      │
│   │  Dashboard   │  │  Bot         │  │   Monitor    │      │
│   │  (Multi-Tab) │  │  + Reports   │  │   + Alerts   │      │
│   └──────────────┘  └──────────────┘  └──────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

---

## Risk Controls Throughout

These safeguards must be present at every phase. Never proceed without them:

### Hard Limits (Non-Overridable)

1. **Max daily loss**: 3% of NLV → halt all trading, flatten delta
2. **Max single position**: 5% of NLV (2% in crisis)
3. **Max correlated exposure**: 15% of NLV in any single sector
4. **Min margin headroom**: 20% free margin at all times
5. **Order size sanity**: No order > 10 contracts without explicit human approval
6. **No naked short options**: Every short option must be defined-risk (spread)

### Soft Limits (Configurable per Regime)

1. **VaR budget**: 1-day 95% VaR < 2% NLV (neutral), < 1% NLV (high vol), < 0.5% NLV (crisis)
2. **Greeks limits**: Per regime as defined in `risk_matrix.yaml`
3. **Strategy concentration**: No single strategy > 30% of portfolio theta
4. **Correlation threshold**: New position rejected if portfolio correlation increases > 0.05

### Kill Switches

1. **Global halt**: Telegram command `/halt` → immediately cancel all open orders, refuse all new trades
2. **Agent halt**: Telegram command `/halt_agent {name}` → disable specific agent
3. **Manual override**: All automated trades can be approved/rejected via dashboard or Telegram

---

## Timeline Summary

| Phase | Duration | Cumulative | Key Deliverable |
|---|---|---|---|
| Phase 0: Stabilization | 1-2 weeks | Week 2 | Zero critical bugs, unified regime detection |
| Phase 1: Analytics | 3-4 weeks | Week 6 | P&L series, VaR, Sharpe, Greeks attribution |
| Phase 2: Options Alpha Engine | 4-6 weeks | Week 12 | Vol surface, flow intel, strategy-aware proposer |
| Phase 3: Social Arbitrage | 4-5 weeks | Week 17 | Reddit/Trends scraper, FinBERT, Camillo signals |
| Phase 4: Synthesis Layer | 3-4 weeks | Week 21 | Bull/bear debate, risk gate, decision pipeline |
| Phase 5: Execution & Lifecycle | 3-4 weeks | Week 25 | Smart fills, exit rules, position health |
| Phase 6: Backtesting | 3-4 weeks | Week 29 | Historical validation, paper trading, stress tests |
| Phase 7: Production | 2-3 weeks | Week 32 | Docker, CI/CD, live trading graduation |

**Total estimated timeline: ~8 months to full production deployment.**

Note: Phases 2 and 3 can run partially in parallel (social data pipeline is independent of vol surface work). Phases are designed so each phase delivers standalone value — you don't need to complete all 7 phases to start getting benefits. After Phase 1, you already have institutional-grade analytics. After Phase 2, you have a genuine options alpha edge.

---

## Key Papers & Resources to Study During Implementation

| Phase | Resource | Why |
|---|---|---|
| Phase 1 | `risk-metrics-calculation` skill (already installed) | VaR/CVaR formulas and patterns |
| Phase 2 | Natenberg "Option Volatility & Pricing" — Ch. 18-22 | Vol surface modeling |
| Phase 2 | vollib.org — Python options pricing | Black-Scholes, implied vol |
| Phase 3 | FinBERT (huggingface.co/ProsusAI/finbert) | Sentiment model |
| Phase 3 | TradingAgents (arXiv 2412.20138) | Multi-agent architecture |
| Phase 4 | FinMem (arXiv 2408.01234) | Layered memory for trading agents |
| Phase 6 | vectorbt.dev | Backtesting framework |
| Phase 6 | QuantLib (quantlib.org) | Options pricing for backtests |
| All | Sebastian "Trading Options Greeks" | |Θ|/|V| ratio, Greeks management |
| All | Passarelli "Trading Options Greeks" | Synthetic adjustments |
| All | Taleb "Dynamic Hedging" | Gamma risk, tail risk management |
