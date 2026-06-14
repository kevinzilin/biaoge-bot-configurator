#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Virtual env [.venv] not found. Please run ./install.sh first." >&2
  exit 1
fi

exec "$PY" "scripts/launch.py" --interactive
