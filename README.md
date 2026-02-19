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

---

## AI Trading Agent — Environment Variables (002-ai-trading-agent)

The following environment variables configure the AI trading agent subsystem:

| Variable | Required | Default | Description |
|---|---|---|---|
| `NEWS_PROVIDER` | No | `alpaca` | News API to use: `alpaca` or `finnhub` |
| `NEWS_API_KEY` | Yes (for live) | — | API key for Alpaca (`APCA-API-KEY-ID`) or Finnhub |
| `NEWS_API_SECRET` | Alpaca only | — | Alpaca API secret key (`APCA-API-SECRET-KEY`) |
| `LLM_MODEL` | No | `gpt-4o-mini` | OpenAI-compatible model name for sentiment scoring and trade explanation |
| `OPENAI_API_KEY` | Yes (for live) | — | OpenAI API key (or GitHub Models token) |
| `OPENAI_API_BASE` | No | OpenAI default | Override endpoint, e.g. GitHub Models: `https://models.inference.ai.azure.com` |
| `NEWS_INTERVAL_SECONDS` | No | `900` | NewsSentry polling interval in seconds (default 15 min) |
| `ARB_FEE_PER_LEG` | No | `0.65` | Override fe| `ARB_FEE_PER_LEG` | No | `0.65` | Override fe| `ARB_FEE_PER_LEG` | Noatr|x.ya| `ARB_FEE_PER_LEG` | No | `0.65` | Override fe| `ARB_FEE_PER_LEG` : polls news APIs and writes scored `SentimentRecord` to `market_intel` table
- `agents/arb_hunter.py` — `ArbHunter`: scans option chains for Put-- `agents/arb_hunter.py` — `ArbHunter`: scans option chains for Put-- `agder_manager.py` — `OrderManager`: stages orders in IBKR TWS with `transmit=False` via Client Portal REST; persists to `staged_orders` table
- `skills/explain_performance.py` — `Expla- `skills/explain_performance.py` — `Expla- `skills/explain_performance.py` — `Expla- `skills/explain_perfrders` — orders staged in TWS (never auto-transmitted)
- `market_intel` — sentiment - `market_intel` — sentiment - `market_intel` — sentiment - `market_intel` — sentiment - `market_intel` —context (thesis + Greeks) for ExplainPerformanceSkill
