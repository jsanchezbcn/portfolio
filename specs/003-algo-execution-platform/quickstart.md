# Quickstart: 003 — Algo Execution & Journaling Platform

**Phase 1 output** | Branch: `003-algo-execution-platform` | Date: 2026-02-19

---

## Prerequisites

- IBKR Client Portal Gateway running on `https://localhost:5001` and authenticated (manual browser login)
- Tastytrade credentials in `.env`: `TT_SECRET`, `TT_REFRESH`
- Python 3.12 virtualenv active: `source .venv/bin/activate`
- All dependencies installed: `pip install -r requirements.txt`

---

## 1. Run the Dashboard (standard — no new steps)

```bash
streamlit run dashboard/app.py
```

No changes to startup procedure. New panels (Order Builder, Journal, Charts) appear automatically once the new modules are loaded.

---

## 2. Verify Beta-Weighted Delta

Once the dashboard loads:

1. Open the **Portfolio Greeks** panel at the top.
2. Check **SPX Equivalent Delta** — this should now match your broker platform within ±5 delta points.
3. Positions with beta unavailable display a **⚠ Beta N/A** badge; their contribution is computed with β=1.0.
4. To test the beta fix in isolation without the dashboard:

```bash
# Requires IBKR gateway running + Tastytrade credentials in .env
python -c "
from risk_engine.beta_weighter import BetaWeighter
import asyncio

async def test():
    bw = BetaWeighter()
    result = await bw.compute_portfolio_spx_delta()
    print(f'SPX Equivalent Delta: {result.spx_equiv_delta:.1f}')
    print(f'Positions with beta unavailable: {result.beta_unavailable_count}')

asyncio.run(test())
"
```

---

## 3. Use the Order Builder & Simulator

1. Open the **Order Builder** panel in the dashboard sidebar.
2. Select **Instrument type** (Stock / Option / Futures) and fill in the legs.
3. Click **Simulate Trade** — results appear within ~5 seconds:
   - Initial Margin Change
   - Projected Post-Trade Greeks (Delta, Gamma, Theta, Vega)
4. (Optional) Enter a **rationale note** in the text box.
5. Click **Submit Order** → review the confirmation modal → click **Confirm**.

The order is now live. Check your IBKR order blotter to confirm.

---

## 4. View the Trade Journal

1. Open the **Trade Journal** tab.
2. Default view: last 20 trades, newest first.
3. Use filters for date range, underlying, or regime.
4. Click **Export CSV** to download journal entries.

---

## 5. Historical Charts

The 15-minute snapshot logger starts automatically with the dashboard. To see data:

1. Wait at least 15 minutes (or 1 snapshot cycle) after starting the dashboard.
2. Open the **Historical Charts** panel.
3. Select a time range (1D / 1W / 1M / All).
4. The **Account Value vs SPX Delta** dual-axis chart and **Delta/Theta Ratio** chart render automatically.

---

## 6. Flatten Risk (Panic Button)

1. Click the red **⚠ Flatten Risk** button in the dashboard header.
2. A dialog shows all short option legs and their proposed buy-to-close orders.
3. Review the list — long positions and futures are excluded.
4. Click **Confirm Flatten** to submit all orders simultaneously, or **Cancel** to abort.

---

## 7. Environment Variables Reference

Add to `.env` (never commit):

```env
# Tastytrade (required for beta data + options chain)
TT_SECRET=your_provider_secret
TT_REFRESH=your_refresh_token

# IBKR Gateway
IBKR_GATEWAY_URL=https://localhost:5001
IBKR_ACCOUNT_ID=U1234567

# AI Risk Analyst (existing — GitHub Copilot SDK or OpenAI-compatible)
COPILOT_TOKEN=ghp_...          # or OPENAI_API_KEY=sk-...

# Feature flags (optional)
THETA_BUDGET_PER_SUGGESTION=0   # 0 = no limit; set positive to cap AI suggestions
SNAPSHOT_INTERVAL_SECONDS=900   # default 15 minutes
```

---

## 8. Running Tests

```bash
# Unit tests only (no broker connection required)
pytest tests/ -m "not manual and not integration" -v

# Integration tests (requires .env + live IBKR gateway)
pytest tests/ -m "integration" -v

# Specific module
pytest tests/test_beta_weighter.py -v
pytest tests/test_order_builder.py -v
pytest tests/test_trade_journal.py -v
```

---

## 9. File Structure Reference

```
risk_engine/
├── beta_weighter.py         # NEW — BetaWeighter class (FR-001–006)

core/
├── order_manager.py         # EXISTS — extend with simulation + execution (FR-007–013)
├── execution.py             # NEW — IBKR what-if + order submission (FR-008–012)

database/
├── local_store.py           # EXISTS — extend with trade_journal + account_snapshots tables
├── db_manager.py            # EXISTS — unchanged (PostgreSQL, not used by this feature)

models/
├── unified_position.py      # EXISTS — extend with BetaWeightedPosition
├── order.py                 # NEW — Order, OrderLeg, SimulationResult, AITradeSuggestion

agents/
├── llm_risk_auditor.py      # EXISTS — extend with suggest_trades() method

dashboard/
├── app.py                   # EXISTS — add Order Builder, Journal, Charts, Flatten panels
├── components/
│   ├── order_builder.py     # NEW — Streamlit order builder UI
│   ├── trade_journal_view.py# NEW — Journal display + CSV export
│   ├── historical_charts.py # NEW — Plotly charts panel
│   └── ai_suggestions.py    # NEW — AI suggestion cards display

tests/
├── test_beta_weighter.py    # NEW
├── test_execution.py        # NEW
├── test_trade_journal.py    # NEW
├── test_order_builder.py    # NEW
└── fixtures/
    ├── sample_positions.json    # EXISTS — extend with futures + index options
    └── sample_whatif_response.json # NEW — mock IBKR whatif API response
```
