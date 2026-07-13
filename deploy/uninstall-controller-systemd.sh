#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Linux" ] || ! command -v systemctl >/dev/null 2>&1; then
  echo "Controller uninstaller requires Linux with systemd." >&2
  exit 1
fi

app_dir=${INKY_BIRD_APP_DIR:-"${HOME}/Services/inky-bird-frame"}
support_dir=${INKY_BIRD_SUPPORT_DIR:-"${HOME}/.config/inky-bird-frame"}
config_path=${INKY_BIRD_CONFIG_PATH:-"${support_dir}/config.toml"}

if ! sudo -v; then
  echo "Administrator access is required to remove system services." >&2
  exit 1
fi

for name in refresh generate catalog-publish notifications; do
  timer="inky-bird-frame-${name}.timer"
  service="inky-bird-frame-${name}.service"
  sudo systemctl disable --now "${timer}" 2>/dev/null || true
  echo "Stopped and disabled ${timer}."
  sudo systemctl stop "${service}" 2>/dev/null || true
  echo "Stopped ${service}."
done
sudo systemctl disable --now inky-bird-frame-controller.service 2>/dev/null || true
echo "Stopped and disabled inky-bird-frame-controller.service."

for name in controller refresh generate catalog-publish notifications; do
  for unit in "inky-bird-frame-${name}.service" "inky-bird-frame-${name}.timer"; do
    if [ -f "/etc/systemd/system/${unit}" ]; then
      sudo rm -f "/etc/systemd/system/${unit}"
      echo "Removed /etc/systemd/system/${unit}."
    fi
  done
done
sudo systemctl daemon-reload
echo "Reloaded the systemd unit configuration."

echo "Controller services uninstalled."
echo "Intentionally left in place:"
echo "  Application runtime and managed catalog: ${app_dir}"
echo "  Configuration: ${config_path}"
echo "  Support data: ${support_dir}"
echo "  Workspace, state, and catalog directories configured in ${config_path}"
echo "Remove those paths manually if you no longer need the controller data."
