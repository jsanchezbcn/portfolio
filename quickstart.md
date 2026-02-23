# Quickstart

## Prerequisites

- Python 3.11+
- IBKR Client Portal Gateway binaries present in `clientportal/`
- Optional: Tastytrade credentials for Greeks fallback and integration tests

## 1) Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Configure environment

```bash
cp .env.example .env
```

Populate credentials in `.env` as needed.

## 3) Start dashboard

```bash
chmod +x start_dashboard.sh
./start_dashboard.sh
```

Open Streamlit URL shown by the script.

## 4) Validate core features

- Select IBKR account in sidebar
- Use `Greeks Diagnostics` controls
- Confirm Gamma Risk by DTE chart renders
- Confirm Theta/Vega Profile renders
- Confirm IV vs HV Analysis section renders (when IV and history are available)
- Confirm regime banner includes Polymarket recession probability source/timestamp

## 5) Validate Algo Execution Platform (US3-US7)

### Order Builder + Trade Journal
- Open the `Order Builder` panel and add a leg (e.g., SPX PUT, qty -1)
- Click `Simulate` -- confirm the simulated order appears in the Trade Journal table

### AI Risk Analyst
- Trigger a risk breach (set a very low delta limit in config/risk_matrix.yaml temporarily)
- Confirm the `AI Risk Analyst` section shows a red breach banner
- Confirm suggestion cards appear within ~10s (requires LLM_API_KEY set)

### Historical Charts
- Scroll to `Historical Charts` section
- Confirm `Account NLV vs Delta` dual-axis chart renders (may show empty if no snapshots yet)
- Confirm `Sebastian Ratio |theta|/|vega|` chart renders with green 0.25-0.40 band

### Flatten Risk
- Scroll to `Flatten Risk` section
- Click `Flatten Risk` button (or sidebar shortcut)
- Confirm confirmation dialog with order table appears
- Click `Cancel` to dismiss without submitting

## 6) Run tests

```bash
# Full test suite
./.venv/bin/python -m pytest tests/ -q

# Individual suites
./.venv/bin/python -m pytest tests/test_tastytrade_adapter.py -q
./.venv/bin/python -m pytest tests/test_iv_hv_calculator.py -q
./.venv/bin/python -m pytest tests/test_polymarket_adapter.py -q
./.venv/bin/python -m pytest tests/test_ai_risk_auditor.py -q
./.venv/bin/python -m pytest tests/test_execution.py -q
```
