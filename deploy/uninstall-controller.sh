#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "Controller uninstaller currently supports macOS." >&2
  exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
native_names=$(python3 "${root}/src/inky_bird_frame/native_names.py")
service_prefix="" app_dir="" support_dir="" config_path=""
eval "${native_names}"
agents_dir="${HOME}/Library/LaunchAgents"
uid=$(id -u)

# controller-cycle is the legacy single-agent layout replaced by the labels below.
for label in serve refresh generate catalog-publish notifications scheduler controller-cycle; do
  agent="com.${service_prefix}.${label}"
  plist="${agents_dir}/${agent}.plist"
  if launchctl print "gui/${uid}/${agent}" >/dev/null 2>&1; then
    launchctl bootout "gui/${uid}/${agent}"
    echo "Booted out LaunchAgent: ${agent}"
  else
    echo "LaunchAgent is not loaded: ${agent}"
  fi
  if [ -f "${plist}" ]; then
    rm -f "${plist}"
    echo "Removed plist: ${plist}"
  else
    echo "Plist is not installed: ${plist}"
  fi
done

echo "Controller LaunchAgents uninstalled."
echo "Intentionally left in place:"
echo "  Application runtime and managed catalog: ${app_dir}"
echo "  Configuration: ${config_path}"
echo "  Logs and support data: ${support_dir}"
echo "  Workspace, state, and catalog directories configured in ${config_path}"
echo "Remove those paths manually if you no longer need the controller data."
