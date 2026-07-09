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
uv_bin=${UV_BIN:-/opt/homebrew/bin/uv}
log_dir="${support_dir}/logs"
agents_dir="${HOME}/Library/LaunchAgents"
serve_plist="${agents_dir}/com.inky-bird-frame.serve.plist"
cycle_plist="${agents_dir}/com.inky-bird-frame.controller-cycle.plist"

if [ ! -f "${config_path}" ]; then
  echo "Controller configuration is missing: ${config_path}" >&2
  exit 1
fi
if [ ! -x "${uv_bin}" ]; then
  echo "uv is not executable: ${uv_bin}" >&2
  exit 1
fi

mkdir -p "${app_dir}" "${app_dir}/catalog" "${support_dir}" "${log_dir}" "${agents_dir}"
rsync -a --delete "${root}/src/" "${app_dir}/src/"
rsync -a "${root}/catalog/" "${app_dir}/catalog/"
for file in pyproject.toml uv.lock README.md LICENSE; do
  install -m 0644 "${root}/${file}" "${app_dir}/${file}"
done

"${uv_bin}" sync --project "${app_dir}" --locked

/usr/bin/python3 - "${serve_plist}" "${cycle_plist}" "${app_dir}" "${config_path}" "${log_dir}" <<'PY'
import plistlib
import sys
from pathlib import Path

serve_path, cycle_path, app_dir, config_path, log_dir = map(Path, sys.argv[1:])
executable = app_dir / ".venv/bin/inky-bird-frame"

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
cycle = {
    **common,
    "Label": "com.inky-bird-frame.controller-cycle",
    "ProgramArguments": [str(executable), "controller-cycle", "--config", str(config_path)],
    "StartInterval": 21600,
    "StandardOutPath": str(log_dir / "controller-cycle.log"),
    "StandardErrorPath": str(log_dir / "controller-cycle.error.log"),
}
for path, payload in ((serve_path, serve), (cycle_path, cycle)):
    with path.open("wb") as handle:
        plistlib.dump(payload, handle, sort_keys=True)
PY

uid=$(id -u)
launchctl bootout "gui/${uid}/com.inky-bird-frame.serve" 2>/dev/null || true
launchctl bootout "gui/${uid}/com.inky-bird-frame.controller-cycle" 2>/dev/null || true
launchctl bootstrap "gui/${uid}" "${serve_plist}"
launchctl bootstrap "gui/${uid}" "${cycle_plist}"
launchctl print "gui/${uid}/com.inky-bird-frame.serve" >/dev/null
launchctl print "gui/${uid}/com.inky-bird-frame.controller-cycle" >/dev/null

echo "Controller installed from ${root} into ${app_dir}."
echo "Configuration: ${config_path}"
echo "Logs: ${log_dir}"
