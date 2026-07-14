#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Linux" ] || ! command -v systemctl >/dev/null 2>&1; then
  echo "Display uninstaller requires Linux with systemd." >&2
  exit 1
fi

app_dir=${INKY_BIRD_APP_DIR:-"${HOME}/Services/inky-bird-frame"}
support_dir=${INKY_BIRD_SUPPORT_DIR:-"${HOME}/.config/inky-bird-frame"}
config_path=${INKY_BIRD_CONFIG_PATH:-"${support_dir}/config.toml"}
venv=${INKY_BIRD_DISPLAY_VENV:-"${HOME}/.virtualenvs/pimoroni"}
noninteractive_sudo=${INKY_BIRD_NONINTERACTIVE_SUDO:-false}

sudo_command=(sudo)
case "${noninteractive_sudo}" in
  true)
    sudo_command+=(-n)
    ;;
  false)
    if ! sudo -v; then
      echo "Administrator access is required to remove the display service." >&2
      exit 1
    fi
    ;;
  *)
    echo "INKY_BIRD_NONINTERACTIVE_SUDO must be true or false." >&2
    exit 1
    ;;
esac

"${sudo_command[@]}" systemctl disable --now inky-bird-frame-display.timer 2>/dev/null || true
echo "Stopped and disabled inky-bird-frame-display.timer."
"${sudo_command[@]}" systemctl stop inky-bird-frame-display.service 2>/dev/null || true
echo "Stopped inky-bird-frame-display.service."

for unit in inky-bird-frame-display.service inky-bird-frame-display.timer; do
  if [ -f "/etc/systemd/system/${unit}" ]; then
    "${sudo_command[@]}" rm -f "/etc/systemd/system/${unit}"
    echo "Removed /etc/systemd/system/${unit}."
  fi
done
"${sudo_command[@]}" systemctl daemon-reload
echo "Reloaded the systemd unit configuration."

echo "Display node services uninstalled."
echo "Intentionally left in place:"
echo "  Application runtime and catalog copy: ${app_dir}"
echo "  Configuration: ${config_path}"
echo "  Pimoroni Python environment: ${venv}"
echo "  Display state directory configured in ${config_path}"
echo "Remove those paths manually if you no longer need the display data."
