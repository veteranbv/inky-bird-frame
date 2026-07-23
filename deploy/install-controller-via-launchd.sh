#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "The launchd controller installer requires macOS." >&2
  exit 1
fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
installer="${root}/deploy/install-controller.sh"
uv_bin=${UV_BIN:-}
if [ -z "${uv_bin}" ]; then
  uv_bin=$(command -v uv || true)
fi
if [ -z "${uv_bin}" ] || [ ! -x "${uv_bin}" ]; then
  echo "uv is not executable. Install uv or set UV_BIN." >&2
  exit 1
fi

timeout_seconds=${INKY_BIRD_LAUNCHD_INSTALL_TIMEOUT_SECONDS:-1800}
case "${timeout_seconds}" in
  '' | *[!0-9]* | 0)
    echo "INKY_BIRD_LAUNCHD_INSTALL_TIMEOUT_SECONDS must be a positive integer." >&2
    exit 1
    ;;
esac

uid=$(id -u)
temporary_root=${TMPDIR:-/tmp}
temporary_root=${temporary_root%/}
temporary_dir=$(mktemp -d "${temporary_root}/inky-bird-frame-install.XXXXXX")
plist_path="${temporary_dir}/install.plist"
stdout_path="${temporary_dir}/stdout.log"
stderr_path="${temporary_dir}/stderr.log"
status_path="${temporary_dir}/status"
label="com.inky-bird-frame.install.${uid}.$$"

# shellcheck disable=SC2329 # Invoked indirectly by the EXIT trap below.
cleanup() {
  status=$?
  trap - EXIT
  launchctl bootout "gui/${uid}/${label}" 2>/dev/null || true
  rm -f "${plist_path}" "${stdout_path}" "${stderr_path}" "${status_path}"
  rmdir "${temporary_dir}" 2>/dev/null || true
  exit "${status}"
}
trap cleanup EXIT

touch "${stdout_path}" "${stderr_path}"
/usr/bin/python3 - \
  "${plist_path}" "${label}" "${root}" "${HOME}" "${stdout_path}" "${stderr_path}" \
  "${status_path}" "${uv_bin}" "${installer}" <<'PY'
import plistlib
import sys
from pathlib import Path

(
    plist_path,
    label,
    working_directory,
    home,
    stdout_path,
    stderr_path,
    status_path,
    uv_bin,
    installer,
) = sys.argv[1:]
payload = {
    "Label": label,
    "ProgramArguments": [
        "/bin/bash",
        "-c",
        'UV_BIN="$1" "$2"; child_status=$?; '
        'printf "%s\\n" "$child_status" > "$3"; exit "$child_status"',
        "_",
        uv_bin,
        installer,
        status_path,
    ],
    "WorkingDirectory": working_directory,
    "EnvironmentVariables": {
        "HOME": home,
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
    },
    "ProcessType": "Background",
    "RunAtLoad": True,
    "StandardOutPath": stdout_path,
    "StandardErrorPath": stderr_path,
}
with Path(plist_path).open("wb") as handle:
    plistlib.dump(payload, handle, sort_keys=True)
PY

launchctl bootstrap "gui/${uid}" "${plist_path}"
deadline=$((SECONDS + timeout_seconds))
while [ ! -f "${status_path}" ]; do
  if [ "${SECONDS}" -ge "${deadline}" ]; then
    cat "${stdout_path}"
    cat "${stderr_path}" >&2
    echo "Controller installation timed out after ${timeout_seconds} seconds." >&2
    exit 124
  fi
  if ! launchctl print "gui/${uid}/${label}" >/dev/null 2>&1; then
    cat "${stdout_path}"
    cat "${stderr_path}" >&2
    echo "Controller installation stopped without reporting an exit status." >&2
    exit 1
  fi
  sleep 1
done

# The status file is written immediately before the child exits. Give launchd a
# short bounded window to flush the configured stdout/stderr files.
for _ in {1..20}; do
  if ! launchctl print "gui/${uid}/${label}" 2>/dev/null | grep -q 'state = running'; then
    break
  fi
  sleep 0.1
done

cat "${stdout_path}"
cat "${stderr_path}" >&2
child_status=$(tr -d '[:space:]' < "${status_path}")
case "${child_status}" in
  '' | *[!0-9]*)
    echo "Controller installation returned an invalid exit status." >&2
    exit 1
    ;;
esac
exit "${child_status}"
