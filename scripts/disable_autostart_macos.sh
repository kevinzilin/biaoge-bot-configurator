#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Usage: bash scripts/disable_autostart_macos.sh"
  exit 0
fi
if [[ "$#" -gt 0 ]]; then
  echo "Unknown option: $1" >&2
  echo "Usage: bash scripts/disable_autostart_macos.sh" >&2
  exit 1
fi

LABEL="${BIAOGE_LAUNCHD_LABEL:-com.biaoge.bot}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ -f "$PLIST" ]]; then
  launchctl unload "$PLIST" >/dev/null 2>&1 || true
  rm -f "$PLIST"
  echo "Disabled LaunchAgent: $PLIST"
else
  launchctl remove "$LABEL" >/dev/null 2>&1 || true
  echo "LaunchAgent not found: $PLIST"
fi
