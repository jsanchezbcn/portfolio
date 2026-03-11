#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
HOST="${IB_SOCKET_HOST:-127.0.0.1}"
PORT="${IB_SOCKET_PORT:-4001}"
CLIENT_ID="${DESKTOP_CLIENT_ID:-30}"
LOG_LEVEL="${DESKTOP_LOG_LEVEL:-INFO}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python not found at $PYTHON_BIN"
  echo "Set PYTHON_BIN env var or create .venv first."
  exit 1
fi

cd "$ROOT_DIR"

# Ensure imports work regardless of caller cwd.
export PYTHONPATH="$ROOT_DIR:${PYTHONPATH:-}"

exec "$PYTHON_BIN" -m desktop.main \
  --host "$HOST" \
  --port "$PORT" \
  --client-id "$CLIENT_ID" \
  --log-level "$LOG_LEVEL"
