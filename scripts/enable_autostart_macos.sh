#!/usr/bin/env bash
set -euo pipefail

RUN_NOW=0
if [[ "${1:-}" == "--now" ]]; then
  RUN_NOW=1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LAUNCH="$ROOT/scripts/launch.py"
LABEL="${BIAOGE_LAUNCHD_LABEL:-com.biaoge.bot}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ ! -x "$PY" ]]; then
  echo "Virtualenv python not found: $PY" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$LAUNCH</string>
    <string>--non-interactive</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/launchd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>BIAOGE_ROOT</key><string>$ROOT</string>
  </dict>
</dict>
</plist>
EOF

if [[ "$RUN_NOW" == "1" ]]; then
  launchctl unload "$PLIST" >/dev/null 2>&1 || true
  launchctl load "$PLIST"
fi
echo "Enabled LaunchAgent: $PLIST"
if [[ "$RUN_NOW" != "1" ]]; then
  echo "Start now: launchctl load \"$PLIST\""
fi
echo "Stop: launchctl unload \"$PLIST\""
