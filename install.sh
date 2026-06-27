#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PYEXE="${PYTHON:-}"
if [[ -n "$PYEXE" ]]; then
  if ! "$PYEXE" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    echo "Python 3.10+ not found or cannot run: $PYEXE" >&2
    exit 1
  fi
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  PYEXE="python3"
elif command -v python >/dev/null 2>&1 && python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  PYEXE="python"
else
  echo "Python 3.10+ not found or cannot run. Please install Python 3.10+." >&2
  exit 1
fi

"$PYEXE" "scripts/bootstrap.py"
