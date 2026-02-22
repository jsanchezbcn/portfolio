# Research: 003 — Algo Execution & Journaling Platform

**Phase 0 output** | Branch: `003-algo-execution-platform` | Date: 2026-02-19

---

## R-001: SPX Beta-Weighted Delta Formula

**Decision**: Use the industry-standard "dollar beta" formula for all instrument classes — identical to IBKR Risk Navigator and Tastytrade's "SPX-Weighted Delta" calculations.

$$\text{SPX-BWD}_{i} = \Delta_{i} \times Q_{i} \times M_{i} \times \beta_{i} \times \frac{P_{\text{underlying}_i}}{P_{\text{SPX}}}$$

| Instrument                    | $\Delta$ input | $Q$        | $M$ (multiplier) | $\beta$           | Price numerator                                                  |
| ----------------------------- | -------------- | ---------- | ---------------- | ----------------- | ---------------------------------------------------------------- |
| Stock / ETF                   | 1.0            | signed qty | 1                | sourced beta      | live underlying price                                            |
| Equity option                 | option delta   | signed qty | 100              | underlying's beta | **live underlying stock price** (NOT strike, NOT option premium) |
| SPX / SPXW / XSP index option | option delta   | signed qty | 100              | 1.0               | index price (ratio cancels to ~1 for SPX)                        |
| /ES futures                   | 1.0            | signed qty | 50               | 1.0               | /ES price                                                        |
| /MES futures                  | 1.0            | signed qty | 5                | 1.0               | /MES price                                                       |

**Rationale**: Both brokers confirmed to use this formula. The price ratio $P_{\text{underlying}} / P_{\text{SPX}}$ is what converts dollar delta into SPX-equivalent delta.

### Example: SPX calls

10 short SPX puts, delta −0.30, SPX = 5900, multiplier = 100, beta = 1.0:
$$\text{SPX-BWD} = −0.30 \times (−10) \times 100 \times 1.0 \times \frac{5900}{5900} = +300$$

### Example: AAPL equity

200 shares AAPL @ $220, beta = 1.14, SPX = 5900:
$$\text{SPX-BWD} = 1.0 \times 200 \times 1 \times 1.14 \times \frac{220}{5900} = +8.50$$

### Bugs in Existing Codebase (to fix in this feature)

| File                       | Line           | Bug                                                       | Fix                                       |
| -------------------------- | -------------- | --------------------------------------------------------- | ----------------------------------------- |
| `adapters/ibkr_adapter.py` | ~L229          | Passes `position.strike` as `price` for options           | Pass live underlying price instead        |
| `beta_config.json`         | `"MES": 0.986` | Statistical regression beta; correct economic value = 1.0 | Change to 1.0                             |
| `adapters/ibkr_adapter.py` | stocks path    | IBKR sometimes returns multiplier=0 for stocks            | Hardcode `multiplier=1.0` for stocks/ETFs |

The core `calculate_spx_weighted_delta` function in `ibkr_portfolio_client.py` is **mathematically correct** — all bugs are in the values passed to it.

---

## R-002: Beta Data Sources

**Decision**: Primary = `tastytrade.metrics.get_market_metrics()`; Fallback chain = `yfinance` → `beta_config.json` static values.

### Primary Source: Tastytrade `get_market_metrics()`

```python
from tastytrade.metrics import get_market_metrics
metrics = await get_market_metrics(session, ["AAPL", "SPY", "QQQ"])
beta_map = {m.symbol: float(m.beta) for m in metrics if m.beta is not None}
```

Returns `MarketMetricInfo.beta` (beta vs SPX), with `beta_updated_at` for freshness checking. Batch endpoint — all symbols in one call. Also returns IV rank, correlation, and other metrics useful for the AI Risk Analyst.

**Session auth note**: Current SDK (v12+) uses `Session(provider_secret=..., refresh_token=...)`. The existing `tastytrade_sdk_options_fetcher.py` uses the old `Session(user, pass)` pattern (no longer supported). The `adapters/tastytrade_adapter.py` wraps an opaque `self.client` — neither file currently fetches beta. The `BetaWeighter` class will need its own Tastytrade session.

### Fallback 1: yfinance (free, no API key)

```python
import yfinance as yf
beta = yf.Ticker(symbol).info.get("beta")  # 5-year monthly vs S&P 500
```

Used only when Tastytrade session unavailable. Different lookback period from Tastytrade (5-year monthly vs Tastytrade's shorter window), but directionally correct for our use case.

### Fallback 2: `beta_config.json` static values

Existing file. Used as final hardcoded fallback for futures and known index instruments (`/ES`, `/MES`, `SPX`, `XSP`) where beta = 1.0 by definition.

**Alternatives considered**: `polygon.io` REST (reliable, but API key required, adds a dependency). Rejected for MVP; can be added as secondary fallback later.

---

## R-003: IBKR WhatIf / Margin Simulation

**Decision**: Use IBKR Client Portal REST `POST /iserver/account/{accountId}/orders/whatif`. In ib_insync mode, `ib.whatIfOrder(contract, order)` is the equivalent.

### REST Endpoint

```
POST https://localhost:5001/iserver/account/{accountId}/orders/whatif
```

### Request Payload (single-leg)

```json
{
  "orders": [
    {
      "acctId": "U1234567",
      "conid": 265598,
      "orderType": "LMT",
      "price": 2.5,
      "side": "BUY",
      "tif": "DAY",
      "quantity": 1
    }
  ]
}
```

### Request Payload (multi-leg combo)

```json
{
  "orders": [
    {
      "acctId": "U1234567",
      "conidex": "28812380;;;756733/1,756734/-1",
      "orderType": "LMT",
      "price": 2.5,
      "side": "BUY",
      "tif": "DAY",
      "quantity": 1
    }
  ]
}
```

Format of `conidex`: `"{spread_conid};;;{leg_conid1}/{ratio},{leg_conid2}/{ratio}"`. Positive ratio = BUY leg, negative = SELL leg. USD spread_conid = `28812380`.

### Response (all numeric values are formatted strings)

```json
{
  "initial": { "current": "$12,345", "change": "$500", "after": "$12,845" },
  "maintenance": { "current": "$...", "change": "$...", "after": "$..." },
  "equity": { "current": "$...", "change": "$...", "after": "$..." },
  "warn": "...",
  "error": null
}
```

Parse with: `float(re.sub(r"[^0-9.\-]", "", s.replace(",", "")))`

### Greeks from WhatIf

**Neither interface returns projected Greeks.** Post-trade Greeks are computed manually:

```python
post_greeks = pre_greeks + sum(
    signed_ratio * qty * multiplier * leg_greek
    for each leg
)
```

Individual leg Greeks fetched via: `GET /iserver/marketdata/snapshot?conids={conid}&fields=7308,7309,7310,7311`
(field IDs: 7308=delta, 7309=gamma, 7310=theta, 7311=vega).

### ib_insync Equivalent

```python
order_state = ib.whatIfOrder(bag_contract, order)
# order_state.initMarginChange, .maintMarginChange, .equityWithLoanChange (strings)
```

**Critical pre-condition**: Must call `/iserver/marketdata/snapshot` for each leg conid before the whatif call, or IBKR returns a "blind trading" warning (simulation still completes, but IBKR flags it).

---

## R-004: Trade Journal SQLite Schema

**Decision**: Single row per trade with `legs_json TEXT` (JSON array) for multi-leg storage. Library: `aiosqlite` (already in use in `database/local_store.py`). New tables added to the existing SQLite database.

### Rationale

- Existing `signals` table in `db_manager.py` already uses `legs_json JSONB` pattern — consistent
- One row = one atomic trade event → trivial CSV export, filtering, and display
- `aiosqlite` already installed and working in `database/local_store.py`
- SQLAlchemy ORM rejected: migration overhead unjustified for two new tables

### Existing DB State

- `database/local_store.py` (SQLite): currently only has `market_intel` table
- `database/db_manager.py` (PostgreSQL): has stub `trade_journal` with only 4 columns — not extended here (SQLite is the local-first store for this feature)

**New SQLite schema** → see `data-model.md` for full CREATE TABLE SQL.

---

## R-005: Screenshots — Versioning Strategy

**Decision**: Split screenshots into high-churn (live data) and low-churn (static UI structure) categories.

| Screenshot                         | Churn | Update trigger                          |
| ---------------------------------- | ----- | --------------------------------------- |
| `broker-ibkr-risk-navigator.png`   | High  | Replace anytime delta comparison needed |
| `broker-tastytrade-beta-delta.png` | High  | Replace anytime delta comparison needed |
| `ui-order-builder.png`             | Low   | Only on OrderBuilder UI redesign        |
| `ui-journal-view.png`              | Low   | Only on journal layout change           |
| `ui-historical-charts.png`         | Low   | Only on chart panel redesign            |
| `ui-ai-suggestions.png`            | Low   | Only on AI suggestion card redesign     |
| `ui-flatten-risk-dialog.png`       | Low   | Only on dialog redesign                 |

High-churn screenshots are committed as-is; no special CI automation needed. Low-churn screenshots are committed once and updated manually.

---

## R-006: AI Risk Analyst Integration

**Decision**: Extend `agents/llm_risk_auditor.py` with a structured JSON output mode triggered by real-time risk breaches. Reuse `agents/llm_client.py` for the LLM call.

**Existing infrastructure**:

- `agents/llm_client.py` — thin wrapper over GitHub Copilot SDK / OpenAI-compatible endpoint
- `agents/llm_risk_auditor.py` — sends portfolio state + Greeks + regime to LLM, returns narrative text audit
- `risk_engine/regime_detector.py` — already detects VIX regime + checks `config/risk_matrix.yaml` limits

**Change required**: Add a `suggest_trades(portfolio_state, breach_info, theta_budget)` method to `llm_risk_auditor.py` that prompts for exactly 3 structured trade suggestions and parses the JSON response. The existing `audit()` method is unchanged.

**Prompt structure (abbreviated)**:

```
You are a risk analyst for an options portfolio. The portfolio has breached the following risk limit: {breach}.
Current state: Delta={delta}, Vega={vega}, Theta={theta}, VIX={vix}, Regime={regime}.
Theta budget per suggestion: {theta_budget}.
Respond with EXACTLY this JSON schema: {"suggestions": [{"legs": [...], "projected_delta_change": float, "projected_theta_cost": float, "rationale": "..."}]} × 3
```

**Alternatives considered**: Separate AI agent class — rejected, adds unnecessary indirection when the existing auditor's context-gathering logic is exactly what's needed.

---

## R-007: Historical Snapshot Logger

**Decision**: Implement as a `asyncio` background task (not a separate daemon process) running a 15-minute interval loop inside the Streamlit app process.

```python
async def _snapshot_loop(db, broker_client):
    while True:
        await asyncio.sleep(900)  # 15 minutes
        snap = await _capture_snapshot(broker_client)
        await db.save_account_snapshot(snap)
```

Started in `dashboard/app.py` via `asyncio.create_task()` (or `threading.Thread` for Streamlit's synchronous model — to be resolved in implementation).

**Rationale**: No separate daemon = no process management complexity. Streamlit's execution model complicates `asyncio`; if needed, a `threading.Timer` approach mirrors the existing pattern from `agents/news_sentry.py` (which uses `asyncio.gather` in a thread).

At 15-min intervals: ~35,040 rows/year — SQLite handles trivially.
