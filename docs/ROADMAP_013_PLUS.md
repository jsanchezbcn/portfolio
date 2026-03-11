# Roadmap: Specs 013 – 021
**Created**: 2026-03-06  
**Depends on**: ARCHITECTURE_REVIEW.md (foundation fixes §1–§3 must land first)  
**Spec pipeline so far**: 007 Config Service · 008 Alembic · 009 Process Supervision · 010 Streaming Greeks · 011 P&L Attribution · 012 Order Lifecycle Notifications

---

## Prerequisite Gate

Before any Spec 013+ work starts, the following from the Architecture Review must be closed:

| Gate | Ref | Why it matters |
|------|-----|----------------|
| Pydantic `Settings` in `config/settings.py` | §2.5 / Spec 007 | All new modules must read from `Settings`, not `os.getenv()` |
| `_resolve_contracts` spam suppressed | `desktop/engine/ib_engine.py` | Emitting 2× per 5-second cycle; add a per-contract dedup cache and `logging_config` rate-limit |
| Single `RegimeDetector` source of truth | §1.2 | Spec 017 (IV Skew agent) publishes REGIME context; needs one canonical regime |
| `BrokerAdapter` ABC expanded | §2.3 | Spec 013 (local Greeks) adds a `compute_estimated_greeks()` abstract method |

---

## Spec 013 — Local Greeks Fallback Engine
**Track**: Technical Reliability  
**Priority**: P1  
**Effort**: ~3 days  
**Replaces**: amber `delta=None` warnings in the portfolio tab

### Problem
`desktop/engine/ib_engine.py` logs `_resolve_contracts: only 0/1 legs resolved` continuously. When IB does not supply Greeks (after-hours, illiquid strikes, unqualified contracts), the UI shows `--` or `None`. The position is still live; the risk is real.

### Solution
Add `core/greeks_engine.py` — a pure-Python BSM calculator that activates when IB returns `None` Greeks.

```
core/
  greeks_engine.py          ← NEW: Black-Scholes + py_vollib wrapper
  greeks_cache.py           ← MOVE from adapters/ibkr_adapter.py (JSON cache logic)
```

**Key design decisions**:
- Use `py_vollib` for BSM (already in `requirements.txt` via `mibian` fallback path in `ibkr_portfolio_client.py`). If not present, add `py_vollib_vectorized` — it handles arrays of strikes in one call.
- Input: `underlying_price` (last known from DB `greek_snapshots`), `strike`, `expiry`, `option_type`, `iv` (last known from DB or from the IB contract's `impliedVol` field before it went `None`).
- Output: Estimated `delta`, `gamma`, `vega`, `theta` tagged with `source="estimated_bsm"`.
- Integration point in `desktop/engine/ib_engine.py`: after each `reqMktData` cycle, for any position where `delta is None`, call `GreeksEngine.estimate(position)` and substitute.
- UI indicator: render estimated Greeks in italic or amber in `desktop/ui/portfolio_tab.py` so the trader knows it is inferred — do not hide the distinction.

**New dependency**: `py_vollib_vectorized>=1.0.3` (add to `requirements.txt`)

**Test coverage**: `tests/test_greeks_engine.py` — parametrize over ATM/ITM/OTM, zero-DTE, and missing-IV edge cases.

---

## Spec 014 — IB Connection Watchdog (Auto-Reconnect)
**Track**: Technical Reliability  
**Priority**: P1  
**Effort**: ~1.5 days  
**Eliminates**: manual reconnect workflow; `IB disconnected event` terminal warning

### Problem
The terminal log shows `IB disconnected event` at 11:31:32 and the app shuts down cleanly (`Application closed`). There is no automatic reconnect. The trader must restart the whole desktop app.

### Solution
Add a `ConnectionWatchdog` state machine to `desktop/engine/ib_engine.py`:

```
States: CONNECTED → DISCONNECTING → RECONNECTING(attempt=n, backoff=2^n) → CONNECTED
                                  → FAILED (after 5 attempts, alert via NotificationDispatcher)
```

**Implementation notes**:
- `ib_async` fires `disconnectedEvent`. Subscribe to it in `IBEngine.__init__`.
- Reconnect logic: `await ib.connectAsync(host, port, clientId)` inside a `try/except` with `asyncio.sleep(backoff)`.
- Backoff: `min(2**attempt, 120)` seconds (caps at 2 min).
- On reconnect, re-subscribe all active tickers (store them in `self._active_subscriptions: set[Contract]`).
- Emit a `desktop/engine/events.py` `ConnectionEvent(status="reconnected"|"failed")` so `main_window.py` can update the status bar.
- Telegram notification via `agent_tools/notification_dispatcher.py` on `FAILED`.

**Test coverage**: `desktop/tests/test_ib_engine.py` — mock `ib.disconnectedEvent`, assert state transitions and backoff timing.

---

## Spec 015 — PostgreSQL Materialized Risk Views
**Track**: Performance  
**Priority**: P2  
**Effort**: ~2 days  
**Depends on**: Spec 008 (Alembic) for DDL management

### Problem
`desktop/engine/ib_engine.py` aggregates `net_delta`, `net_gamma`, `net_vega`, `net_theta` per-ticker in Python every 30 seconds by iterating `UnifiedPosition` objects. As the `greek_snapshots` table grows past 100k rows this becomes the dominant CPU cost in the worker cycle.

### Solution
Create two PostgreSQL Materialized Views in a new Alembic migration:

```sql
-- mv_portfolio_greeks: net Greeks per (account, ticker, expiry_bucket)
CREATE MATERIALIZED VIEW mv_portfolio_greeks AS
SELECT
    account_id,
    ticker,
    date_trunc('week', expiry)   AS expiry_bucket,
    SUM(delta * quantity * 100)  AS net_delta,
    SUM(gamma * quantity * 100)  AS net_gamma,
    SUM(vega  * quantity * 100)  AS net_vega,
    SUM(theta * quantity * 100)  AS net_theta,
    MAX(snapshot_ts)             AS last_updated
FROM greek_snapshots
WHERE snapshot_ts > NOW() - INTERVAL '2 days'
GROUP BY account_id, ticker, date_trunc('week', expiry);

CREATE UNIQUE INDEX ON mv_portfolio_greeks (account_id, ticker, expiry_bucket);

-- Refresh hook: called at end of each ib_engine cycle
-- REFRESH MATERIALIZED VIEW CONCURRENTLY mv_portfolio_greeks;
```

**Integration**:
- `database/db_manager.py`: add `async def refresh_risk_view()` — runs the `REFRESH` statement.
- `desktop/engine/ib_engine.py`: call `refresh_risk_view()` at the end of each 30-second cycle instead of computing in Python.
- `desktop/ui/risk_tab.py`: read from `mv_portfolio_greeks` via a `SELECT` rather than iterating the positions list.
- Fall back to Python aggregation if the materialized view is empty (e.g., fresh DB).

**Test coverage**: `tests/test_db_manager.py` — assert that `mv_portfolio_greeks` is refreshed and returns consistent data after a mock snapshot insert.

---

## Spec 016 — Interactive What-If Scenario Simulator
**Track**: Advanced Risk Analytics  
**Priority**: P1  
**Effort**: ~4 days  
**Depends on**: Spec 013 (local Greeks) for position-level delta/gamma when IB Greeks are absent

### Problem
The `simulate_margin_impact()` in `IBKRAdapter` (§2.3 of Architecture Review) tests IB's What-If endpoint for a single proposed order. There is no way to visually stress-test the *existing* portfolio against a continuous range of underlying moves or VIX shocks before a trade.

### Solution
New tab `desktop/ui/whatif_tab.py` with:

1. **Scenario Controls** (left panel):
   - SPX move slider: −20% to +20% (step 1%). Default: 0%.
   - VIX shock slider: −50% to +150% (step 5%). Default: 0%.
   - Days-forward spinner: 0 to 30 DTE shift.
   - "Add scenario" button to stack multiple scenarios.

2. **P&L Curve** (right panel, `pyqtgraph.PlotWidget`):
   - X-axis: SPX % move. Y-axis: Portfolio P&L ($).
   - Series per account + consolidated.
   - Vertical lines for 1σ and 2σ daily move (derived from current VIX).

3. **Greeks Surface Table** (bottom):
   - Columns: Ticker | Current Δ | Stressed Δ | ΔΔ | Current Γ | Stressed Γ | P&L contribution.
   - Color-coded by contribution magnitude.

**Greeks re-calculation**:
For each position, apply finite-difference re-pricing using `GreeksEngine` from Spec 013:
```python
stressed_price = underlying_price * (1 + spx_pct_move)
stressed_iv    = current_iv * (1 + vix_shock_pct)
stressed_delta = GreeksEngine.estimate(position, price=stressed_price, iv=stressed_iv, dte_shift=days_fwd)
pnl_contrib    = (stressed_delta - current_delta) * underlying_price * 100
```

**Integration**:
- `desktop/ui/main_window.py`: add `WhatIfTab` to `QTabWidget`.
- `desktop/engine/ib_engine.py`: expose `get_snapshot()` method returning a frozen copy of all positions for stress testing without mutating live data.
- Re-use `pyqtgraph` (already in `requirements.txt` for the existing charts widgets).

**Test coverage**: `desktop/tests/test_whatif_tab.py` — mock positions, assert P&L curve data points at ±10% SPX move.

---

## Spec 017 — IV Skew & Tail-Risk Early Warning Agent
**Track**: Advanced Risk Analytics  
**Priority**: P2  
**Effort**: ~2 days  
**Depends on**: Spec 010 (Streaming Greeks pipeline), single `RegimeDetector` (gate above)

### Problem
No current agent monitors the shape of the vol surface. A steepening put skew (OTM put IV rising relative to ATM) while the underlying is flat is often a leading indicator of institutional hedging. The `MarketIntelligenceAgent` publishes `REGIME_CHANGED` but uses only VIX spot level — it is level-based, not skew-based.

### Solution
New agent `agents/skew_monitor.py`:

```python
class SkewMonitorAgent:
    """
    Monitors 25-delta put/call IV skew for SPX / SPY.
    Publishes SKEW_ALERT to NotificationDispatcher when:
      - |put_skew - call_skew| > threshold AND direction is put-biased
      - AND skew has been widening for > N consecutive cycles
    """
```

**Data source**: IB `reqMktData` with generic tick `106` (implied vol) for two FOP/option contracts:
- 25-delta OTM put on `/ES` (nearest monthly expiry)
- 25-delta OTM call on `/ES` (same expiry)

Store readings in a new `iv_skew_history` table (PostgreSQL):
```sql
CREATE TABLE iv_skew_history (
    ts          TIMESTAMPTZ NOT NULL,
    underlying  TEXT NOT NULL,      -- 'ES'
    expiry      DATE NOT NULL,
    put_iv      NUMERIC(8,4),
    call_iv     NUMERIC(8,4),
    skew        NUMERIC(8,4) GENERATED ALWAYS AS (put_iv - call_iv) STORED,
    PRIMARY KEY (ts, underlying, expiry)
);
```

**Alert logic**:
1. Compute 20-period rolling Z-score of `skew`.
2. If Z-score > 2.0 AND `skew > 0` (put-biased): fire `SKEW_ALERT`.
3. Alert channels: `agent_tools/notification_dispatcher.py` → Telegram, and in-app `desktop/ui/market_tab.py` banner.

**Test coverage**: `agents/tests/test_skew_monitor.py` — mock time-series of IV data, assert alert fires at right Z-score thresholds with hysteresis.

---

## Spec 018 — Correlation Heatmap Tab
**Track**: Advanced Risk Analytics  
**Priority**: P3  
**Effort**: ~2 days  
**Depends on**: `trade_journal` + `greek_snapshots` historical data (needs ≥ 20 days of snapshots)

### Problem
The Risk Tab shows per-position Greeks but not inter-position correlation. A portfolio that looks diversified by ticker can have hidden concentration if all names are high-beta tech.

### Solution
New panel `desktop/ui/widgets/correlation_heatmap.py` (added to `risk_tab.py`):

**Data**:
- Pull daily P&L-proxy (theta + delta × Δspx) per position from `greek_snapshots` over a rolling 60-day window.
- Compute Pearson correlation matrix using `numpy`.

**Rendering**:
- Use `pyqtgraph.ImageItem` with a diverging colormap (blue = negative correlation, red = positive).
- Overlay cell labels with the correlation coefficient.
- Threshold marker: cells with `|r| > 0.80` get a bold border — high hidden concentration warning.

**UI placement**: collapsible section inside the existing `risk_tab.py` to avoid a new full tab.

**Test coverage**: `desktop/tests/test_correlation_heatmap.py` — synthetic 5-position matrix, assert heatmap data shape and threshold detection.

---

## Spec 019 — Natural Language Quick-Action Bar
**Track**: AI & Workflow  
**Priority**: P2  
**Effort**: ~3 days  
**Depends on**: `agents/llm_client.py`, `core/order_manager.py` `DRAFT` state, Spec 007 (LLM model in Settings)

### Problem
Navigating tabs to locate and close multiple legs is slow. The `ai_risk_tab.py` already has a Copilot integration for risk audits, but it is read-only and tab-bound.

### Solution
Add a persistent `QLineEdit` command bar at the top of `desktop/ui/main_window.py` (above the tab bar):

```
[ ⌘  Type a command...  e.g. "Close all winners > 50%"          ] [Run]
```

**Architecture**:
1. User input → `agents/llm_client.py` with a new system prompt fragment:
   ```
   You are a trade action parser. From the user's natural-language instruction,
   return a JSON array of StagedAction objects:
   [{"action": "close"|"open", "ticker": str, "legs": [...], "condition": str}]
   If you cannot safely parse an unambiguous action, return {"error": "clarification needed"}.
   ```
2. `StagedAction` objects → `core/order_manager.py` creates `Order` records in `DRAFT` state.
3. The `orders_tab.py` "Staged Legs" table surfaces them immediately with a **Review & Submit** button — no orders fire without explicit confirmation.
4. If the LLM returns `{"error": ...}`, show the clarification request inline below the command bar.

**Safety rails**:
- Maximum 10 legs per NL command.
- NL actions cannot bypass risk-matrix limits (they go through the same `BreachDetector` as manual orders).
- Log all NL commands + LLM responses to `trade_journal.notes` for audit.

**Test coverage**: `tests/test_nlq_parser.py` — mock LLM responses, assert correct `Order` objects created for 5 representative commands.

---

## Spec 020 — Voice-Driven Journal Entry
**Track**: AI & Workflow  
**Priority**: P3  
**Effort**: ~2 days  
**Depends on**: `desktop/ui/journal_tab.py`, system audio access on macOS

### Problem
`desktop/ui/journal_tab.py` exists. Post-trade journaling has low adoption because typing notes after a volatile session is a friction point.

### Solution
Add a **Dictate** button to `journal_tab.py` that:
1. Starts macOS system dictation (`NSDataDetector` or local Whisper).
2. Appends transcribed text to the current journal entry text field.

**Two implementation tiers**:

| Tier | Library | Tradeoffs |
|------|---------|-----------|
| **Local (default)** | `openai-whisper` (runs on CPU, ~200 MB model) | Private, works offline, ~2s transcription latency |
| **Cloud (opt-in)** | `openai.Audio.transcribe()` (Whisper API) | Faster, costs ~$0.006/min, requires network |

Toggle via `Settings.whisper_mode: Literal["local", "cloud", "disabled"] = "disabled"`.

**Integration**:
- `QMediaRecorder` (PySide6 multimedia) → captures microphone to a temp WAV.
- On stop: pass WAV to Whisper, get text, `QTextEdit.insertPlainText(text)`.
- Auto-stamp the entry with `datetime.now()` and the current account/portfolio snapshot reference.

**New dependency**: `openai-whisper>=20231117` (optional, guarded by `try: import whisper`).

**Test coverage**: `desktop/tests/test_journal_tab.py` — mock Whisper output, assert text insertion and timestamp.

---

## Spec 021 — UI Compact Mode & Sound Profiles
**Track**: UI/UX Polish  
**Priority**: P3  
**Effort**: ~1.5 days

### 021-A: Dynamic Column Visibility (Compact Mode)

**Location**: `desktop/ui/portfolio_tab.py`, `desktop/ui/orders_tab.py`

Add a `QAction` toggle ("Compact Mode") to the View menu in `main_window.py`:
- **Full mode**: Current columns including Expiry, Exchange, Multiplier.
- **Compact mode**: Hide `expiry`, `exchange`, `multiplier`, `account_id` — keep `ticker`, `strike`, `type`, `qty`, `delta`, `theta`, `nlv`.

Persist the preference in `desktop/config/` via the existing `Preferences` model (`desktop/config/preferences.py`).

### 021-B: Sound Profiles

Add `desktop/engine/sound_engine.py`:
```python
class SoundEngine:
    PROFILES = {
        "order_filled":     "resources/sounds/cash_register.wav",
        "limit_breached":   "resources/sounds/low_alert.wav",
        "connection_lost":  "resources/sounds/thud.wav",
        "skew_alert":       "resources/sounds/chime.wav",   # Spec 017
    }
    def play(self, event: SoundEvent) -> None: ...
```

Use `QSoundEffect` (PySide6, no extra dependency). Sound files: free CC0 WAVs (< 1s each, committed to `desktop/resources/sounds/`).

Wire to:
- `core/order_manager.py` `FILLED` transition → `order_filled`
- `risk_engine/` limit breach → `limit_breached`
- `desktop/engine/ib_engine.py` disconnect → `connection_lost` (also triggers Spec 014 watchdog)

Toggle per-profile in `Preferences` → Settings dialog.

---

## Implementation Sequence

```
Phase 1 — Reliability (unblocks everything else)
  Spec 013  Local Greeks Fallback           P1  3 days
  Spec 014  IB Connection Watchdog          P1  1.5 days

Phase 2 — Risk Intelligence
  Spec 015  Materialized Risk Views         P2  2 days   (run parallel with 016)
  Spec 016  What-If Simulator               P1  4 days
  Spec 017  IV Skew Early Warning           P2  2 days

Phase 3 — AI & UX
  Spec 018  Correlation Heatmap             P3  2 days
  Spec 019  NL Quick-Action Bar             P2  3 days
  Spec 020  Voice Journal                   P3  2 days
  Spec 021  Compact Mode + Sound Profiles   P3  1.5 days
```

**Total estimated effort**: ~21 developer-days across 9 specs.

---

## New Dependencies Summary

| Package | Spec | Note |
|---------|------|------|
| `py_vollib_vectorized>=1.0.3` | 013 | BSM Greeks calculator |
| `openai-whisper>=20231117` | 020 | Optional, guarded import |

No new UI framework dependencies — `pyqtgraph` and `PySide6-Multimedia` are already pulled in by the existing desktop app.

---

## Risk & Mitigation

| Risk | Affected Specs | Mitigation |
|------|----------------|------------|
| `_resolve_contracts` storm floods logs before 013 lands | 013, 014 | Rate-limit log to 1 warning per contract per 60s immediately (quick win, 30 min) |
| IV data not available for skew agent without streaming subscription | 017 | Gate the agent behind `Settings.skew_monitor_enabled = False`; activate only when streaming Greeks pipeline (Spec 010) is confirmed live |
| NL bar sends malformed JSON from LLM to order_manager | 019 | Wrap LLM response parse in `try/except`; reject and surface error string; never create Order from unparseable response |
| Whisper model download (200 MB) on first run surprises user | 020 | Show a one-time download prompt in Preferences; default to `disabled` until user opts in |
| Materialized view `REFRESH CONCURRENTLY` requires access share lock | 015 | Use `REFRESH MATERIALIZED VIEW CONCURRENTLY` (non-blocking); fall back to direct aggregation query if view age > 5min |
```
