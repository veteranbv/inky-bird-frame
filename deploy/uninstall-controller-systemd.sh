#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Linux" ] || ! command -v systemctl >/dev/null 2>&1; then
  echo "Controller uninstaller requires Linux with systemd." >&2
  exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
native_names=$(python3 "${root}/src/inky_bird_frame/native_names.py")
service_prefix="" app_dir="" support_dir="" config_path=""
eval "${native_names}"

if ! sudo -v; then
  echo "Administrator access is required to remove system services." >&2
  exit 1
fi

for name in refresh generate catalog-publish notifications; do
  timer="${service_prefix}-${name}.timer"
  service="${service_prefix}-${name}.service"
  sudo systemctl disable --now "${timer}" 2>/dev/null || true
  echo "Stopped and disabled ${timer}."
  sudo systemctl stop "${service}" 2>/dev/null || true
  echo "Stopped ${service}."
done
sudo systemctl disable --now "${service_prefix}-controller.service" 2>/dev/null || true
sudo systemctl disable --now "${service_prefix}-scheduler.service" 2>/dev/null || true
echo "Stopped and disabled ${service_prefix}-controller.service."

for name in controller scheduler refresh generate catalog-publish notifications; do
  for unit in "${service_prefix}-${name}.service" "${service_prefix}-${name}.timer"; do
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
