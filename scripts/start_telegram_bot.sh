#!/usr/bin/env bash
# scripts/start_telegram_bot.sh — Start the Portfolio Risk Telegram Bot
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV="$PROJECT_ROOT/.venv/bin/python"
BOT_SCRIPT="$PROJECT_ROOT/agents/telegram_bot.py"
LOG_FILE="/tmp/telegram_bot.log"
PID_FILE="/tmp/telegram_bot.pid"

cd "$PROJECT_ROOT"

# ── Check if already running ──────────────────────────────────────────────────
if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Telegram bot already running (PID $OLD_PID). Use 'kill $OLD_PID' to stop it."
        exit 0
    fi
fi

# ── Launch ────────────────────────────────────────────────────────────────────
echo "Starting Telegram bot…"
nohup "$VENV" "$BOT_SCRIPT" >> "$LOG_FILE" 2>&1 &
BOT_PID=$!
echo "$BOT_PID" > "$PID_FILE"

sleep 1
if kill -0 "$BOT_PID" 2>/dev/null; then
    echo "Telegram bot started (PID $BOT_PID)"
    echo "Log:  $LOG_FILE"
    echo "Stop: kill $BOT_PID"
else
    echo "ERROR: Bot exited immediately. Check $LOG_FILE"
    cat "$LOG_FILE" | tail -20
    exit 1
fi
