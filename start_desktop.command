#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

if [[ ! -x "./start_desktop.sh" ]]; then
  echo "start_desktop.sh not found or not executable in $ROOT_DIR"
  read -r -p "Press Enter to close..." _
  exit 1
fi

./start_desktop.sh
