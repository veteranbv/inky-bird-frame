# Docker controller

Docker runs the controller. The Raspberry Pi display node still uses the native
systemd installation because it needs direct access to the Inky hardware.

The normal Docker path pulls a published image from GitHub Container Registry.
It does not build the project from source. Images are available for AMD64 and
ARM64 hosts.

## What runs

Compose starts three services from the same image:

| Service | Job | Credentials |
| --- | --- | --- |
| `bootstrap` | Copies the included public bird catalog into persistent storage, then exits | None |
| `controller` | Serves health, catalog metadata, and approved images on port 8793 | None |
| `scheduler` | Refreshes observations and runs generation, notifications, and optional catalog publication | Codex and any configured service credentials |

The controller and scheduler use the same commands as a native installation.
One failed scheduled job is logged and retried later without stopping the HTTP
service or unrelated jobs.

## Before you begin

You need:

- Docker Engine with the Compose plugin, or Docker Desktop;
- a 64-bit AMD64 or ARM64 host;
- enough storage for generated images and controller state;
- a ChatGPT plan that includes Codex, or an OpenAI API key with separate API
  billing; and
- a controller address that the display Pi can reach on TCP 8793.

Do not expose port 8793 to the public internet. It is an unauthenticated,
read-only service intended for a trusted network.

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

1. replace the example ZIP code;
2. choose one or more discovery sources;
3. set `display_node.controller_url` to the address the display Pi will use;
4. leave `public_catalog.enabled = false` unless you own the catalog
   repository; and
5. enable only the notifications you want.

The relative controller paths in the example are intentional. After the file
is imported into `/data`, they resolve to `/data/workspace`, `/data/catalog`,
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

The `.env` file controls the container image tag. A release bundle pins a
version such as `0.1.0`. Keep that pin for repeatable updates and rollback. Use
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
docker compose run --rm --no-deps -T scheduler \
  config install --destination /data/config.toml < config.toml
docker compose run --rm --no-deps scheduler \
  config validate --config /data/config.toml
```

`config install` validates the full TOML file before replacing the private
container copy. The imported file is stored with mode `0600`. Repeat the import
after changing the host copy.

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
curl --fail --silent http://127.0.0.1:8793/health
docker compose logs --tail 100 scheduler
```

The health response should contain `"ok": true`. The first scheduler pass
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
`public_catalog.checkout_dir = "/data/public-catalog"` in `config.toml`, import
the updated file, and recreate the scheduler.

## Storage and recovery

`controller-data` contains configuration, observations, generated work,
approved plates, retry state, notification queues, and any publication
checkout. `codex-auth` and `github-auth` contain authentication state.

Compose uses `restart: unless-stopped`, so the HTTP service and scheduler return
after Docker starts following a reboot. The scheduler requires a successful
observation refresh before it generates anything.

Back up `config.toml`, `controller.env`, `.env`, and the `controller-data`
volume. Protect authentication volumes if you include them in a backup.
`docker compose down` keeps all volumes. `docker compose down --volumes`
deletes permanent controller state and should not be part of a normal update.

## Update

Read the release notes, then change `INKY_BIRD_IMAGE` in `.env` to the desired
version:

```bash
docker compose pull
docker compose run --rm --no-deps -T scheduler \
  config install --destination /data/config.toml < config.toml
docker compose up --detach --remove-orphans --force-recreate
curl --fail --silent http://127.0.0.1:8793/health
```

Recreating the services is required after changing `controller.env`; a restart
does not reload a container's environment.

For rollback, restore the prior image tag in `.env` and repeat the same `pull`
and `up` commands. Persistent data is not removed.

## Build from source

Source builds are for contributors and operators who are testing a local
change. They are not required for a normal installation.

From a repository checkout:

```bash
docker compose -f compose.yaml -f compose.build.yaml build --pull
docker compose -f compose.yaml -f compose.build.yaml run --rm --no-deps -T scheduler \
  config install --destination /data/config.toml < config.toml
docker compose -f compose.yaml -f compose.build.yaml up --detach
```

The override sets `pull_policy: build`, so every service uses the locally built
runtime. The default `compose.yaml` remains registry-only.

## Troubleshooting

```bash
docker compose ps --all
docker compose logs controller scheduler bootstrap
docker compose run --rm --no-deps scheduler \
  config validate --config /data/config.toml
```

If Codex cannot start its sandbox on Ubuntu, confirm that the included AppArmor
profile is installed and loaded. Do not disable AppArmor's system-wide
unprivileged-user-namespace restriction.

The image publication workflow runs only for trusted `main`, published
releases, or an owner-started manual run. Pull requests build and test the
container in CI but cannot publish packages or receive controller credentials.
