#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Controller installer currently supports macOS." >&2
  exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
app_dir=${INKY_BIRD_APP_DIR:-"${HOME}/Services/inky-bird-frame"}
support_dir=${INKY_BIRD_SUPPORT_DIR:-"${HOME}/Library/Application Support/Inky Bird Frame"}
config_path=${INKY_BIRD_CONFIG_PATH:-"${support_dir}/config.toml"}
uv_bin=${UV_BIN:-}
if [ -z "${uv_bin}" ]; then
  uv_bin=$(command -v uv || true)
fi
python_version=${INKY_BIRD_PYTHON_VERSION:-3.11}
log_dir="${support_dir}/logs"
agents_dir="${HOME}/Library/LaunchAgents"
serve_plist="${agents_dir}/com.inky-bird-frame.serve.plist"
refresh_plist="${agents_dir}/com.inky-bird-frame.refresh.plist"
generation_plist="${agents_dir}/com.inky-bird-frame.generate.plist"
catalog_publish_plist="${agents_dir}/com.inky-bird-frame.catalog-publish.plist"
notifications_plist="${agents_dir}/com.inky-bird-frame.notifications.plist"
legacy_cycle_plist="${agents_dir}/com.inky-bird-frame.controller-cycle.plist"

restore_catalog_publisher_schedule() {
  local uid=$1
  local recovery_dir
  local recovery_plist
  recovery_dir=$(mktemp -d "${support_dir}/catalog-publish-recovery.XXXXXX")
  recovery_plist="${recovery_dir}/catalog-publish.plist"
  if ! cp "${catalog_publish_plist}" "${recovery_plist}"; then
    rmdir "${recovery_dir}"
    return 1
  fi
  if ! /usr/bin/plutil -replace RunAtLoad -bool false "${recovery_plist}"; then
    rm -f "${recovery_plist}"
    rmdir "${recovery_dir}"
    return 1
  fi
  if ! launchctl bootstrap "gui/${uid}" "${recovery_plist}"; then
    rm -f "${recovery_plist}"
    rmdir "${recovery_dir}"
    return 1
  fi
  rm -f "${recovery_plist}"
  rmdir "${recovery_dir}"
}

restore_previous_agent() {
  local label=$1
  local plist="${agents_dir}/com.inky-bird-frame.${label}.plist"
  local backup="${plist_backup_dir}/com.inky-bird-frame.${label}.plist"
  if launchctl print "gui/${uid}/com.inky-bird-frame.${label}" >/dev/null 2>&1; then
    return 0
  fi
  if [ ! -f "${backup}" ]; then
    return 0
  fi
  cp "${backup}" "${plist}" || return 0
  if [ "${label}" = "catalog-publish" ]; then
    restore_catalog_publisher_schedule "${uid}" || true
  else
    launchctl bootstrap "gui/${uid}" "${plist}" || true
  fi
}

if [ ! -f "${config_path}" ]; then
  echo "Controller configuration is missing: ${config_path}" >&2
  exit 1
fi
chmod 600 "${config_path}"
if [ -z "${uv_bin}" ] || [ ! -x "${uv_bin}" ]; then
  echo "uv is not executable. Install uv or set UV_BIN." >&2
  exit 1
fi

mkdir -p "${app_dir}/catalog" "${app_dir}/deploy" "${support_dir}" "${log_dir}" "${agents_dir}"
if [ "${root}" != "${app_dir}" ]; then
  rsync -a --delete "${root}/src/" "${app_dir}/src/"
  install -m 0755 "${root}/deploy/install-controller.sh" "${app_dir}/deploy/"
  for file in pyproject.toml uv.lock README.md LICENSE; do
    install -m 0644 "${root}/${file}" "${app_dir}/${file}"
  done
fi

"${uv_bin}" sync --project "${app_dir}" --python "${python_version}" --extra controller --locked

uid=$(id -u)
plist_backup_dir=$(mktemp -d "${support_dir}/launchd-restore.XXXXXX")
previously_loaded_labels=()
installation_complete=false
cleanup() {
  status=$?
  trap - EXIT
  if [ "${installation_complete}" != true ] \
    && [ "${#previously_loaded_labels[@]}" -gt 0 ]; then
    for label in "${previously_loaded_labels[@]}"; do
      restore_previous_agent "${label}"
    done
  fi
  rm -rf "${plist_backup_dir}"
  exit "${status}"
}
trap cleanup EXIT

for label in serve refresh generate catalog-publish notifications; do
  if launchctl print "gui/${uid}/com.inky-bird-frame.${label}" >/dev/null 2>&1; then
    previously_loaded_labels+=("${label}")
  fi
  plist="${agents_dir}/com.inky-bird-frame.${label}.plist"
  if [ -f "${plist}" ]; then
    cp "${plist}" "${plist_backup_dir}/"
  fi
done

"${app_dir}/.venv/bin/python" - \
  "${serve_plist}" "${refresh_plist}" "${generation_plist}" "${catalog_publish_plist}" \
  "${notifications_plist}" \
  "${root}" "${app_dir}" "${config_path}" "${log_dir}" <<'PY'
import plistlib
import sys
from pathlib import Path

from inky_bird_frame.catalog import catalog_state_lock
from inky_bird_frame.config import DiscoveryProvider, load_config
from inky_bird_frame.errors import ConfigurationError
from inky_bird_frame.publisher import sync_public_catalog

(
    serve_path,
    refresh_path,
    generation_path,
    catalog_publish_path,
    notifications_path,
    root,
    app_dir,
    config_path,
    log_dir,
) = map(Path, sys.argv[1:])
executable = app_dir / ".venv/bin/inky-bird-frame"
config = load_config(config_path)
environment_credentials = []
if DiscoveryProvider.EBIRD in config.discovery.sources and config.discovery.ebird_api_key_env:
    environment_credentials.append("ebird_api_key_env")
if (
    DiscoveryProvider.BIRDWEATHER in config.discovery.sources
    and config.discovery.birdweather_token_env
):
    environment_credentials.append("birdweather_token_env")
if environment_credentials:
    names = ", ".join(environment_credentials)
    raise ConfigurationError(
        f"The macOS LaunchAgent installer cannot use {names}; store the corresponding "
        "credential directly in the private config file"
    )
environment_destinations = [
    destination.name
    for destination in config.notifications.destinations
    if config.notifications.enabled and destination.url_env is not None
]
if environment_destinations:
    names = ", ".join(environment_destinations)
    raise ConfigurationError(
        "The macOS LaunchAgent installer requires direct notification URLs in the "
        f"private config file; replace url_env for: {names}"
    )
schedule = config.schedule
config.controller.workspace_dir.mkdir(parents=True, exist_ok=True)
config.controller.catalog_dir.parent.mkdir(parents=True, exist_ok=True)
with catalog_state_lock(config.controller.state_dir):
    source_catalog = root / "catalog"
    managed_catalog = app_dir / "catalog"
    if root != app_dir and managed_catalog.resolve() != config.controller.catalog_dir.resolve():
        sync_public_catalog(source_catalog, managed_catalog)
        source_catalog = managed_catalog
    sync_public_catalog(source_catalog, config.controller.catalog_dir)

common = {
    "WorkingDirectory": str(app_dir),
    "ProcessType": "Background",
    "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
}
serve = {
    **common,
    "Label": "com.inky-bird-frame.serve",
    "ProgramArguments": [str(executable), "serve", "--config", str(config_path)],
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ThrottleInterval": 10,
    "StandardOutPath": str(log_dir / "serve.log"),
    "StandardErrorPath": str(log_dir / "serve.error.log"),
}
refresh = {
    **common,
    "Label": "com.inky-bird-frame.refresh",
    "ProgramArguments": [str(executable), "refresh", "--config", str(config_path)],
    "RunAtLoad": True,
    "StartInterval": schedule.refresh_minutes * 60,
    "StandardOutPath": str(log_dir / "refresh.log"),
    "StandardErrorPath": str(log_dir / "refresh.error.log"),
}
generation = {
    **common,
    "Label": "com.inky-bird-frame.generate",
    "ProgramArguments": [str(executable), "generate", "--config", str(config_path)],
    "StartInterval": schedule.generation_minutes * 60,
    "StandardOutPath": str(log_dir / "generate.log"),
    "StandardErrorPath": str(log_dir / "generate.error.log"),
}
catalog_publish = {
    **common,
    "Label": "com.inky-bird-frame.catalog-publish",
    "ProgramArguments": [
        str(executable),
        "catalog-publish",
        "--config",
        str(config_path),
    ],
    "RunAtLoad": True,
    "StartInterval": schedule.catalog_publish_minutes * 60,
    "StandardOutPath": str(log_dir / "catalog-publish.log"),
    "StandardErrorPath": str(log_dir / "catalog-publish.error.log"),
}
notifications = {
    **common,
    "Label": "com.inky-bird-frame.notifications",
    "ProgramArguments": [
        str(executable),
        "notifications",
        "dispatch",
        "--config",
        str(config_path),
    ],
    "RunAtLoad": True,
    "StartInterval": config.notifications.delivery_retry_minutes * 60,
    "StandardOutPath": str(log_dir / "notifications.log"),
    "StandardErrorPath": str(log_dir / "notifications.error.log"),
}
for path, payload in (
    (serve_path, serve),
    (refresh_path, refresh),
    (generation_path, generation),
):
    with path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)
if config.public_catalog.enabled:
    with catalog_publish_path.open("wb") as handle:
        plistlib.dump(catalog_publish, handle, sort_keys=True)
else:
    catalog_publish_path.unlink(missing_ok=True)
if config.notifications.enabled:
    with notifications_path.open("wb") as handle:
        plistlib.dump(notifications, handle, sort_keys=True)
else:
    notifications_path.unlink(missing_ok=True)
PY

if [ -f "${catalog_publish_plist}" ]; then
  publisher_was_loaded=false
  if launchctl print "gui/${uid}/com.inky-bird-frame.catalog-publish" >/dev/null 2>&1; then
    launchctl bootout "gui/${uid}/com.inky-bird-frame.catalog-publish"
    publisher_was_loaded=true
  fi
  if ! "${app_dir}/.venv/bin/inky-bird-frame" catalog-publish \
    --config "${config_path}" --dry-run; then
    if [ "${publisher_was_loaded}" = true ]; then
      restore_catalog_publisher_schedule "${uid}"
    fi
    exit 1
  fi
fi

launchctl bootout "gui/${uid}/com.inky-bird-frame.serve" 2>/dev/null || true
launchctl bootout "gui/${uid}/com.inky-bird-frame.controller-cycle" 2>/dev/null || true
launchctl bootout "gui/${uid}/com.inky-bird-frame.refresh" 2>/dev/null || true
launchctl bootout "gui/${uid}/com.inky-bird-frame.generate" 2>/dev/null || true
launchctl bootout "gui/${uid}/com.inky-bird-frame.catalog-publish" 2>/dev/null || true
launchctl bootout "gui/${uid}/com.inky-bird-frame.notifications" 2>/dev/null || true
rm -f "${legacy_cycle_plist}"
launchctl bootstrap "gui/${uid}" "${serve_plist}"
launchctl bootstrap "gui/${uid}" "${refresh_plist}"
launchctl bootstrap "gui/${uid}" "${generation_plist}"
if [ -f "${catalog_publish_plist}" ]; then
  launchctl bootstrap "gui/${uid}" "${catalog_publish_plist}"
fi
if [ -f "${notifications_plist}" ]; then
  launchctl bootstrap "gui/${uid}" "${notifications_plist}"
fi
launchctl print "gui/${uid}/com.inky-bird-frame.serve" >/dev/null
launchctl print "gui/${uid}/com.inky-bird-frame.refresh" >/dev/null
launchctl print "gui/${uid}/com.inky-bird-frame.generate" >/dev/null
if [ -f "${catalog_publish_plist}" ]; then
  launchctl print "gui/${uid}/com.inky-bird-frame.catalog-publish" >/dev/null
fi
if [ -f "${notifications_plist}" ]; then
  launchctl print "gui/${uid}/com.inky-bird-frame.notifications" >/dev/null
fi

installation_complete=true
echo "Controller installed from ${root} into ${app_dir}."
echo "Configuration: ${config_path}"
echo "Logs: ${log_dir}"
