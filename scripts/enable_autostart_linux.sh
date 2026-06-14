#!/usr/bin/env bash
set -euo pipefail

MODE="user"
RUN_NOW=0
for arg in "$@"; do
  case "$arg" in
    --system) MODE="system" ;;
    --now) RUN_NOW=1 ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: bash scripts/enable_autostart_linux.sh [--system] [--now]" >&2
      exit 1
      ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LAUNCH="$ROOT/scripts/launch.py"
NAME="biaoge-bot.service"

if [[ ! -x "$PY" ]]; then
  echo "Virtualenv python not found: $PY" >&2
  exit 1
fi

mkdir -p "$ROOT/logs"

if [[ "$MODE" == "system" ]]; then
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "System mode requires root. Try: sudo bash scripts/enable_autostart_linux.sh --system" >&2
    exit 1
  fi
  SERVICE_PATH="/etc/systemd/system/$NAME"
  SYSTEMCTL=(systemctl)
  WANTED_BY="multi-user.target"
else
  SERVICE_DIR="$HOME/.config/systemd/user"
  mkdir -p "$SERVICE_DIR"
  SERVICE_PATH="$SERVICE_DIR/$NAME"
  SYSTEMCTL=(systemctl --user)
  WANTED_BY="default.target"
fi

cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=Biaoge Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=BIAOGE_ROOT=$ROOT
ExecStart=$PY $LAUNCH --non-interactive
Restart=always
RestartSec=10

[Install]
WantedBy=$WANTED_BY
EOF

"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" enable "$NAME"
if [[ "$RUN_NOW" == "1" ]]; then
  "${SYSTEMCTL[@]}" start "$NAME"
fi

echo "Enabled systemd service: $SERVICE_PATH"
echo "Status: ${SYSTEMCTL[*]} status $NAME"
if [[ "$RUN_NOW" != "1" ]]; then
  echo "Start now: ${SYSTEMCTL[*]} start $NAME"
fi
if [[ "$MODE" == "user" ]]; then
  echo "For boot before login, rerun with --system or enable lingering: loginctl enable-linger \"$(id -un)\""
fi
