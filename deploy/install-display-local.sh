#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Linux" ] || ! command -v systemctl >/dev/null 2>&1; then
  echo "Display installer requires Linux with systemd." >&2
  exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
native_names=$(python3 "${root}/src/inky_bird_frame/native_names.py")
service_prefix="" app_dir="" support_dir="" config_path=""
eval "${native_names}"
export INKY_BIRD_SERVICE_PREFIX="${service_prefix}"
venv=${INKY_BIRD_DISPLAY_VENV:-"${HOME}/.virtualenvs/pimoroni"}
run_initial_display=${INKY_BIRD_RUN_INITIAL_DISPLAY:-false}
noninteractive_sudo=${INKY_BIRD_NONINTERACTIVE_SUDO:-false}

if [ ! -f "${config_path}" ]; then
  echo "Display-node configuration is missing: ${config_path}" >&2
  exit 1
fi
if [ ! -x "${venv}/bin/python" ]; then
  echo "Pimoroni Python environment is missing: ${venv}" >&2
  exit 1
fi
# Distribution ownership changes only while the display role is quiescent.
# setup is not a substitute for an operator's backup/upgrade transaction.
if "${venv}/bin/python" -m pip show inky-bird-frame >/dev/null 2>&1 || \
    "${venv}/bin/python" -m pip show inky-bird >/dev/null 2>&1; then
  for suffix in timer service; do
    state=$(systemctl show --property=ActiveState --value "${service_prefix}-display.${suffix}")
    case "${state}" in
      inactive | failed) ;;
      *)
        echo "Stop the existing display timer and let its service finish before updating its runtime. Preserve a recoverable runtime backup first. No changes made." >&2
        exit 1
        ;;
    esac
  done
fi
sudo_command=(sudo)
case "${noninteractive_sudo}" in
  true)
    sudo_command+=(-n)
    if ! "${sudo_command[@]}" -v; then
      echo "Noninteractive administrator access is required before installing the display service. No changes made." >&2
      exit 1
    fi
    ;;
  false)
    if ! sudo -v; then
      echo "Administrator access is required to install the display service." >&2
      exit 1
    fi
    ;;
  *)
    echo "INKY_BIRD_NONINTERACTIVE_SUDO must be true or false." >&2
    exit 1
    ;;
esac

chmod 600 "${config_path}"
mkdir -p "${app_dir}/src" "${app_dir}/catalog" "${app_dir}/deploy" "${support_dir}"
if [ "${root}" != "${app_dir}" ]; then
  mkdir -p "${app_dir}/docs"
  rsync -a --delete "${root}/src/" "${app_dir}/src/"
  rsync -a "${root}/catalog/" "${app_dir}/catalog/"
  install -m 0755 "${root}/deploy/install-display-local.sh" "${app_dir}/deploy/"
  for file in pyproject.toml uv.lock README.md LICENSE docs/browser-access.md; do
    install -m 0644 "${root}/${file}" "${app_dir}/${file}"
  done
fi
# The old and new distributions own the same import files. Remove the old
# RECORD before installing the renamed distribution, never after it.
if "${venv}/bin/python" -m pip show inky-bird-frame >/dev/null 2>&1; then
  "${venv}/bin/python" -m pip uninstall --yes inky-bird-frame
fi
"${venv}/bin/python" -m pip install --disable-pip-version-check -e "${app_dir}[inky]"

unit_dir=$(mktemp -d)
trap 'rm -rf "${unit_dir}"' EXIT
"${venv}/bin/python" - \
  "${unit_dir}" "${app_dir}" "${config_path}" "${venv}" "$(id -un)" <<'PY'
import os
import sys
from pathlib import Path

from inky_bird_frame.config import load_config
from inky_bird_frame.installation import display_systemd_units


unit_dir, app_dir, config_path, venv = map(Path, sys.argv[1:5])
user = sys.argv[5]
executable = venv / "bin/inky-bird"
config = load_config(config_path)
units = display_systemd_units(
    config,
    executable=executable,
    app_dir=app_dir,
    config_path=config_path,
    user=user,
    service_prefix=os.environ["INKY_BIRD_SERVICE_PREFIX"],
)
for name, content in units.items():
    (unit_dir / name).write_text(content)
PY

"${sudo_command[@]}" install -m 0644 \
  "${unit_dir}/${service_prefix}-display.service" \
  "/etc/systemd/system/${service_prefix}-display.service"
"${sudo_command[@]}" install -m 0644 \
  "${unit_dir}/${service_prefix}-display.timer" \
  "/etc/systemd/system/${service_prefix}-display.timer"
"${sudo_command[@]}" systemctl daemon-reload
"${sudo_command[@]}" systemctl enable "${service_prefix}-display.timer"
"${sudo_command[@]}" systemctl restart "${service_prefix}-display.timer"
if [ "${run_initial_display}" = true ]; then
  "${sudo_command[@]}" systemctl start "${service_prefix}-display.service"
fi
systemctl is-enabled --quiet "${service_prefix}-display.timer"
systemctl is-active --quiet "${service_prefix}-display.timer"

echo "Display node installed from ${root} into ${app_dir}."
echo "Configuration: ${config_path}"
