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
scheduler_plist="${agents_dir}/com.inky-bird-frame.scheduler.plist"
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
  if [ ! -f "${backup}" ]; then
    return 0
  fi
  # A loaded label here is the failed replacement; put the saved agent back.
  launchctl bootout "gui/${uid}/com.inky-bird-frame.${label}" 2>/dev/null || true
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
chmod 700 "${support_dir}" "${log_dir}"
find "${log_dir}" -maxdepth 1 -type f -name '*.log*' -exec chmod 600 {} +
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
was_previously_loaded() {
  local target=$1
  local loaded
  if [ "${#previously_loaded_labels[@]}" -gt 0 ]; then
    for loaded in "${previously_loaded_labels[@]}"; do
      if [ "${loaded}" = "${target}" ]; then
        return 0
      fi
    done
  fi
  return 1
}

cleanup() {
  status=$?
  trap - EXIT
  if [ "${installation_complete}" != true ]; then
    for label in serve refresh generate catalog-publish notifications scheduler; do
      if was_previously_loaded "${label}"; then
        restore_previous_agent "${label}"
      else
        # Not running before this install: undo any half-installed agent.
        launchctl bootout "gui/${uid}/com.inky-bird-frame.${label}" 2>/dev/null || true
        if [ -f "${plist_backup_dir}/com.inky-bird-frame.${label}.plist" ]; then
          cp "${plist_backup_dir}/com.inky-bird-frame.${label}.plist" \
            "${agents_dir}/com.inky-bird-frame.${label}.plist" || true
        else
          rm -f "${agents_dir}/com.inky-bird-frame.${label}.plist"
        fi
      fi
    done
  fi
  rm -rf "${plist_backup_dir}"
  exit "${status}"
}
trap cleanup EXIT

for label in serve refresh generate catalog-publish notifications scheduler; do
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
  "${notifications_plist}" "${scheduler_plist}" \
  "${root}" "${app_dir}" "${config_path}" "${log_dir}" <<'PY'
import plistlib
import sys
from pathlib import Path

from inky_bird_frame.catalog import catalog_state_lock
from inky_bird_frame.config import DiscoveryProvider, load_config
from inky_bird_frame.errors import ConfigurationError
from inky_bird_frame.http import write_bytes_atomic
from inky_bird_frame.installation import controller_executable_path, native_oidc_environment
from inky_bird_frame.publisher import sync_public_catalog

(
    serve_path,
    refresh_path,
    generation_path,
    catalog_publish_path,
    notifications_path,
    scheduler_path,
    root,
    app_dir,
    config_path,
    log_dir,
) = map(Path, sys.argv[1:])
executable = app_dir / ".venv/bin/inky-bird-frame"
config = load_config(config_path)
environment_credentials = []
if config.discovery.geoapify_api_key_env:
    environment_credentials.append("geoapify_api_key_env")
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
owner_environment = native_oidc_environment(config, config_path)
schedule = config.schedule
config.controller.workspace_dir.mkdir(parents=True, exist_ok=True)
config.controller.catalog_dir.parent.mkdir(parents=True, exist_ok=True)
with catalog_state_lock(config.controller.state_dir):
    source_catalog = root / "catalog"
    managed_catalog = app_dir / "catalog"
    if root != app_dir and managed_catalog.resolve() != config.controller.catalog_dir.resolve():
        sync_public_catalog(
            source_catalog,
            managed_catalog,
            allow_replacements=True,
            retain_independent_approvals=True,
        )
        source_catalog = managed_catalog
    sync_public_catalog(
        source_catalog,
        config.controller.catalog_dir,
        allow_replacements=True,
        retain_independent_approvals=True,
    )

common = {
    "WorkingDirectory": str(app_dir),
    "ProcessType": "Background",
    "Umask": 0o077,
}


def managed_agent(name: str) -> dict[str, object]:
    return {
        **common,
        "EnvironmentVariables": {
            "PATH": controller_executable_path(
                config.controller.codex_path,
                generation_enabled=config.controller.generation_enabled,
            ),
            "PYTHONUNBUFFERED": "1",
            "INKY_BIRD_MANAGED_LOG_PATH": str(log_dir / f"{name}.log"),
            **(owner_environment if name == "serve" else {}),
        },
    }


serve = {
    **managed_agent("serve"),
    "Label": "com.inky-bird-frame.serve",
    "ProgramArguments": [str(executable), "serve", "--config", str(config_path)],
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ThrottleInterval": 10,
    "StandardOutPath": str(log_dir / "serve.log"),
    "StandardErrorPath": str(log_dir / "serve.error.log"),
}
refresh = {
    **managed_agent("refresh"),
    "Label": "com.inky-bird-frame.refresh",
    "ProgramArguments": [str(executable), "refresh", "--config", str(config_path)],
    "RunAtLoad": True,
    "StartInterval": schedule.refresh_minutes * 60,
    "StandardOutPath": str(log_dir / "refresh.log"),
    "StandardErrorPath": str(log_dir / "refresh.error.log"),
}
generation = {
    **managed_agent("generate"),
    "Label": "com.inky-bird-frame.generate",
    "ProgramArguments": [str(executable), "generate", "--config", str(config_path)],
    "StartInterval": schedule.generation_minutes * 60,
    "StandardOutPath": str(log_dir / "generate.log"),
    "StandardErrorPath": str(log_dir / "generate.error.log"),
}
catalog_publish = {
    **managed_agent("catalog-publish"),
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
    **managed_agent("notifications"),
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
):
    write_bytes_atomic(path, plistlib.dumps(payload, sort_keys=True), mode=0o600)
if config.controller.generation_enabled:
    write_bytes_atomic(generation_path, plistlib.dumps(generation, sort_keys=True), mode=0o600)
else:
    generation_path.unlink(missing_ok=True)
if config.public_catalog.enabled:
    write_bytes_atomic(catalog_publish_path, plistlib.dumps(catalog_publish, sort_keys=True), mode=0o600)
else:
    catalog_publish_path.unlink(missing_ok=True)
if config.notifications.enabled:
    write_bytes_atomic(notifications_path, plistlib.dumps(notifications, sort_keys=True), mode=0o600)
else:
    notifications_path.unlink(missing_ok=True)
if config.controller.configuration_ownership == "app":
    scheduler = {
        **managed_agent("scheduler"),
        "Label": "com.inky-bird-frame.scheduler",
        "ProgramArguments": [str(executable), "scheduler", "--config", str(config_path)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_dir / "scheduler.log"),
        "StandardErrorPath": str(log_dir / "scheduler.error.log"),
    }
    write_bytes_atomic(scheduler_path, plistlib.dumps(scheduler, sort_keys=True), mode=0o600)
    for path in (refresh_path, generation_path, catalog_publish_path, notifications_path):
        path.unlink(missing_ok=True)
else:
    scheduler_path.unlink(missing_ok=True)
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
launchctl bootout "gui/${uid}/com.inky-bird-frame.refresh" 2>/dev/null || true
launchctl bootout "gui/${uid}/com.inky-bird-frame.generate" 2>/dev/null || true
launchctl bootout "gui/${uid}/com.inky-bird-frame.catalog-publish" 2>/dev/null || true
launchctl bootout "gui/${uid}/com.inky-bird-frame.notifications" 2>/dev/null || true
launchctl bootout "gui/${uid}/com.inky-bird-frame.scheduler" 2>/dev/null || true
launchctl bootstrap "gui/${uid}" "${serve_plist}"
if [ -f "${refresh_plist}" ]; then
  launchctl bootstrap "gui/${uid}" "${refresh_plist}"
fi
if [ -f "${scheduler_plist}" ]; then
  launchctl bootstrap "gui/${uid}" "${scheduler_plist}"
fi
if [ -f "${generation_plist}" ]; then
  launchctl bootstrap "gui/${uid}" "${generation_plist}"
fi
if [ -f "${catalog_publish_plist}" ]; then
  launchctl bootstrap "gui/${uid}" "${catalog_publish_plist}"
fi
if [ -f "${notifications_plist}" ]; then
  launchctl bootstrap "gui/${uid}" "${notifications_plist}"
fi
launchctl print "gui/${uid}/com.inky-bird-frame.serve" >/dev/null
if [ -f "${refresh_plist}" ]; then
  launchctl print "gui/${uid}/com.inky-bird-frame.refresh" >/dev/null
fi
if [ -f "${scheduler_plist}" ]; then
  launchctl print "gui/${uid}/com.inky-bird-frame.scheduler" >/dev/null
fi
if [ -f "${generation_plist}" ]; then
  launchctl print "gui/${uid}/com.inky-bird-frame.generate" >/dev/null
fi
if [ -f "${catalog_publish_plist}" ]; then
  launchctl print "gui/${uid}/com.inky-bird-frame.catalog-publish" >/dev/null
fi
if [ -f "${notifications_plist}" ]; then
  launchctl print "gui/${uid}/com.inky-bird-frame.notifications" >/dev/null
fi

installation_complete=true
# Remove the legacy single-agent layout only after the new agents verified,
# so a failed upgrade leaves the previous controller runnable.
launchctl bootout "gui/${uid}/com.inky-bird-frame.controller-cycle" 2>/dev/null || true
rm -f "${legacy_cycle_plist}"
echo "Controller installed from ${root} into ${app_dir}."
echo "Configuration: ${config_path}"
echo "Logs: ${log_dir}"
if ! "${app_dir}/.venv/bin/inky-bird-frame" access setup --if-needed --config "${config_path}"; then
  echo "Controller installation completed; owner setup is incomplete. Run access setup in a private terminal. Display and scheduled work continue." >&2
fi
