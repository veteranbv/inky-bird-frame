#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Linux" ] || ! command -v systemctl >/dev/null 2>&1; then
  echo "Display installer requires Linux with systemd." >&2
  exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
app_dir=${INKY_BIRD_APP_DIR:-"${HOME}/Services/inky-bird-frame"}
support_dir=${INKY_BIRD_SUPPORT_DIR:-"${HOME}/.config/inky-bird-frame"}
config_path=${INKY_BIRD_CONFIG_PATH:-"${support_dir}/config.toml"}
venv=${INKY_BIRD_DISPLAY_VENV:-"${HOME}/.virtualenvs/pimoroni"}
run_initial_display=${INKY_BIRD_RUN_INITIAL_DISPLAY:-false}

if [ ! -f "${config_path}" ]; then
  echo "Display-node configuration is missing: ${config_path}" >&2
  exit 1
fi
if [ ! -x "${venv}/bin/python" ]; then
  echo "Pimoroni Python environment is missing: ${venv}" >&2
  exit 1
fi
if ! sudo -v; then
  echo "Administrator access is required to install the display service." >&2
  exit 1
fi

chmod 600 "${config_path}"
mkdir -p "${app_dir}/src" "${app_dir}/catalog" "${app_dir}/deploy" "${support_dir}"
if [ "${root}" != "${app_dir}" ]; then
  rsync -a --delete "${root}/src/" "${app_dir}/src/"
  rsync -a "${root}/catalog/" "${app_dir}/catalog/"
  install -m 0755 "${root}/deploy/install-display-local.sh" "${app_dir}/deploy/"
  for file in pyproject.toml uv.lock README.md LICENSE; do
    install -m 0644 "${root}/${file}" "${app_dir}/${file}"
  done
fi
"${venv}/bin/python" -m pip install --disable-pip-version-check -e "${app_dir}[inky]"

unit_dir=$(mktemp -d)
trap 'rm -rf "${unit_dir}"' EXIT
"${venv}/bin/python" - \
  "${unit_dir}" "${app_dir}" "${config_path}" "${venv}" "$(id -un)" <<'PY'
import sys
from pathlib import Path

from inky_bird_frame.config import load_config
from inky_bird_frame.installation import display_systemd_units


unit_dir, app_dir, config_path, venv = map(Path, sys.argv[1:5])
user = sys.argv[5]
executable = venv / "bin/inky-bird-frame"
config = load_config(config_path)
units = display_systemd_units(
    config,
    executable=executable,
    app_dir=app_dir,
    config_path=config_path,
    user=user,
)
for name, content in units.items():
    (unit_dir / name).write_text(content)
PY

sudo install -m 0644 \
  "${unit_dir}/inky-bird-frame-display.service" \
  /etc/systemd/system/inky-bird-frame-display.service
sudo install -m 0644 \
  "${unit_dir}/inky-bird-frame-display.timer" \
  /etc/systemd/system/inky-bird-frame-display.timer
sudo systemctl daemon-reload
sudo systemctl enable inky-bird-frame-display.timer
sudo systemctl restart inky-bird-frame-display.timer
if [ "${run_initial_display}" = true ]; then
  sudo systemctl start inky-bird-frame-display.service
fi
systemctl is-enabled --quiet inky-bird-frame-display.timer
systemctl is-active --quiet inky-bird-frame-display.timer

echo "Display node installed from ${root} into ${app_dir}."
echo "Configuration: ${config_path}"
