#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLIENTPORTAL_DIR="$ROOT_DIR/clientportal"
PORTAL_URL="${IBKR_GATEWAY_URL:-https://localhost:5001}"
WAIT_SECONDS="${IBKR_RESTART_WAIT_SECONDS:-45}"
OPEN_AUTH=0
STATUS_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --open-auth)
      OPEN_AUTH=1
      shift
      ;;
    --status)
      STATUS_ONLY=1
      shift
      ;;
    --wait)
      WAIT_SECONDS="${2:-45}"
      shift 2
      ;;
    *)
      echo "Unknown arg: $1"
      echo "Usage: $0 [--status] [--open-auth] [--wait SECONDS]"
      exit 2
      ;;
  esac
done

is_up() {
  curl -k -sS --max-time 2 "$PORTAL_URL/v1/api/tickle" >/dev/null 2>&1
}

auth_status() {
  curl -k -sS --max-time 4 "$PORTAL_URL/v1/api/iserver/auth/status" || true
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
}

stop_gateway() {
  echo "Stopping IBKR Client Portal Gateway process(es)..."

  # Kill anything holding port 5001 first (most precise)
  local pids
  pids="$(lsof -ti tcp:5001 || true)"
  if [[ -n "$pids" ]]; then
    echo "$pids" | xargs kill -TERM 2>/dev/null || true
    sleep 1
    # force-kill leftovers
    pids="$(lsof -ti tcp:5001 || true)"
    if [[ -n "$pids" ]]; then
      echo "$pids" | xargs kill -KILL 2>/dev/null || true
    fi
  fi

  # Belt-and-suspenders: known process patterns
  pkill -f "clientportal/.*/bin/run.sh" 2>/dev/null || true
  pkill -f "root/conf.yaml" 2>/dev/null || true
  pkill -f "ibgroup.web.core.clientportal.gw" 2>/dev/null || true

  sleep 1
}

wait_until_up() {
  local max_wait="$1"
  local elapsed=0
  while (( elapsed < max_wait )); do
    if is_up; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  return 1
}

if [[ "$STATUS_ONLY" -eq 1 ]]; then
  if is_up; then
    echo "IBKR portal status: UP"
    echo "Auth status: $(auth_status)"
    exit 0
  fi
  echo "IBKR portal status: DOWN"
  exit 1
fi

stop_gateway
start_gateway

if wait_until_up "$WAIT_SECONDS"; then
  echo "IBKR Client Portal is up after restart."
  echo "Auth status: $(auth_status)"
else
  echo "Client Portal did not become ready within ${WAIT_SECONDS}s."
  echo "Check: $ROOT_DIR/.clientportal.log"
  exit 1
fi

if [[ "$OPEN_AUTH" -eq 1 ]]; then
  # Open login/landing page so user can authenticate again.
  open "$PORTAL_URL" 2>/dev/null || true
  echo "Opened browser for IBKR authentication: $PORTAL_URL"
fi
