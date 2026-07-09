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
mkdir -p "$1/src" "$1/catalog"
REMOTE
rsync -a --delete -e "ssh -i '${ssh_key}' -o BatchMode=yes -o IdentitiesOnly=yes" \
  "${root}/src/" "${remote}:${remote_app}/src/"
rsync -a -e "ssh -i '${ssh_key}' -o BatchMode=yes -o IdentitiesOnly=yes" \
  "${root}/catalog/" "${remote}:${remote_app}/catalog/"
for file in pyproject.toml uv.lock README.md LICENSE; do
  rsync -a -e "ssh -i '${ssh_key}' -o BatchMode=yes -o IdentitiesOnly=yes" \
    "${root}/${file}" "${remote}:${remote_app}/${file}"
done

ssh "${ssh_options[@]}" "${remote}" bash -s -- \
  "${remote_app}" "${remote_config}" "${remote_venv}" <<'REMOTE'
set -euo pipefail
app_dir=$1
config_path=$2
venv=$3

if [ ! -f "${config_path}" ]; then
  echo "Display-node configuration is missing: ${config_path}" >&2
  exit 1
fi
if [ ! -x "${venv}/bin/python" ]; then
  echo "Pimoroni Python environment is missing: ${venv}" >&2
  exit 1
fi
if ! sudo -n true; then
  echo "Passwordless sudo is required to install the display service." >&2
  exit 1
fi

"${venv}/bin/python" -m pip install --disable-pip-version-check -e "${app_dir}"

sudo tee /etc/systemd/system/inky-bird-frame-display.service >/dev/null <<UNIT
[Unit]
Description=Rotate the next approved Inky Bird Frame plate
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$(id -un)
WorkingDirectory=${app_dir}
Environment=PYTHONUNBUFFERED=1
ExecStart=${venv}/bin/inky-bird-frame display-cycle --config ${config_path}
TimeoutStartSec=15min
UNIT

sudo tee /etc/systemd/system/inky-bird-frame-display.timer >/dev/null <<'UNIT'
[Unit]
Description=Rotate the Inky Bird Frame every 30 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
RandomizedDelaySec=2min
Persistent=true
Unit=inky-bird-frame-display.service

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now inky-bird-frame-display.timer
sudo systemctl start inky-bird-frame-display.service
REMOTE

echo "Display node deployed to ${remote}."
