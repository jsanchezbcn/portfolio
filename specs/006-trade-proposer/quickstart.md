# Quickstart: Trade Proposer Agent

## 1. Overview

The Trade Proposer Agent runs as a background worker. It polls the current risk state (from the database or live adapter), checks against `config/risk_matrix.yaml`, and generates hedges when limits are breached.

## 2. Configuration

Ensure your `config/risk_matrix.yaml` defines the limits for each regime.
The agent uses the following environment variables:

- `DB_URL`: Connection string for PostgreSQL.
- `IB_API_MODE`: `SOCKET` or `PORTAL`.
- `PROPOSER_INTERVAL`: Default 300 (5 minutes).

## 3. Running the Agent

From the repository root:

```bash
python -m agents.trade_proposer
```

## 4. Reviewing Proposals

Proposals appear in the **"Trade Proposer Queue"** section of the Streamlit dashboard.

1. Status is `Pending` by default.
2. Clicking "Approve" (In Dashboard) will hand the trade off to the `ExecutionAgent` (Feature 003).
3. Old proposals are automatically marked `Superseded` when a new risk check runs.

## 5. Development & Testing

Run unit tests for the engine:

```bash
pytest tests/test_proposer_engine.py
```

To test with mock breaches:

```bash
MOCK_BREACH=TRUE python -m agents.trade_proposer --run-once
```
