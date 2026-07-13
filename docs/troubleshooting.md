# Troubleshooting

Run the role-specific doctor first. It is read-only and continues after a failed
check so one report can expose independent problems.

```bash
inky-bird-frame doctor controller --config /path/to/config.toml
inky-bird-frame doctor display --config /path/to/config.toml
```

The command exits `0` only when no check has status `fail`. Warnings do not make
the role unready, but should still be reviewed.

## Controller

### `config` fails

Run the focused validator:

```bash
inky-bird-frame config validate --config /path/to/config.toml
```

Start from `config.example.toml`. Do not paste YAML or JSON into the TOML file.
Keep strings quoted, array items comma-separated, and each table header unique.

### `codex_executable` fails

```bash
command -v codex
codex --version
```

Put the absolute `command -v` result in `controller.codex_path`, then rerun
setup so the service definition receives the corrected path.

### `codex_auth` fails

```bash
codex login status
codex login
# Headless alternative:
codex login --device-auth
```

Authenticate as the same OS account that owns the service. Do not copy Codex
credentials into the repository, configuration file, GitHub Actions, or the
display node.

### `controller_health` fails

On macOS:

```bash
launchctl print "gui/$(id -u)/com.inky-bird-frame.serve"
tail -n 100 "$HOME/Library/Application Support/Inky Bird Frame/logs/serve.error.log"
```

On systemd Linux:

```bash
systemctl status inky-bird-frame-controller.service
journalctl -u inky-bird-frame-controller.service -n 100 --no-pager
```

Confirm that another process is not using the configured port:

```bash
ss -ltnp | grep ':8793'                    # Linux
lsof -nP -iTCP:8793 -sTCP:LISTEN           # macOS
```

### Discovery or refresh fails

Run the command interactively to get its structured error:

```bash
inky-bird-frame discover --config /path/to/config.toml
inky-bird-frame refresh --config /path/to/config.toml
```

Check DNS, outbound HTTPS, the ZIP code, and the controller clock. For eBird,
also run `config validate` and confirm that the personal API key is available.
A multi-provider refresh reports each provider independently and continues when
at least one configured provider is healthy. A refresh failure does not remove
the existing active catalog.

### Generation fails or keeps retrying

```bash
inky-bird-frame status --config /path/to/config.toml
codex login status
```

Transient source, reference, and Codex failures are deferred per species and do
not block later queue entries. Terminal factual or visual failures require an
explicit `retry TAXON_ID` after investigation. See
[`operations.md`](operations.md#failure-recovery).

## Display node

### The Pi is not reachable after imaging

Confirm that Imager enabled SSH, configured the correct Wi-Fi country and SSID,
and created the expected username. Check the router's DHCP clients. A
`hostname.local` lookup requires mDNS support; the DHCP address can be used for
administration instead.

Connect a monitor or KVM if the Pi never receives a lease. Application setup
cannot repair an OS that is not booted or associated with Wi-Fi.

### `boot_config` warns

Pimoroni's current manual configuration requires:

```text
dtparam=spi=on
dtoverlay=spi0-0cs
```

On current Raspberry Pi OS these normally live in
`/boot/firmware/config.txt`. Use Pimoroni's installer when possible. Reboot
after changing boot configuration.

### `inky_hardware` fails

Power off before reseating the Pi. Verify that all 40 pins are aligned and the
Pi is not offset by one row or column. Confirm that the Python environment is
the Pimoroni environment:

```bash
"$HOME/.virtualenvs/pimoroni/bin/python" -c \
  'from inky.auto import auto; d=auto(); print(d.width, d.height)'
```

The supported result is `1600 1200`. If import fails, rerun Pimoroni's installer
and reinstall this project's `inky` extra into that same environment.

### The included image is rotated incorrectly

Use the committed `display.png`, not `portrait.png`:

```bash
"$HOME/.virtualenvs/pimoroni/bin/inky-bird-frame" display-image \
  catalog/species/12942-eastern-bluebird/display.png
```

The catalog's display asset is already rotated left for a portrait-mounted
PIM774. Do not add a second OS-level rotation.

### The Pi reaches Wi-Fi but not the controller

```bash
ip route
getent hosts YOUR_CONTROLLER
curl --fail --verbose "http://YOUR_CONTROLLER:8793/health"
```

Check the controller URL, firewall, VLAN routing, and wireless client isolation.
Only the controller needs a stable application address. Do not disable Ethernet;
it remains a useful recovery path when connected.

### The timer is active but the image does not change

```bash
systemctl status inky-bird-frame-display.timer
systemctl status inky-bird-frame-display.service
journalctl -u inky-bird-frame-display.service -n 100 --no-pager
```

Force one foreground-equivalent cycle through systemd:

```bash
sudo systemctl start inky-bird-frame-display.service
```

If the controller has no active approved species, its health response reports
`active_species: 0`. Generate and refresh on the controller, then retry. A failed
download, checksum, or panel refresh leaves the previous e-paper image and
selection state unchanged.

## Service reinstall

After correcting configuration or paths, rerun setup. It is designed to
converge an existing installation rather than create duplicate services.

```bash
inky-bird-frame setup controller --config /path/to/config.toml --yes
inky-bird-frame setup display --config /path/to/config.toml \
  --source-dir /path/to/inky-bird-frame \
  --venv "$HOME/.virtualenvs/pimoroni" --yes
```

Use only the command for the role on that machine.
