# Docker controller

Docker runs the controller. The Raspberry Pi display node still uses the native
systemd installation because it needs direct access to the Inky hardware.

The normal Docker path pulls a published image from GitHub Container Registry.
It does not build the project from source. Images are available for AMD64 and
ARM64 hosts.

The image includes the project license and the pinned Codex CLI and GitHub CLI
license material under `/usr/share/licenses/inky-bird-frame/`. See
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) for component details.

## What runs

Compose starts four services from the same image:

| Service | Job | Credentials |
| --- | --- | --- |
| `config-sync` | Validates the host `config.toml`, installs a private runtime copy, then exits | Configuration file and referenced environment values |
| `bootstrap` | Copies the included public bird catalog into persistent storage, then exits | None |
| `controller` | Serves health, catalog metadata, and approved images on port 8793 | None |
| `scheduler` | Refreshes observations and runs generation, notifications, and optional catalog publication | Codex and any configured service credentials |

The controller and scheduler use the same commands as a native installation.
One failed scheduled job is logged and retried later without stopping the HTTP
service or unrelated jobs.

The host `config.toml` is authoritative. Compose bind-mounts it read-only into
the one-shot `config-sync` service, not the long-running services. Local Docker
Compose cannot remap the owner or mode of a file-backed config, so mounting a
mode-0600 host file directly into the unprivileged controller is not portable.
`config-sync` is therefore a short-lived container-root init process. It runs
offline with a read-only root filesystem, `no-new-privileges`, every capability
dropped except `DAC_OVERRIDE` and `CHOWN`, one read-only host bind, and the
controller data volume. It receives the same optional `controller.env` as the
scheduler so references such as `ebird_api_key_env` can be validated without
putting their values in `config.toml`. With networking disabled, those values
cannot be sent anywhere by this service. It validates and atomically installs
a mode-0600 copy, then transfers ownership to Inky's UID/GID 10001 before any
long-running service can start. A failure at any step blocks startup. The
private bind relabels the source for enforcing SELinux hosts and refuses to
create a directory when the configured file is missing. The controller and
scheduler remain unprivileged with no added capabilities. Set
`INKY_BIRD_CONFIG` in `.env` when the host file is not `./config.toml`.

### User namespace remapping

Standard Docker Engine and rootless Docker can read a mode-0600 host config:
standard Engine uses the init service's two scoped capabilities, while
rootless Docker maps container root to the host account that owns the file.
[Daemon-wide `userns-remap`](https://docs.docker.com/engine/security/userns-remap/)
instead maps container root to a subordinate host UID. Docker requires bind
mount permissions to be arranged for that UID.

If `userns-remap` is enabled, find the configured remap account and its first
subordinate UID in `/etc/subuid`, then grant only that mapped-root UID read
access to the private host file. For example, when the remap account is
`dockremap`:

```bash
remap_root_uid="$(awk -F: '$1 == "dockremap" { print $2; exit }' /etc/subuid)"
test -n "$remap_root_uid"
sudo setfacl --modify "u:${remap_root_uid}:r" config.toml
getfacl config.toml
```

Use the account actually configured by your daemon; `default` means
`dockremap`. Do not make the file group- or world-readable, and do not disable
user-namespace isolation for this container. Without the narrow ACL,
`config-sync` fails closed with an actionable error before reading or replacing
the runtime copy. Docker's
[UID/GID mapping guide](https://docs.docker.com/engine/security/rootless/uid-gid-mapping/)
explains the difference between rootless and `userns-remap` ownership.

`bootstrap` runs `catalog sync --source-catalog /app/catalog --catalog
/data/catalog --state-dir /data/var/controller`. Compose opts into reviewed
migrations and independent local approval retention through environment
variables instead of version-specific command-line arguments, so the current
Compose definition can still start an older image after the matching persistent
snapshot is restored. Bootstrap validates the bundled and persistent catalogs,
copies missing species, and applies only replacements whose recorded approval
hashes and complete migration ancestry match the persistent plate. A validated
persistent descendant is reported as `retained_newer`. A valid local plate made
before the same exact species entered the public catalog has no shared approval
ancestry, so bootstrap preserves it and reports it as `retained_independent`.
It never changes taxon identity or downgrades a local plate. Shared-history
forks, malformed catalogs, and incomplete or tampered migration records still
fail closed. The sync rebuilds the index and holds the controller state lock so
it cannot race a running cycle. The
[operations guide](operations.md#copy-species-between-catalogs) describes the
command in general.

## Before you begin

You need:

- Docker Engine with the Compose plugin, or Docker Desktop;
- a 64-bit AMD64 or ARM64 host;
- enough storage for generated images and controller state;
- a ChatGPT plan that includes Codex, or an OpenAI API key with separate API
  billing; and
- a controller address that the display Pi can reach on TCP 8793.

Do not expose port 8793 to the public internet. It serves a read-only catalog
and accepts narrow, unauthenticated display-health reports on a trusted
network.

## 1. Download the deployment bundle

Each GitHub release includes a small Docker bundle. It contains Compose,
example configuration, and the Ubuntu AppArmor profile. The bundle pins the
controller image to that release.

```bash
mkdir -p "$HOME/inky-bird-frame"
cd "$HOME/inky-bird-frame"
curl -fsSLO \
  https://github.com/veteranbv/inky-bird-frame/releases/latest/download/inky-bird-frame-docker.tar.gz
curl -fsSLO \
  https://github.com/veteranbv/inky-bird-frame/releases/latest/download/inky-bird-frame-docker.tar.gz.sha256
sha256sum -c inky-bird-frame-docker.tar.gz.sha256
tar -xzf inky-bird-frame-docker.tar.gz --strip-components=1
```

The checksum command uses GNU `sha256sum`, which is standard on Linux. On
macOS, use:

```bash
expected=$(cut -d ' ' -f 1 inky-bird-frame-docker.tar.gz.sha256)
printf '%s  %s\n' "$expected" inky-bird-frame-docker.tar.gz | shasum -a 256 -c -
```

If you prefer a source checkout, clone the repository and use the same
`compose.yaml`. Compose still pulls the published image unless you explicitly
add the [source-build override](#build-from-source).

## 2. Create private configuration

```bash
cp config.example.toml config.toml
cp controller.env.example controller.env
cp .env.example .env
chmod 600 config.toml controller.env .env
```

Edit `config.toml`. At minimum:

1. replace the example discovery location;
2. choose one or more discovery sources;
3. set `display_node.controller_url` to the address the display Pi will use;
4. leave `public_catalog.enabled = false` unless you own the catalog
   repository; and
5. enable only the notifications you want.

The relative controller paths in the example are intentional. After the file
is synchronized into `/data`, they resolve to `/data/workspace`, `/data/catalog`,
and `/data/var/controller` inside the persistent volume. Codex receives write
access to the workspace directory, not the private configuration, approved
catalog, or controller state directories.

Secrets can stay in the mode-`0600` TOML file or come from `controller.env`.
For environment-backed values, name the variable in TOML:

```toml
[discovery]
sources = ["inaturalist", "ebird"]
ebird_api_key_env = "EBIRD_API_KEY"

[[notifications.destinations]]
name = "pushover"
url_env = "APPRISE_PUSHOVER_URL"
events = ["generation_approved", "terminal_error", "degraded", "recovered"]
```

Then set the matching values in `controller.env`, one `NAME=value` per line.
Do not quote a value unless the quote characters are part of the secret.

For self-hosted BirdNET-Go, `birdnet_go_url` must be reachable from inside the
controller container. A DNS name or LAN address is usually clearer than a
host-only loopback address:

```toml
[discovery]
sources = ["birdnet-go"]
birdnet_go_url = "http://birdnet-go.local:8080"
```

No detector credential or audio volume is required. Keep the endpoint on a
trusted network or VPN. A TLS reverse proxy may provide transport encryption,
but this provider does not send proxy authentication credentials.

To import BirdNET Analyzer results, copy the CSV to a temporary controller-
readable path or bind-mount it read-only, then run the import command in the
controller container. The durable result is stored in `/data/var/controller`;
the CSV and recordings do not belong in the persistent volume. For example:

```bash
docker compose cp /path/to/results.csv controller:/tmp/results.csv
docker compose exec controller inky-bird-frame birdnet-analyzer import \
  --config /data/config.toml --csv /tmp/results.csv --observed-on 2026-08-09
docker compose exec controller rm /tmp/results.csv
```

Use `--observed-on` only when every row shares that known recording date. Add
`"birdnet-analyzer"` to `discovery.sources` and run `refresh` after importing.

For an eBird personal-data export, copy the complete ZIP into the controller's
temporary filesystem, preview it, and then import it:

```bash
docker compose cp /path/to/ebird.zip controller:/tmp/ebird.zip
docker compose exec controller inky-bird-frame ebird archive import \
  --config /data/config.toml --archive /tmp/ebird.zip --dry-run
docker compose exec controller inky-bird-frame ebird archive import \
  --config /data/config.toml --archive /tmp/ebird.zip
docker compose exec controller rm /tmp/ebird.zip
```

The durable private history is written under `/data/var/controller`; the raw
export is not retained. Add `"ebird-archive"` to `discovery.sources` and run
`refresh` after the successful import.

Bird Buddy is different: its email and password are used once and must not be
kept in `config.toml` or `controller.env`. After obtaining Bird Buddy's
permission, add `"birdbuddy"` to `discovery.sources`, import the configuration,
and perform the one-time login.

Manually added app sightings are excluded by default because they are not tied
to the selected feeder. Set `birdbuddy_include_manual_sightings = true` in
`[discovery]` before importing the configuration when they should count for
this installation. The `-e` options below pass existing shell variables without
putting their values in the command line:

```bash
read -r -p "Bird Buddy email: " INKY_BIRDBUDDY_EMAIL
read -r -s -p "Bird Buddy password: " INKY_BIRDBUDDY_PASSWORD
printf '\n'
export INKY_BIRDBUDDY_EMAIL INKY_BIRDBUDDY_PASSWORD
docker compose run --rm --no-deps \
  -e INKY_BIRDBUDDY_EMAIL -e INKY_BIRDBUDDY_PASSWORD scheduler \
  birdbuddy login --config /data/config.toml --confirm-authorized-access
unset INKY_BIRDBUDDY_EMAIL INKY_BIRDBUDDY_PASSWORD
```

If the guest account can access several feeders, rerun with the reported
`--feeder-id`. Check the redacted local state with `birdbuddy status`. Logout
removes the local refresh token while keeping detection history:

```bash
docker compose run --rm --no-deps scheduler \
  birdbuddy status --config /data/config.toml
docker compose run --rm --no-deps scheduler \
  birdbuddy logout --config /data/config.toml --yes
```

The `.env` file controls the container image tag. A release bundle pins its
exact release version. Keep that pin for repeatable updates and rollback. Use
`latest` only when you want the newest trusted `main` build.

## 3. Install the Ubuntu AppArmor profile

Ubuntu restricts unprivileged user namespaces through AppArmor. Codex uses a
Bubblewrap sandbox that needs a narrowly scoped exception. Install the included
profile on an Ubuntu Docker host:

```bash
sudo install -m 0644 deploy/apparmor/inky-bird-frame-codex \
  /etc/apparmor.d/inky-bird-frame-codex
sudo apparmor_parser -r /etc/apparmor.d/inky-bird-frame-codex
```

Docker Desktop does not need this host step. Other Linux distributions may not
enable Ubuntu's AppArmor user-namespace restriction.

The scheduler drops every Linux capability and runs with a read-only root
filesystem and `no-new-privileges`. Docker's outer seccomp and AppArmor
profiles are disabled only for the scheduler because they block Bubblewrap's
namespace setup. The host profile limits the exception to the Codex executable,
and Codex applies its own filesystem and network sandbox to generated commands.

## 4. Pull and configure the controller

```bash
docker compose pull
docker compose config --quiet
docker compose run --rm --no-deps config-sync
docker compose run --rm --no-deps scheduler \
  config validate --config /data/config.toml
```

`config-sync` validates the full host file before replacing the private runtime
copy. An invalid edit leaves the last valid runtime copy untouched and prevents
dependent services from starting through `docker compose up`.

## 5. Authenticate Codex

Codex credentials live in a separate persistent Docker volume. Sign in once:

```bash
docker compose run --rm --no-deps --entrypoint codex scheduler login --device-auth
docker compose run --rm --no-deps --entrypoint codex scheduler login status
```

Device-code login must be allowed by the ChatGPT account or workspace. ChatGPT
sign-in uses the associated Codex subscription. API-key login is available but
billed separately. Treat the `codex-auth` volume as a password-equivalent
secret.

## 6. Start and verify

```bash
docker compose up --detach
docker compose ps
docker compose run --rm --no-deps controller --version
curl --fail --silent http://127.0.0.1:8793/health
docker compose logs --tail 100 scheduler
```

The command and the health response identify the running application version.
The health response should also contain `"ok": true`. The first scheduler pass
refreshes observations before generation is allowed. `bootstrap` should finish
with exit code zero; it safely checks the catalog again whenever Compose
recreates the project.

After this check passes, continue with
[Prepare the display Pi](installation.md#2-prepare-the-display-pi).

## Optional catalog publication

Most users do not need GitHub authentication. It is required only when
`public_catalog.enabled = true` and this controller is allowed to publish new
plates to a repository you own.

```bash
docker compose run --rm --no-deps --entrypoint gh scheduler auth login --web
docker compose run --rm --no-deps --entrypoint gh scheduler auth setup-git
docker compose run --rm --no-deps --entrypoint git scheduler clone \
  https://github.com/OWNER/REPOSITORY.git /data/public-catalog
```

Set `public_catalog.repository = "OWNER/REPOSITORY"` and
`public_catalog.checkout_dir = "/data/public-catalog"` in `config.toml`, run
`config-sync`, and recreate the scheduler.

## Storage and recovery

`controller-data` contains configuration, observations, generated work,
approved plates, retry state, notification queues, any publication checkout,
and optional Bird Buddy refresh-token state. `codex-auth` and `github-auth`
contain authentication state.

Compose uses `restart: unless-stopped`, so the HTTP service and scheduler return
after Docker starts following a reboot. The scheduler requires a successful
observation refresh before it generates anything.

Back up `config.toml`, `controller.env`, `.env`, and the `controller-data`
volume. Treat every backup containing `controller-data` as credential-sensitive
when Bird Buddy is enabled. Protect authentication volumes if you include them.
`docker compose down` keeps all volumes. `docker compose down --volumes`
deletes permanent controller state and should not be part of a normal update.
Use the [backup and restore guide](backup.md) for a quiesced volume snapshot,
credential options, restore procedure, and validation.

Before an update, take a restorable point-in-time snapshot of `controller-data`
and the matching configuration files. Stop `scheduler` and `controller` while
capturing a filesystem-level copy if the backup system cannot snapshot the
volume atomically. Keep that snapshot until the updated controller has completed
bootstrap, refresh, generation status, and display verification.

## Update

Read the release notes and take a verified backup, then change
`INKY_BIRD_IMAGE` in `.env` to the desired version:

When moving from v0.8.1 or earlier, confirm that the host `config.toml` contains
the settings currently installed in `controller-data` before replacing the old
Compose file. If the runtime copy may be newer, export it without printing its
secrets to the terminal:

```bash
umask 077
docker compose run --rm --no-deps -T --entrypoint cat controller \
  /data/config.toml > config.from-controller.toml
cmp -s config.toml config.from-controller.toml || \
  echo "Review the two private files before updating"
```

Keep the intended version as `config.toml` with mode `0600`. The new
`config-sync` service treats that host file as authoritative.

```bash
docker compose pull
docker compose run --rm --no-deps config-sync
docker compose up --detach --remove-orphans --force-recreate
docker compose run --rm --no-deps controller --version
curl --fail --silent http://127.0.0.1:8793/health
```

After editing `config.toml`, rerun `config-sync` and recreate the controller and
scheduler so both processes load the same validated copy. Recreating the
services is also required after changing `controller.env`; a restart does not
reload a container's environment.

Changing the image tag rolls back application code only; it does not roll back
persistent state. Reviewed catalog sync is forward-only. A newer, validated
persistent descendant is retained rather than replaced with an older bundled
plate. A validated independent local approval is also retained when the bundled
catalog later adds that exact species. To roll back catalog or state migrations,
stop the stack, restore the
matching pre-update `controller-data` snapshot and configuration, select the
prior image tag, and repeat the `pull` and `up` commands. Do not combine an old
image with newer persistent state unless that release's notes explicitly state
that the combination is compatible.

## Build from source

Source builds are for contributors and operators who are testing a local
change. They are not required for a normal installation.

From a repository checkout:

```bash
docker compose -f compose.yaml -f compose.build.yaml build --pull
docker compose -f compose.yaml -f compose.build.yaml run --rm --no-deps config-sync
docker compose -f compose.yaml -f compose.build.yaml up --detach
```

The override sets `pull_policy: build`, so every service uses the locally built
runtime. The default `compose.yaml` remains registry-only.

## Troubleshooting

```bash
docker compose ps --all
docker compose logs config-sync bootstrap controller scheduler
docker compose run --rm --no-deps scheduler \
  config validate --config /data/config.toml
```

If Codex cannot start its sandbox on Ubuntu, confirm that the included AppArmor
profile is installed and loaded. Do not disable AppArmor's system-wide
unprivileged-user-namespace restriction.

The image publication workflow runs only for trusted `main`, published
releases, or an owner-started manual run. Pull requests build and test the
container in CI but cannot publish packages or receive controller credentials.
