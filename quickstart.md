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

## 5) Run tests

```bash
./.venv/bin/python -m pytest tests/test_tastytrade_adapter.py -q
./.venv/bin/python -m pytest tests/test_iv_hv_calculator.py -q
./.venv/bin/python -m pytest tests/test_polymarket_adapter.py -q
```
