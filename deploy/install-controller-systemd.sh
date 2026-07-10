#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Linux" ] || ! command -v systemctl >/dev/null 2>&1; then
  echo "Controller installer requires Linux with systemd." >&2
  exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
app_dir=${INKY_BIRD_APP_DIR:-"${HOME}/Services/inky-bird-frame"}
support_dir=${INKY_BIRD_SUPPORT_DIR:-"${HOME}/.config/inky-bird-frame"}
config_path=${INKY_BIRD_CONFIG_PATH:-"${support_dir}/config.toml"}
python_version=${INKY_BIRD_PYTHON_VERSION:-3.11}
quiesce_timeout_seconds=1800
uv_bin=${UV_BIN:-}
if [ -z "${uv_bin}" ]; then
  uv_bin=$(command -v uv || true)
fi

if [ ! -f "${config_path}" ]; then
  echo "Controller configuration is missing: ${config_path}" >&2
  exit 1
fi
if [ -z "${uv_bin}" ] || [ ! -x "${uv_bin}" ]; then
  echo "uv is not executable. Install uv or set UV_BIN." >&2
  exit 1
fi
if ! sudo -v; then
  echo "Administrator access is required to install system services." >&2
  exit 1
fi

unit_dir=$(mktemp -d)
installation_complete=false
active_timers=()
cleanup() {
  status=$?
  trap - EXIT
  if [ "${installation_complete}" != true ]; then
    for timer in "${active_timers[@]}"; do
      sudo systemctl start "${timer}" || true
    done
  fi
  rm -rf "${unit_dir}"
  exit "${status}"
}
trap cleanup EXIT

for name in refresh generate catalog-publish notifications; do
  timer="inky-bird-frame-${name}.timer"
  if systemctl is-active --quiet "${timer}"; then
    active_timers+=("${timer}")
  fi
  sudo systemctl stop "${timer}" 2>/dev/null || true
done
deadline=$((SECONDS + quiesce_timeout_seconds))
for name in refresh generate catalog-publish notifications; do
  service="inky-bird-frame-${name}.service"
  while true; do
    state=$(systemctl show --property=ActiveState --value "${service}" 2>/dev/null || true)
    case "${state}" in
      active | activating | deactivating | reloading) ;;
      *) break ;;
    esac
    if ((SECONDS >= deadline)); then
      echo "Timed out waiting for ${service} to finish." >&2
      exit 1
    fi
    sleep 2
  done
done

chmod 600 "${config_path}"
mkdir -p "${app_dir}/src" "${app_dir}/catalog" "${app_dir}/deploy" "${support_dir}"
if [ "${root}" != "${app_dir}" ]; then
  rsync -a --delete "${root}/src/" "${app_dir}/src/"
  rsync -a "${root}/catalog/" "${app_dir}/catalog/"
  install -m 0755 "${root}/deploy/install-controller-systemd.sh" "${app_dir}/deploy/"
  for file in pyproject.toml uv.lock README.md LICENSE; do
    install -m 0644 "${root}/${file}" "${app_dir}/${file}"
  done
fi

"${uv_bin}" sync --project "${app_dir}" --python "${python_version}" --extra controller --locked

"${app_dir}/.venv/bin/python" - \
  "${unit_dir}" "${root}" "${app_dir}" "${config_path}" "${HOME}" "$(id -un)" <<'PY'
import sys
from pathlib import Path

from inky_bird_frame.catalog import catalog_state_lock, rebuild_catalog_index
from inky_bird_frame.config import load_config
from inky_bird_frame.errors import ConfigurationError
from inky_bird_frame.installation import controller_systemd_units
from inky_bird_frame.publisher import sync_public_catalog


unit_dir, root, app_dir, config_path, home = map(Path, sys.argv[1:6])
user = sys.argv[6]
config = load_config(config_path)
environment_destinations = [
    destination.name
    for destination in config.notifications.destinations
    if config.notifications.enabled and destination.url_env is not None
]
if environment_destinations:
    names = ", ".join(environment_destinations)
    raise ConfigurationError(
        "The systemd installer requires direct notification URLs in the private "
        f"config file; replace url_env for: {names}"
    )
with catalog_state_lock(config.controller.state_dir):
    rebuild_catalog_index(config.controller.catalog_dir)
    sync_public_catalog(root / "catalog", config.controller.catalog_dir)

executable = app_dir / ".venv/bin/inky-bird-frame"
units = controller_systemd_units(
    config,
    executable=executable,
    app_dir=app_dir,
    config_path=config_path,
    home=home,
    user=user,
)
for name, content in units.items():
    (unit_dir / name).write_text(content)
PY

"${app_dir}/.venv/bin/inky-bird-frame" refresh --config "${config_path}"

if [ -f "${unit_dir}/inky-bird-frame-catalog-publish.timer" ]; then
  "${app_dir}/.venv/bin/inky-bird-frame" catalog-publish \
    --config "${config_path}" --dry-run
fi

for unit in "${unit_dir}"/*; do
  sudo install -m 0644 "${unit}" "/etc/systemd/system/$(basename "${unit}")"
done
for optional in catalog-publish notifications; do
  if [ ! -f "${unit_dir}/inky-bird-frame-${optional}.timer" ]; then
    sudo systemctl disable --now "inky-bird-frame-${optional}.timer" 2>/dev/null || true
    sudo rm -f \
      "/etc/systemd/system/inky-bird-frame-${optional}.service" \
      "/etc/systemd/system/inky-bird-frame-${optional}.timer"
  fi
done

sudo systemctl daemon-reload
sudo systemctl enable inky-bird-frame-controller.service
sudo systemctl enable inky-bird-frame-refresh.timer
sudo systemctl enable inky-bird-frame-generate.timer
if [ -f "${unit_dir}/inky-bird-frame-catalog-publish.timer" ]; then
  sudo systemctl enable inky-bird-frame-catalog-publish.timer
fi
if [ -f "${unit_dir}/inky-bird-frame-notifications.timer" ]; then
  sudo systemctl enable inky-bird-frame-notifications.timer
fi
sudo systemctl restart inky-bird-frame-controller.service
sudo systemctl restart inky-bird-frame-refresh.timer
sudo systemctl restart inky-bird-frame-generate.timer
if [ -f "${unit_dir}/inky-bird-frame-catalog-publish.timer" ]; then
  sudo systemctl restart inky-bird-frame-catalog-publish.timer
fi
if [ -f "${unit_dir}/inky-bird-frame-notifications.timer" ]; then
  sudo systemctl restart inky-bird-frame-notifications.timer
fi

systemctl is-enabled --quiet inky-bird-frame-controller.service
systemctl is-active --quiet inky-bird-frame-controller.service
systemctl is-enabled --quiet inky-bird-frame-refresh.timer
systemctl is-active --quiet inky-bird-frame-refresh.timer
systemctl is-enabled --quiet inky-bird-frame-generate.timer
systemctl is-active --quiet inky-bird-frame-generate.timer
if [ -f "${unit_dir}/inky-bird-frame-catalog-publish.timer" ]; then
  systemctl is-enabled --quiet inky-bird-frame-catalog-publish.timer
  systemctl is-active --quiet inky-bird-frame-catalog-publish.timer
fi
if [ -f "${unit_dir}/inky-bird-frame-notifications.timer" ]; then
  systemctl is-enabled --quiet inky-bird-frame-notifications.timer
  systemctl is-active --quiet inky-bird-frame-notifications.timer
fi

installation_complete=true
echo "Controller installed from ${root} into ${app_dir}."
echo "Configuration: ${config_path}"
