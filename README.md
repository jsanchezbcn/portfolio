# Portfolio Risk Manager

Portfolio risk dashboard combining IBKR positions with options Greeks enrichment, regime-aware risk monitoring, AI trade suggestions, and a full trade journal.

## Features

- IBKR account positions and account summary
- IBKR-first Greeks with Tastytrade fallback diagnostics
- SPX-weighted delta tracking
- Gamma risk by DTE buckets with near-expiry warning
- Theta/Vega profile visualization with target zone
- IV vs HV analysis with edge classification
- Regime detection using VIX term structure + Polymarket recession probability
- **BetaWeighter** — per-position SPX beta-weighted delta (Tastytrade data + yfinance fallback)
- **ExecutionEngine** — multi-leg BAG combo order routing, WhatIf simulation, PARTIAL fill support
- **Trade Journal** — persistent SQLite log of all fills with VIX, regime, Greeks captured at fill time; CSV export
- **AI Risk Analyst** — GPT-4.1 trade suggestions on risk breaches; pre-fills order builder on "Use This Trade"
- **Historical Charts** — NLV vs SPX-Delta dual-axis, Theta/Delta efficiency ratio, Sebastian |Θ|/|V| ratio
- **Flatten Risk** — one-click buy-to-close all short options with mandatory confirmation dialog
- AI assistant scaffolding with tool schema definitions

## New Environment Variables

| Variable                    | Default   | Description                                        |
| --------------------------- | --------- | -------------------------------------------------- |
| `LLM_MODEL`                 | `gpt-4.1` | LLM model for AI Risk Analyst / Market Brief       |
| `SNAPSHOT_INTERVAL_SECONDS` | `900`     | Seconds between account snapshot captures (15 min) |

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

Run the full focused test suite:

```bash
./.venv/bin/python -m pytest tests/ --ignore=tests/integration -q
```

Key test files:

| File                            | Coverage                                                  |
| ------------------------------- | --------------------------------------------------------- |
| `tests/test_execution.py`       | ExecutionEngine simulate/submit/flatten_risk              |
| `tests/test_trade_journal.py`   | LocalStore record_fill/query_journal/export_csv/snapshots |
| `tests/test_ai_risk_auditor.py` | LLMRiskAuditor.suggest_trades()                           |
| `tests/test_orders.py`          | Order FSM, OrderLeg, BAG combo                            |
| `tests/test_order_builder.py`   | Streamlit order builder component                         |
| `tests/test_arbitrage.py`       | ArbitrageHunter                                           |

Integration tests (require credentials/network):

```bash
./.venv/bin/python -m pytest tests/integration -m integration -q
```

## Architecture

- `adapters/`: broker and data-source adapters (`IBKRAdapter`, `TastytradeAdapter`, `PolymarketAdapter`)
- `agent_tools/`: portfolio and market analytics logic
- `risk_engine/`: regime model and threshold detection
- `dashboard/`: Streamlit UI including **Trade Proposer Queue** panel
- `agents/trade_proposer.py`: async 300s monitoring loop — detects Greek breaches and generates top-3 hedge proposals
- `agents/proposer_engine.py`: `RiskRegimeLoader`, `BreachDetector`, `CandidateGenerator`, `ProposerEngine`
- `models/proposed_trade.py`: SQLModel ORM for `proposed_trades` table
- `specs/001-portfolio-risk-manager/`: feature specs and implementation tasks
- `specs/006-trade-proposer/`: Feature 006 Trade Proposer specs, plan, tasks

### Feature 006: Trade Proposer

Every 300 s the agent:
1. Fetches live Greeks from IBKR (or uses `MOCK_BREACH=TRUE` synthetic breach for CI)
2. Detects regime-adjusted limit breaches via `BreachDetector` → `config/risk_matrix.yaml`
3. Generates SPX/SPY/ES option candidates (bear put spreads, calendar spreads)
4. Scores by capital efficiency: `risk_reduction / (max(margin, 1) + n_legs × 0.65)`
5. Persists top-3 to `proposed_trades` table (prior Pending rows → Superseded)
6. Fires Telegram alert when `efficiency_score > PROPOSER_NOTIFY_THRESHOLD` or `regime == crisis_mode`

```bash
# CI smoke test (no gateway needed)
MOCK_BREACH=TRUE python -m agents.trade_proposer --run-once
```

## Security Notes

- Keep `.env` untracked.
- Use pre-commit with detect-secrets:

```bash
pre-commit install
pre-commit run --all-files
```
