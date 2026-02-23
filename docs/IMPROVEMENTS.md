# Portfolio Guardrails — Audit & Improvements

**Audit date:** 2026-02-17  
**Test status:** 33/33 passed  
**End goal:** A risk-first system that prevents catastrophic losses by enforcing Greek limits, concentration caps, and margin guardrails before and during trades.

---

## Bugs Fixed in This Audit

| # | File | Bug | Fix |
|---|------|-----|-----|
| 1 | `core/processor.py` | `deque(maxlen=10_000)` auto-evicts from the deque but not from the companion `_dedupe_set`, leaving orphaned entries. One leaked entry per 10,001 records — unbounded long-term growth. | Changed to `deque()` (no maxlen) + `_max_dedupe_size = 10_000` constant. Manual `popleft()` in the while loop now correctly removes from both structures simultaneously. |
| 2 | `adapters/ibkr_adapter.py` | `to_stream_snapshot_payload` used `datetime.utcnow()` (deprecated in Python 3.12+; returns tz-naive datetime). | Replaced with `datetime.now(timezone.utc)`. |
| 3 | `database/db_manager.py` | `snapshot_from_mapping` passed the full DB row (including `persisted_at`) to `GreekSnapshotRecord(**payload)`. Since `GreekSnapshotRecord` uses `slots=True`, any unknown field raises `TypeError`. | Filter to known fields only before unpacking. |

---

## Confirmed Non-Bug: IBKR Stream Coverage = 0

- **Root cause:** Every `--ibkr-stream-benchmark` test was run on **Feb 17, 2026 (Presidents' Day — US markets closed)**. IBKR's CPAPI streams real-time quotes only during market hours.
- **Evidence:** Websocket connects fine; account selection confirmed (`selectedAccount: U2052408` in `act` payload); control-plane messages arrive normally; zero `smd+{conid}` data = expected for a closed market.
- **Action needed:** Re-run the benchmark on a regular trading day (9:30 AM–4:00 PM ET) to confirm E2E Greek streaming works.

---

## Prioritized Improvements (Guardrails Focus)

### P0 — Critical (Direct trading risk)

#### 1. Pre-Trade Simulation Guardrail
**Current state:** `check_risk_limits` runs *after* a position is recorded.  
**Gap:** No function rejects a trade *before* it is submitted if it would breach a regime limit.  
**Proposed:**
```python
def simulate_trade_impact(
    positions: list[UnifiedPosition],
    proposed: UnifiedPosition,
    regime: MarketRegime,
) -> dict[str, Any]:
    """Return {'ok': bool, 'violations': list, 'projected': dict}."""
    combined = positions + [proposed]
    summary = get_portfolio_summary(combined)
    violations = check_risk_limits(summary, regime)
    return {"ok": len(violations) == 0, "violations": violations, "projected": summary}
```
Wire this into `handle_trades_create` as a blocking check (`422 Unprocessable` with violation details if it fails).

#### 2. Margin / Buying-Power Guardrail
**Current state:** No integration with IBKR's `GET /v1/api/portfolio/{account}/summary`.  
**Gap:** The system can approve a trade that pushes margin utilization to 100%, triggering a forced liquidation.  
**Proposed:** Before any trade or at portfolio refresh, call the IBKR account summary endpoint to pull `netliquidation`, `excessliquidity`, `cushion` fields. Define `MIN_CUSHION = 0.20` (20% excess liquidity buffer). Reject trades that would breach it.

#### 3. Max DTE Expiry Alarm (Gamma Spike Protection)
**Current state:** `get_gamma_risk_by_dte` groups gamma by bucket but there is no automatic alert when a short option is ≤ 2 DTE.  
**Gap:** Short gamma risk grows exponentially inside 2 DTE — this is the most common source of unexpected loss.  
**Proposed:** Add `_check_dte_expiry_risk(positions)` that returns `WARNING` for any short option with DTE ≤ 5 and `CRITICAL` for DTE ≤ 2. Surface on the dashboard and write to the log as `LOGGER.critical(...)`.

---

### P1 — High (Risk enforcement gaps)

#### 4. Real-Time Alert Delivery
**Current state:** Violations are detected and returned as a Python list; nothing is sent anywhere.  
**Gap:** If the system finds a breach at 2 AM, no human is notified.  
**Proposed:** Add an `AlertDispatcher` class with a pluggable backend:
- `SlackDispatcher` — POST to a webhook URL (`SLACK_WEBHOOK_URL` env var)
- `EmailDispatcher` — send via SMTP
- `LogDispatcher` (default) — structured `LOGGER.critical` with payload

Call after every `check_risk_limits` that returns violations.

#### 5. Per-Underlying Concentration Limit
**Current state:** Only portfolio-total Greeks are bounded.  
**Gap:** 80% of vega in a single underlying (e.g., /ES) is not flagged.  
**Proposed:** Add `max_single_underlying_vega_pct: float = 0.60` to `RegimeLimits`. Add `check_concentration_risk(positions, regime)` that computes percentage of total vega per underlying and flags breaches.

#### 6. Max Single-Position Size
**Current state:** No individual contract size check.  
**Gap:** A 100-lot short strangle can be entered without any automated guard.  
**Proposed:** Add `max_position_contracts: int = 50` to `RegimeLimits` (configurable per regime). `check_risk_limits` checks every position's `abs(quantity)`.

#### 7. Daily P&L Drawdown Guardrail
**Current state:** Unrealized P&L is tracked per position but no daily max-loss rule exists.  
**Gap:** A 3% drawdown day should trigger a pause, not auto-continue.  
**Proposed:** Record start-of-day `netliquidation` from IBKR. Define `MAX_DAILY_LOSS_PCT = 0.03`. If `(start_value - current_value) / start_value > MAX_DAILY_LOSS_PCT`, mark all new trades as blocked and alert.

---

### P2 — Medium (Quality and correctness)

#### 8. Regime Change Auto-Action
**Current state:** Regime is detected on demand; no callback runs when it changes.  
**Gap:** If VIX spikes from 18 → 40 between refreshes, the system takes no action.  
**Proposed:** Store `_last_regime` in `RegimeDetector`. On `detect_regime()`, if the regime has changed, emit a `RegimeChangeEvent` with old/new regime names, which triggers an alert and optionally reduces position targets.

#### 9. IBKR Futures-Option Multiplier Coverage
**Current state:** `_extract_contract_multiplier` only hardcodes ES=50, MES=5. All other futures (NQ, CL, GC, ZB, RTY…) fall through to the equity-option default of 100, which is wrong.  
**Proposed:** Expand the multiplier table for all standard CME/CBOT futures. Store as a `dict` constant.
```python
_FUTURES_MULTIPLIERS = {
    "ES": 50, "MES": 5, "NQ": 20, "MNQ": 2,
    "YM": 5, "MYM": 0.5, "RTY": 50, "M2K": 5,
    "CL": 1000, "NG": 10_000, "GC": 100, "SI": 5000,
    "ZB": 1000, "ZN": 1000,
}
```

#### 10. Deduplicated `get_positions` Call in the IBKR Benchmark
**Current state:** `run_ibkr_stream_benchmark` in `debug_greeks_cli.py` calls `adapter.fetch_positions()` (which internally calls `get_positions`) and then calls `adapter.client.get_positions()` again directly — two REST round-trips for the same data.  
**Proposed:** Pass the raw positions through from the first call. Saves ~3s per benchmark run.

#### 11. Tastytrade Session Pooling
**Current state:** Each Greek lookup opens a new Tastytrade websocket connection, subscribes, waits for data, and disconnects. For 20 options, this is 20 sequential HTTP + WS cycles (even with batching, each batch reconnects).  
**Proposed:** Cache a persistent `TastytradeDXLinkSession` that stays connected during the portfolio refresh cycle and batches all subscriptions in a single session.

#### 12. DSN Should Never Be Logged
**Current state:** `DBManager.dsn` is a string property that contains the plaintext DB password. If any code logs `self.dsn` or `self.db_manager.dsn` for debugging, the password is exposed.  
**Proposed:** Add a `_redacted_dsn` property that masks the password. Use that in LOGGER output. Add a linting rule (`detect-secrets` is already in `.pre-commit-config.yaml` — verify it covers this pattern).

---

### P3 — Low (Polish and developer experience)

#### 13. Crisis Mode Violation Message Clarification
**Current state:** `crisis_mode` sets `max_negative_vega: 0`, so *any* short vega is flagged as "Short volatility exposure exceeds limit" — but the intent is to force a full hedge (net-zero or net-long vega).  
**Proposed:** Add a `violation_message` field to `RegimeLimits` or make the message in `check_risk_limits` regime-aware. In crisis mode, the message should read "Portfolio must be vega-neutral or long in crisis mode."

#### 14. IV-vs-HV Signal Granularity
**Current state:** `get_iv_analysis` uses fixed thresholds (0.10, 0.15) for sell signals.  
**Gap:** These are hardcoded and not regime-aware. A 15-point IV premium is a strong sell in VIX 15 but moderate in VIX 35 where realized vol is high.  
**Proposed:** Make thresholds relative to VIX (`iv_premium_threshold = 0.10 + (vix / 100)`).

#### 15. Dashboard Risk Alert Panel
**Current state:** The Streamlit dashboard shows position data, Greeks, and charts.  
**Gap:** There is no dedicated "Risk Status" panel showing current regime, limit compliance, and any breaches in red.  
**Proposed:** Add a `st.container` at the top of `dashboard/app.py` that displays:
  - Current regime name + VIX, one-line
  - Traffic-light for each Greek limit (green/yellow/red)
  - Badge count of active violations

#### 16. IBKR Stream Market-Hours Guard in the Benchmark
**Current state:** The benchmark runs and silently returns `coverage=0` on market holidays.  
**Proposed:** Add a market-hours check at benchmark start:
```python
from datetime import datetime, timezone
import exchange_calendars as xcals
nyse = xcals.get_calendar("XNYS")
if not nyse.is_session(datetime.now(timezone.utc).date()):
    print("Warning: US markets are closed today. IBKR stream will return no data.")
```

#### 17. Regime Config Schema Validation
**Current state:** `risk_matrix.yaml` is loaded without schema validation. A typo (e.g., `max_beta_deta: 300`) silently defaults to 0.0.  
**Proposed:** Add a `pydantic` or `cerberus` schema check on load, raising a clear `ValueError` for missing or mistyped fields.

#### 18. Flash Risk Snapshot Before Dashboard Load
**Current state:** Dashboard fetches live positions on every refresh, which takes 3–5 seconds.  
**Proposed:** Write a periodic background job (every 60s) that writes a `risk_snapshot.json` to disk. The dashboard loads from this file instantly and shows a "last updated X seconds ago" badge.

---

## Architecture Summary

```
[Market Data]  [IBKR Positions]  [Tastytrade Greeks]
      ↓               ↓                  ↓
   RegimeDetector  ←→  IBKRAdapter  ←→  TaskytradeAdapter
                          ↓
                   UnifiedPosition[]
                          ↓
              ─────────────────────────────────
              │    PortfolioTools (Brain)      │
              │  get_portfolio_summary()       │
              │  check_risk_limits()           │
              │  simulate_trade_impact() [P0]  │  ← MISSING
              │  check_concentration() [P1]    │  ← MISSING
              │  check_dte_expiry() [P0]       │  ← MISSING
              ─────────────────────────────────
                          ↓
              ─────────────────────────────────
              │    AlertDispatcher [P1]        │  ← MISSING
              │  Slack / Email / Log           │
              ─────────────────────────────────
                          ↓
              ─────────────────────────────────
              │    DataProcessor               │
              │  Stream ingestion (IBKR/Tasty) │
              │  Dedupe + persist to DB        │
              ─────────────────────────────────
                          ↓
              ─────────────────────────────────
              │    PostgreSQL greek_snapshots  │
              │  Partitioned, indexed          │
              ─────────────────────────────────
```

The three most impactful missing pieces for "guardrails" are:
1. **`simulate_trade_impact`** — block trades that would breach limits
2. **`AlertDispatcher`** — notify humans when limits are already breached
3. **Margin cushion check** — prevent margin calls entirely



---

## US3/US4/US5/US6/US7 -- Algo Execution Platform (Feb 2026)

### Architecture Decisions

**Multi-leg BAG combo routing (T032)**
- IBKR requires all legs of a spread/strangle submitted as a single BAG order with secType BAG, conidex, and comboLegs
- Single-leg or no-conid orders use the per-leg orders list
- Partial fills (PartiallyFilled) are a terminal state; order gets PARTIAL status and is journaled

**Trade Journal (T036-T046)**
- SQLite via aiosqlite in database/local_store.py
- record_fill() is fire-and-forget via ThreadPoolExecutor(max_workers=1) + asyncio.run() -- never blocks submit() return path
- VIX and SPX price fetched at fill time via MarketDataTools() with safe fallback (never raises)
- Pre-Greeks captured from PortfolioGreeks passed to submit() from the order builder

**AI Trade Suggestions (T048-T056)**
- LLMRiskAuditor.suggest_trades() -- structured prompt with portfolio Greeks, VIX, regime, breach details; requires 3 JSON suggestions
- All LLM failures return [] -- never raise
- AITradeSuggestion.suggestion_id links to TradeJournalEntry.ai_suggestion_id for full audit trail
- Background daemon thread runs AI analysis without blocking Streamlit

**Historical Charts (T060-T065b)**
- Background snapshot thread (threading, not asyncio -- survives Streamlit reruns) captures state every 15 min
- Sebastian |theta|/|vega| ratio is mandatory: green 0.25-0.40; red <0.20 or >0.50

**Flatten Risk (T067-T073)**
- ExecutionEngine.flatten_risk(positions) -- pure function, no broker calls; returns pre-approved MARKET orders
- Mandatory confirmation dialog before any orders submitted
- All fills journaled with strategy_tag = FLATTEN and standard rationale string

### Known Limitations

1. Post-Greeks: post_greeks_json = "{}" at fill time; background polling after fill is a future enhancement
2. Flatten margin estimate: Uses 5000 x n_orders heuristic; true value requires WhatIf per order
3. Historical data accumulation: Charts only show data captured while the dashboard is running (no backfill)
4. LLM suggestion quality: First-pass prompt; production hardening needs few-shot examples and Pydantic schema validation
