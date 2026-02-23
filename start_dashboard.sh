#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENTPORTAL_DIR="$ROOT_DIR/clientportal"
STREAMLIT_PORT="${STREAMLIT_PORT:-8506}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python not found at $PYTHON_BIN"
  echo "Set PYTHON_BIN env var or create .venv first."
  exit 1
fi

is_gateway_up() {
  curl -k -sS --max-time 2 https://localhost:5001/v1/api/tickle >/dev/null 2>&1
}

start_gateway() {
  if [[ ! -d "$CLIENTPORTAL_DIR" ]]; then
    echo "clientportal directory not found at $CLIENTPORTAL_DIR"
    exit 1
  fi

  echo "Starting IBKR Client Portal Gateway..."
  (
    cd "$CLIENTPORTAL_DIR"
    nohup ./bin/run.sh root/conf.yaml > "$ROOT_DIR/.clientportal.log" 2>&1 &
  )

  for i in {1..60}; do
    if is_gateway_up; then
      echo "IBKR Client Portal is up."
      return 0
    fi
    sleep 1
  done

  echo "Client Portal did not become ready in time. Check .clientportal.log"
  exit 1
}

if is_gateway_up; then
  echo "IBKR Client Portal already running."
else
  start_gateway
fi

# ── Start background workers ──────────────────────────────────────────────────
echo "Starting portfolio workers..."

# Terminate any stale workers/bot from a previous session
pkill -f "portfolio_worker.py" 2>/dev/null || true
pkill -f "telegram_bot.py" 2>/dev/null || true
sleep 1

nohup "$PYTHON_BIN" "$ROOT_DIR/workers/portfolio_worker.py" --worker-id worker-1 \
  > /tmp/worker-1.log 2>&1 &
echo "  Worker-1 started (PID $!)."

nohup "$PYTHON_BIN" "$ROOT_DIR/workers/portfolio_worker.py" --worker-id worker-2 \
  > /tmp/worker-2.log 2>&1 &
echo "  Worker-2 started (PID $!)."

# ── Start Telegram bot ────────────────────────────────────────────────────────
if [[ -f "$ROOT_DIR/agents/telegram_bot.py" ]]; then
  nohup "$PYTHON_BIN" "$ROOT_DIR/agents/telegram_bot.py" \
    > /tmp/telegram_bot.log 2>&1 &
  echo "  Telegram bot started (PID $!)."
fi

echo "Starting Streamlit on port $STREAMLIT_PORT..."
cd "$ROOT_DIR"
exec "$PYTHON_BIN" -m streamlit run dashboard/app.py \
  --server.headless true \
  --server.port "$STREAMLIT_PORT" \
  --server.fileWatcherType none
