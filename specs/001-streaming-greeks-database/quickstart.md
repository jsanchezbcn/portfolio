# Quickstart: Streaming Greeks and Database

## Prerequisites

- Python 3.11+
- PostgreSQL 16 running locally
- Active `.env` with broker credentials and DB variables
- IBKR gateway/session available at `wss://localhost:5000/v1/api/ws`

## 1) Install dependencies

```bash
pip install -r requirements.txt
pip install asyncpg websockets tastytrade python-dotenv
```

## 2) Validate environment variables

Required DB variables:

- `DB_HOST` (expected: `localhost`)
- `DB_PORT` (expected: `5432`)
- `DB_NAME` (expected: `portfolio_engine`)
- `DB_USER` (expected: `portfolio`)
- `DB_PASS`

Required broker/auth variables: existing IBKR and Tastytrade credentials used by current project.

## 3) Start local PostgreSQL

Ensure database exists and is reachable.

Example checks:

```bash
psql -h localhost -p 5432 -U portfolio -d portfolio_engine -c "SELECT 1;"
```

## 4) Run streaming ingestion service

Run your service entrypoint that initializes:

- `database/db_manager.py`
- `streaming/ibkr_ws.py`
- `streaming/tasty_dxlink.py`
- `core/processor.py`

Expected runtime behavior:

- Auto-creates `trades` and `greek_snapshots` if missing
- Starts IBKR + Tastytrade stream tasks independently
- Sends IBKR heartbeat every 60s
- Buffers and flushes snapshots every 1 second or 50 records

## 5) Verify database writes

```bash
psql -h localhost -p 5432 -U portfolio -d portfolio_engine -c "SELECT count(*) FROM greek_snapshots;"
psql -h localhost -p 5432 -U portfolio -d portfolio_engine -c "SELECT broker, max(event_time) FROM greek_snapshots GROUP BY broker;"
```

## 6) Verify latency SLO (<500ms)

- Inspect ingestion logs/metrics for p95 tick-to-write latency.
- Confirm p95 remains `< 500ms` under normal market activity.

## 7) Run tests

```bash
pytest tests/test_unified_position.py tests/test_portfolio_tools.py
pytest tests/integration -k "stream or db" -m "integration or manual"
```

## 8) IBKR stream speed + Greeks coverage benchmark

Use the CLI benchmark to measure websocket connection latency, first-stream-message latency, and per-contract Greek coverage (`delta`, `gamma`, `theta`, `vega`) for a live account.

```bash
./.venv/bin/python debug_greeks_cli.py \
	--account U2052408 \
	--ibkr-stream-benchmark \
	--ibkr-ws-url wss://localhost:5001/v1/api/ws \
	--max-options 20 \
	--stream-timeout-seconds 12 \
	--output-prefix .ibkr_stream_u2052408
```

Artifacts:

- `.ibkr_stream_u2052408.json`: timing summary + stream payload diagnostics
- `.ibkr_stream_u2052408.csv`: per-position native/stream Greeks coverage table

If `coverage` shows zeros and sample payloads are only `topic=system`, the websocket is connected but the subscription payload is not aligned with IBKR market data topic format.

## 9) Verification notes (latest run)

- Latest benchmark artifact prefix: `.ibkr_stream_u2052408_postfix6`
- Connection status: successful websocket connect (`connect_error: null`)
- Timing sample: `connect_ws_ms ~ 47.61`, `first_stream_message_ms ~ 230.76`
- Account context: selected account switched to `U2052408`
- Current limitation: contract-level stream messages were not observed in the benchmark window, so Greek coverage remained `0` for all sampled contracts

## Troubleshooting

- If IBKR disconnects, verify gateway auth/session and TLS endpoint availability.
- If Tastytrade stream fails, verify token refresh and account entitlements.
- If DB writes stall, check pool saturation, table indexes, and partition creation.
