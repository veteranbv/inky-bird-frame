# Browser settings

This guide describes `main`. Use the guide for your installed release when
upgrading a working controller.

## Start with approved art

Choose [Docker or NAS](docker.md#before-you-begin) or follow the native steps
below. Neither path requires display hardware or a Codex login to browse
approved art.

### Native setup

Use a fresh checkout and check the
[native prerequisites](installation.md#before-you-begin).

```bash
uv sync --extra controller --locked
uv run inky-bird-frame config init --destination ./private/config.toml
uv run inky-bird-frame setup controller --config ./private/config.toml
```

The `config init` command creates a mode-0600 configuration and refuses to
replace an existing file. Use an empty `private` directory for browser account
creation; prior files or runtime paths outside that directory require local
account setup instead. The `setup controller` command previews installation.
Check the paths before repeating it with `--yes`. Setup copies the approved catalog and
starts the native services. Follow the [installation guide](installation.md)
for operating-system prerequisites and service-account behavior.

New configurations have no selected sources and generation off. You can create
your owner account, browse approved artwork, save selections and open Frame
without Codex authentication or a paid generation. A refresh with no sources
is healthy and makes no requests to observation services.

Open the controller's actual home-network address followed by `:8793/app`.
`http://127.0.0.1:8793/app` works only on the controller itself. A phone needs
the controller's LAN address. Do not treat an untested friendly hostname as a
working address. See [browser access](browser-access.md) for accepted origins,
Docker port forwarding and optional HTTPS.

## One file, one owner

Existing installations remain **managed**. Their external TOML file and injected
environment remain authoritative. Source pages link to setup, but settings are
locked until the operator changes the file or explicitly adopts app management.
An upgrade does not change the existing generation policy.

New `config init` installations are **app-managed**. Browser changes update that
same configuration file. Inky checks the complete candidate before an atomic
replacement. The file's content hash is its revision: a stale tab cannot replace
a newer save. A private `config.toml.previous` holds the exact previous bytes.
Each successful save replaces that one previous-file backup; it is not a backup
history or a substitute for a complete controller backup.

In Manage → Settings and backups, adoption has a separate preview and confirm
step. It preserves existing source choices and generation policy. Then re-run
native setup and restart the services. App-managed native installations use one
dynamic scheduler instead of fixed per-job timers. Edits stay locked until that
restart. Adoption in Docker makes `config-sync` preserve the app-managed runtime
file on subsequent starts, rather than overwrite it from the original host seed.
Back up the persistent runtime volume, not only that seed.

To return to file management, stop Inky, restore the matching previous
configuration or deliberately set `configuration_ownership = "managed"`, run
native setup again and restart. Validate the complete file first. Restore an
older application only with its matching complete backup.

## Safe daily changes

Source pages let you enable a supported source and enter its applicable
connection fields. Settings controls the shared live window, community-report
location, generation policy and notification destinations. Review the proposed
choices before saving. Unsupported source/window combinations fail without
changing the file. A settings save is not a successful provider connection test.

Provider credentials are write-only. An empty credential field keeps its stored
value. Notification URLs follow the same rule when the destination name stays
the same; renaming requires the URL again. Credentials backed by environment
references remain installer-owned. The browser cannot create a new environment
reference, change its name or replace its secret.

HTTP does not encrypt credentials or private data in transit. Use it only on a
trusted home network, with a unique Inky password. HTTPS is optional for ordinary
local use and required by the advanced SSO deployment. See the access guide.

Generation requires an explicit acknowledgement before changing from off to on.
Install and authenticate Codex through the operator workflow first. Run the
controller doctor before enabling it. AI work uses your configured account's
allowance or costs. Existing review, retry and hold rules remain in force.

Turning generation off preserves queued work, history and art. It does not stop
an attempt that is already running. The dynamic scheduler stops starting
generation jobs when it reads the changed policy. Managed native deployments
also need setup rerun to remove the generation timer. The generation command
itself performs no work or recovery-notification writes while disabled.

Changing discovery policy invalidates its previous successful refresh for new
generation. The scheduler requests a new refresh. Work stays incomplete until
discovery matches the selected policy. This prevents old nearby observations
from becoming new work after you change sources or location.

Port binding, process identity, executables, HTTPS certificates, proxy trust,
OIDC configuration and public publication remain installer-owned. Browser edits
cannot change those boundaries. An external installation change needs a restart.

## Backups and verification

Configuration and its previous-file copy can contain live credentials. Keep
both private. Use a stopped, consistent [controller backup](backup.md).

After saving, reload Settings and check the saved choices. For a native install,
run the controller doctor. An app-managed installation should report its
scheduler service; a managed generation-off installation should not require a
generation timer or Codex authentication. Check actual discovery and notification
outcomes separately. A valid configuration is not proof of successful delivery.
