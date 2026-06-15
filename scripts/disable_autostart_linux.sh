#!/usr/bin/env bash
set -euo pipefail

MODE="user"
STOP_RUNNING=0
for arg in "$@"; do
  case "$arg" in
    --system) MODE="system" ;;
    --stop) STOP_RUNNING=1 ;;
    *)
      echo "Unknown option: $arg" >&2
      echo "Usage: bash scripts/disable_autostart_linux.sh [--system] [--stop]" >&2
      exit 1
      ;;
  esac
done

NAME="biaoge-bot.service"

if [[ "$MODE" == "system" ]]; then
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "System mode requires root. Try: sudo bash scripts/disable_autostart_linux.sh --system" >&2
    exit 1
  fi
  SERVICE_PATH="/etc/systemd/system/$NAME"
  SYSTEMCTL=(systemctl)
else
  SERVICE_PATH="$HOME/.config/systemd/user/$NAME"
  SYSTEMCTL=(systemctl --user)
fi

if [[ "$STOP_RUNNING" == "1" ]]; then
  "${SYSTEMCTL[@]}" stop "$NAME" >/dev/null 2>&1 || true
fi

"${SYSTEMCTL[@]}" disable "$NAME" >/dev/null 2>&1 || true
rm -f "$SERVICE_PATH"
"${SYSTEMCTL[@]}" daemon-reload
"${SYSTEMCTL[@]}" reset-failed "$NAME" >/dev/null 2>&1 || true

echo "Disabled systemd service: $SERVICE_PATH"
if [[ "$STOP_RUNNING" != "1" ]]; then
  echo "Stop current process if needed: ${SYSTEMCTL[*]} stop $NAME"
fi
