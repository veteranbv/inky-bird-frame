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

Check DNS, outbound HTTPS, the configured location, and the controller clock.
For eBird, also run `config validate` and confirm that the personal API key is
available. For Geoapify, distinguish an invalid key from an exact-match or
multiple-match rejection; correct the country/postal format or use direct
coordinates rather than guessing between results.
A multi-provider refresh reports each provider independently and continues when
at least one configured provider is healthy. A refresh failure does not remove
the existing active catalog.

### A source shows a bird, but the frame does not change

Start at the controller. `discover` queries the configured providers
interactively, `refresh` saves the result and rebuilds the active catalog, and
`status` shows the approved catalog and any retained generation work:

```bash
inky-bird-frame discover --config /path/to/config.toml
inky-bird-frame refresh --config /path/to/config.toml
inky-bird-frame status --config /path/to/config.toml
```

If `discover` or `refresh` returns `"ok": false`, start with its top-level
`error`. When every configured provider fails, the command stops before it can
return a `providers` array. A successful discovery result may still report one
failed provider. In that case, find the named source in `providers` and read its
`error` field before changing configuration. `status` reports catalog and
generation state; it does not query providers.

A BirdWeather status of `ok` means the station request and taxonomy step
completed without a provider-level error. `species_count` is the number of
distinct avian species matched to the catalog's iNaturalist taxonomy, not the
number of recordings. `unresolved_count` reports otherwise usable station
species that could not be matched. Common error causes include the station
token, outbound HTTPS, the controller clock, a BirdWeather outage, iNaturalist
availability, or a taxonomy mismatch.

The station API returns one row per species, not one row per recording.
Detection counts use the configured time window, and the row count is capped
by `species_limit` and BirdWeather's 100-species maximum. Species are also
deduplicated against the other discovery providers. Repeated detections update
one species record; they do not create more plates. If a detected species is
absent from `approved`, it still needs a plate before it can appear. Check
the nested `generation` object before forcing a run. It includes live-only and
durably queued detections. `generation.complete = false` means a missing or stale
refresh prevents immediate action even when `eligible` is nonempty. A plate you
previously rejected or an interrupted pending directory without a manifest
appears in `generation.terminal_blocked`; inspect the taxon and run
`retry TAXON_ID` to make it eligible again. When `generation.actionable` is
nonempty, let the scheduled generator run or invoke
`inky-bird-frame generate --config /path/to/config.toml` for an immediate
attempt.

For BirdNET-Go, use the same commands and inspect the `birdnet-go` provider.
Connection failures usually mean the configured base URL is not reachable from
the controller process or container. A response-contract error can indicate an
older or incompatible BirdNET-Go API. An `unresolved_count` means the summary
was reachable but one or more scientific names did not exactly match an active
iNaturalist bird species. Correct false positives in BirdNET-Go; its next
summary will exclude detections marked false-positive.

On macOS 15 or later, compare manual and scheduled refreshes before changing
the BirdNET-Go server. If a manual refresh succeeds but the LaunchAgent reports
`[Errno 65] No route to host`, macOS Local Network privacy may be blocking the
background process:

```bash
inky-bird-frame refresh --config "/path/to/config.toml"
refresh_label="gui/$(id -u)/com.inky-bird-frame.refresh"
refresh_pid="$(launchctl kickstart -kp "$refresh_label")"
while kill -0 "$refresh_pid" 2>/dev/null; do sleep 1; done
tail -n 200 "$HOME/Library/Application Support/Inky Bird Frame/logs/refresh.log"
```

An unauthenticated HTTPS reverse proxy on a routed network path can avoid the
direct-subnet restriction. Set `birdnet_go_url` to the proxy's base URL and
repeat the LaunchAgent test. The proxy must preserve
`/api/v2/analytics/species/summary`; Inky does not send proxy credentials. See
the [BirdNET-Go macOS setup](discovery.md#macos-local-network-access) and
[Apple TN3179](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy)
for the platform behavior and system-wide alternatives.

For BirdNET Analyzer, verify the CSV was imported before enabling the provider.
An undated import is intentionally empty in every finite window; use
`window = "all-time"` or reimport with `--observed-on` only when the recording
date is known. A missing-history error means no successful non-dry-run import
exists in the configured `state_dir`. A permissions error requires the state
file to be owned by the controller account and use mode `0600`.

For eBird Archive, run `ebird archive status` and confirm the checklist,
observation, species, and date-range aggregates. A missing-history error means
the official **Download My Data** ZIP or CSV has not completed a non-dry-run
import in this `state_dir`. A history-reduction error means the new file omits
one or more previously imported checklists; verify it is a complete export
before using `--allow-history-reduction`. Unresolved aggregate or hybrid labels
are expected to remain private and never enter generation.

For Bird Buddy, run `birdbuddy status` first to inspect the saved attestation,
feeder, and history. This command is local-only and cannot prove that the saved
session remains valid. Run `discover` to perform the authenticated refresh. A
missing, invalid, or revoked session then requires an explicit login; Inky never
retains the password for an automatic retry. If several feeders are accessible,
rerun login with the intended `--feeder-id`. Rate limiting or a malformed
GraphQL response degrades only this provider. A schema or pagination error can
mean the private, unsupported API changed; disable `birdbuddy` in
`discovery.sources` while investigating rather than repeatedly logging in or
editing state JSON.

The active catalog is exactly what the display node can choose from:

```bash
# On the controller
# Match the reachable address and port in [controller]. These are the defaults.
CONTROLLER_URL="http://127.0.0.1:8793"
curl --fail --silent "${CONTROLLER_URL}/v1/catalog"

# On a systemd display node
journalctl -u inky-bird-frame-display.service -n 100 --no-pager
```

When `prioritize_latest_detection` is enabled, the display shows the newest
unconsumed detection once and then resumes normal rotation. Its successful
cycle reports `"selection_reason": "latest_detection"`; `display_update` may
be `unchanged` when that plate is already on the panel. A station
classification that looks wrong must be corrected at the detector. Inky Bird
Frame does not download the audio or apply a second confidence filter.

### TLS or certificate errors right after boot

Raspberry Pi computers have no battery-backed real-time clock. After a power
loss, the clock is wrong until NTP synchronizes, so outbound HTTPS calls can
fail certificate validity checks during the first minutes after boot. These
errors clear on their own once the clock is correct.

```bash
timedatectl
```

Wait for `System clock synchronized: yes` before treating a certificate
failure as a persistent problem.

### Generation fails or keeps retrying

```bash
inky-bird-frame status --config /path/to/config.toml
codex login status
```

Transient source, reference, and Codex failures are deferred per species and do
not block later queue entries. Terminal factual or visual failures require an
explicit `retry TAXON_ID` after investigation. See
[`operations.md`](operations.md#failure-recovery).

For a specific run, inspect its private structured history before reading the
larger Codex logs:

```bash
jq . /path/to/state/runs/TAXON-TIMESTAMP/attempt-history.json
```

`outcome` separates generation-process errors, review-process errors, ordinary
image corrections, source-backed profile conflicts, and passing attempts.
`regressed_findings` and `regressed_axes` show a correction that returned or a
score that fell below the quality threshold. A profile conflict may perform
one bounded refresh; compare `profile-before-refresh.json` and
`profile-after-refresh.json`. Do not edit the cached profile to copy a
reviewer's claim. If a validated model is pinned with `controller.codex_model`,
confirm that the deployed Codex CLI still accepts it.

### Catalog publication fails

Inspect the structured command result first. On macOS it is written to the
standard-output log:

```bash
tail -n 100 "$HOME/Library/Application Support/Inky Bird Frame/logs/catalog-publish.log"
gh auth status --hostname github.com
inky-bird-frame catalog-publish --config /path/to/config.toml --dry-run
```

Run GitHub CLI as the same OS account that owns the publisher service. The
`.error.log` file contains process-level diagnostics, not ordinary structured
command failures. After correcting authentication or repository state, require
the dry-run to pass before running an immediate publication cycle.

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

The supported results are `800 480` for PIM773 and `1600 1200` for PIM774. If
import fails, rerun Pimoroni's installer and reinstall this project's `inky`
extra into that same environment.

### The included image is rotated incorrectly

Use the committed `display.png`, not `portrait.png`:

```bash
"$HOME/.virtualenvs/pimoroni/bin/inky-bird-frame" display-image \
  catalog/species/12942-eastern-bluebird/display.png
```

The catalog's display asset is already rotated left for a portrait-mounted
panel. PIM773 fitting preserves that orientation automatically. Do not add a
second OS-level rotation.

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
