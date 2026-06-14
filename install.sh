#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYEXE="${PYTHON:-}"
if [[ -z "$PYEXE" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYEXE="python3"
  elif command -v python >/dev/null 2>&1; then
    PYEXE="python"
  else
    echo "Python not found. Please install Python 3.10+." >&2
    exit 1
  fi
fi

"$PYEXE" "scripts/bootstrap.py"
