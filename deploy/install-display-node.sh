#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
display_host=${INKY_BIRD_DISPLAY_HOST:?Set INKY_BIRD_DISPLAY_HOST to the display node address}
display_user=${INKY_BIRD_DISPLAY_USER:?Set INKY_BIRD_DISPLAY_USER to the SSH user}
ssh_key=${INKY_BIRD_DISPLAY_SSH_KEY:?Set INKY_BIRD_DISPLAY_SSH_KEY to the SSH private key path}
remote_app=${INKY_BIRD_DISPLAY_APP_DIR:?Set INKY_BIRD_DISPLAY_APP_DIR to the remote application directory}
remote_config=${INKY_BIRD_DISPLAY_CONFIG_PATH:?Set INKY_BIRD_DISPLAY_CONFIG_PATH to the remote config path}
remote_venv=${INKY_BIRD_DISPLAY_VENV:?Set INKY_BIRD_DISPLAY_VENV to the remote Python environment}
remote="${display_user}@${display_host}"
ssh_options=(-i "${ssh_key}" -o BatchMode=yes -o IdentitiesOnly=yes)

if [ ! -f "${ssh_key}" ]; then
  echo "Display deployment key is missing: ${ssh_key}" >&2
  exit 1
fi

ssh "${ssh_options[@]}" "${remote}" bash -s -- "${remote_app}" <<'REMOTE'
set -euo pipefail
mkdir -p "$1/src" "$1/catalog" "$1/deploy"
REMOTE
rsync -a --delete -e "ssh -i '${ssh_key}' -o BatchMode=yes -o IdentitiesOnly=yes" \
  "${root}/src/" "${remote}:${remote_app}/src/"
rsync -a -e "ssh -i '${ssh_key}' -o BatchMode=yes -o IdentitiesOnly=yes" \
  "${root}/catalog/" "${remote}:${remote_app}/catalog/"
for file in pyproject.toml uv.lock README.md LICENSE; do
  rsync -a -e "ssh -i '${ssh_key}' -o BatchMode=yes -o IdentitiesOnly=yes" \
    "${root}/${file}" "${remote}:${remote_app}/${file}"
done
rsync -a -e "ssh -i '${ssh_key}' -o BatchMode=yes -o IdentitiesOnly=yes" \
  "${root}/deploy/install-display-local.sh" "${remote}:${remote_app}/deploy/"

ssh "${ssh_options[@]}" "${remote}" bash -s -- \
  "${remote_app}" "${remote_config}" "${remote_venv}" <<'REMOTE'
set -euo pipefail
app_dir=$1
config_path=$2
venv=$3
if ! sudo -n true; then
  echo "Passwordless sudo is required for remote display deployment." >&2
  exit 1
fi
INKY_BIRD_APP_DIR="${app_dir}" \
INKY_BIRD_CONFIG_PATH="${config_path}" \
INKY_BIRD_DISPLAY_VENV="${venv}" \
INKY_BIRD_RUN_INITIAL_DISPLAY=true \
INKY_BIRD_NONINTERACTIVE_SUDO=true \
  "${app_dir}/deploy/install-display-local.sh"
REMOTE

echo "Display node deployed to ${remote}."
