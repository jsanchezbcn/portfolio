# Portfolio Risk Manager

Portfolio risk dashboard combining IBKR positions with options Greeks enrichment and regime-aware risk monitoring.

## Features

- IBKR account positions and account summary
- IBKR-first Greeks with Tastytrade fallback diagnostics
- SPX-weighted delta tracking
- Gamma risk by DTE buckets with near-expiry warning
- Theta/Vega profile visualization with target zone
- IV vs HV analysis with edge classification
- Regime detection using VIX term structure + Polymarket recession probability
- AI assistant scaffolding with tool schema definitions

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy environment template and configure credentials:

```bash
cp .env.example .env
```

4. Start services:

```bash
./start_dashboard.sh
```

This script ensures IBKR Client Portal is reachable, then launches Streamlit.

## Testing

Run focused tests with project venv:

```bash
./.venv/bin/python -m pytest tests/test_portfolio_tools.py -q
./.venv/bin/python -m pytest tests/test_iv_hv_calculator.py -q
./.venv/bin/python -m pytest tests/test_polymarket_adapter.py -q
```

Integration tests (require credentials/network):

```bash
./.venv/bin/python -m pytest tests/integration -m integration -q
```

## Architecture

- `adapters/`: broker and data-source adapters (`IBKRAdapter`, `TastytradeAdapter`, `PolymarketAdapter`)
- `agent_tools/`: portfolio and market analytics logic
- `risk_engine/`: regime model and threshold detection
- `dashboard/`: Streamlit UI
- `specs/001-portfolio-risk-manager/`: feature specs and implementation tasks

## Security Notes

- Keep `.env` untracked.
- Use pre-commit with detect-secrets:

```bash
pre-commit install
pre-commit run --all-files
```
