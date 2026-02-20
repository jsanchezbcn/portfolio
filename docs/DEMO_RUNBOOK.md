# Demo Runbook

## Prerequisites

- IBKR Client Portal accessible at `https://localhost:5001`
- `.venv` exists and dependencies installed
- `.env` configured for broker credentials/tokens

## Demo Flow (Recommended)

### 1. Run deterministic analytics demo

```bash
PYwTHONPATH=. ./.venv/bin/python demo_us7_deterministic.py
```

Expected outcome: prints Theta/Vega ratio zone, gamma-by-DTE, and IV/HV edge signals.

### 2. Run focused regression suite

```bash
PYTHONPATH=. ./.venv/bin/pytest tests/test_unified_position.py tests/test_regime_detector.py tests/test_market_data_tools.py tests/test_portfolio_tools.py tests/test_ibkr_adapter.py tests/test_tastytrade_adapter.py tests/test_end_to_end.py -q
```

Expected outcome: all tests pass.

### 3. Launch dashboard

```bash
chmod +x start_dashboard.sh && ./start_dashboard.sh
```

Open `http://localhost:8506`.

## What to Show in UI

- Regime banner with VIX/term-structure + macro probability
- Portfolio summary metrics
- Gamma by DTE chart + warning behavior
- Theta/Vega profile chart with target zone
- IV vs HV table and edge summary
- Sidebar `Data Freshness` timestamps

## Shutdown / Cleanup

```bash
pkill -f 'streamlit run dashboard/app.py' || true
rm -f .positions_snapshot_*.json .greeks_debug_*.json .greeks_debug_*.csv .greeks_debug_*.log
```
