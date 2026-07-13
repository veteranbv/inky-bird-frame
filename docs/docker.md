# Docker controller

Docker runs the controller only. The Raspberry Pi display node still uses the
native systemd installation because it needs direct access to the Inky hardware.

The Compose project contains three services:

| Service | Responsibility | Credentials |
| --- | --- | --- |
| `bootstrap` | Add the image's validated public catalog to persistent storage, then exit | None |
| `controller` | Serve health, catalog metadata, and approved assets | None |
| `scheduler` | Run refresh, generation, optional publication, and notification delivery | Codex and optional GitHub CLI authentication |

All work is serialized through the same one-shot commands used by native
installations. A failed job is logged and retried on its next interval without
stopping unrelated jobs. Generation starts only after a successful observation
refresh in the current scheduler process. A long job can delay later jobs; each
overdue job runs once when the prior work finishes instead of creating a burst
of overlapping processes.

The scheduler runs as UID 10001 with a read-only root filesystem, all Linux
capabilities dropped, and `no-new-privileges`. It disables Docker's outer
seccomp and AppArmor profiles because they block the unprivileged namespace and
mount setup required by Bubblewrap. On Ubuntu, a targeted host AppArmor profile
allows only the container's Codex binary to create the user namespace. Codex then
applies its own Bubblewrap filesystem and network sandbox to generated commands.
The controller and bootstrap retain Docker's default security profiles, and every
service has all Linux capabilities dropped.

## Prerequisites

Install Docker Engine with the Compose plugin, or Docker Desktop, on a 64-bit
ARM or x86 Linux host, macOS computer, or Windows computer using Linux
containers. The host must remain reachable from the display node on TCP 8793.

Clone the repository and create private configuration files:

```bash
git clone https://github.com/veteranbv/inky-bird-frame.git
cd inky-bird-frame
cp config.example.toml config.toml
cp controller.env.example controller.env
chmod 600 config.toml controller.env
```

Ubuntu restricts unprivileged user namespaces through AppArmor. Install the
included profile before starting the scheduler. This keeps the system-wide
restriction enabled and grants the exception only to Codex inside this image:

```bash
sudo install -m 0644 deploy/apparmor/inky-bird-frame-codex \
  /etc/apparmor.d/inky-bird-frame-codex
sudo apparmor_parser -r /etc/apparmor.d/inky-bird-frame-codex
```

Docker Desktop does not require this host step.

Both private filenames are ignored by Git. Keep `config.toml` at mode `0600`;
it is imported into container-managed private storage instead of being exposed
through a host UID-dependent bind mount.

## Configure

Edit `config.toml`. In addition to the normal discovery settings, use these
container paths:

```toml
[controller]
workspace_dir = "/data/workspace"
catalog_dir = "/data/catalog"
state_dir = "/data/state"
codex_path = "/usr/local/bin/codex"
bind_host = "0.0.0.0"
port = 8793

[display_node]
controller_url = "http://YOUR_CONTROLLER:8793"
state_dir = "/data/display-state"

[public_catalog]
enabled = false
checkout_dir = "/data/public-catalog"
gh_path = "/usr/local/bin/gh"
```

Set `YOUR_CONTROLLER` to a stable DNS name or address reachable by the display
Pi. Keep `enabled = false` unless this is the repository owner's trusted
publication controller.

Prefer environment-backed secrets. For example:

```toml
[discovery]
sources = ["inaturalist", "ebird"]
ebird_api_key_env = "EBIRD_API_KEY"

[[notifications.destinations]]
name = "pushover"
url_env = "APPRISE_PUSHOVER_URL"
events = ["discovery", "generation_approved", "terminal_error", "degraded", "recovered"]
```

Put the corresponding values in `controller.env`, one `NAME=value` per line.
Do not quote Compose environment-file values unless the quotes are part of the
secret. Direct TOML values remain supported for users who manage the entire
private file through a secrets system.

Build the image, validate the resolved Compose model, and import the private
configuration through standard input:

```bash
docker compose build --pull
docker compose config --quiet
docker compose run --rm --no-deps -T scheduler \
  config install --destination /data/config.toml < config.toml
docker compose run --rm --no-deps scheduler \
  config validate --config /data/config.toml
```

`config install` validates the complete TOML before atomically replacing the
container copy and stores it with mode `0600`. Run the same import after any
local configuration change. The local file remains the operator-owned source
of truth. If the services are already running, reload the imported settings:

```bash
docker compose up --detach --force-recreate controller scheduler
```

Recreating the services is required when `controller.env` changes because a
container restart does not reload its environment. It also gives both services
the newly imported `config.toml` in one consistent operation.

## Authenticate Codex

The image contains a checksum-verified official Codex CLI binary. Authenticate
once through the scheduler's persistent credential volume:

```bash
docker compose run --rm --no-deps --entrypoint codex scheduler login --device-auth
docker compose run --rm --no-deps --entrypoint codex scheduler login status
```

Device-code login must be allowed by the ChatGPT account or workspace. ChatGPT
sign-in uses the associated Codex subscription; API-key login is available but
billed separately. The `codex-auth` volume is writable because Codex refreshes
its tokens. Treat that volume as a password-equivalent secret.

Repository owners who enable automatic catalog publication must also
authenticate GitHub CLI, configure Git credential access, and create the
trusted checkout expected by `[public_catalog].checkout_dir`:

```bash
docker compose run --rm --no-deps --entrypoint gh scheduler auth login --web
docker compose run --rm --no-deps --entrypoint gh scheduler auth setup-git
docker compose run --rm --no-deps --entrypoint git scheduler clone \
  https://github.com/OWNER/REPOSITORY.git /data/public-catalog
```

Set `repository = "OWNER/REPOSITORY"` in `config.toml` and verify that the
authenticated GitHub account is that repository owner. Other users do not need
GitHub authentication or a publication checkout.

## Start and verify

Start the configured controller:

```bash
docker compose up --detach
docker compose ps
curl --fail --silent http://127.0.0.1:8793/health
docker compose logs --tail 100 scheduler
```

The first scheduler pass refreshes observations immediately. It then runs
generation and any enabled notification or publication work. A healthy HTTP
response has `"ok": true`. The `bootstrap` service should show exit code zero;
it runs again safely whenever Compose recreates the project.

If the host firewall is enabled, allow TCP 8793 only from the trusted network.
Do not expose this unauthenticated read-only service to the public internet.

## Persistence and recovery

`controller-data` stores the private configuration, observations, generated
work, approved plates, retry state, notification queues, and the optional
publication checkout. `codex-auth` and `github-auth` contain authentication
state. Compose uses
`restart: unless-stopped`, so the HTTP service and scheduler return after the
Docker daemon starts following a reboot. The scheduler performs a fresh
observation refresh before allowing generation.

Back up `config.toml`, `controller.env`, and the `controller-data` volume. Codex
and GitHub authentication can be recreated, but they must be protected if
included in a backup. `docker compose down` preserves volumes. Do not use
`docker compose down --volumes` unless permanent controller state should be
deleted.

## Update

For a source checkout:

```bash
git pull --ff-only
docker compose build --pull
docker compose run --rm --no-deps -T scheduler \
  config install --destination /data/config.toml < config.toml
docker compose up --detach --remove-orphans --force-recreate
curl --fail --silent http://127.0.0.1:8793/health
```

For an owner-published image, set `INKY_BIRD_IMAGE` to its explicit tag, then
run `docker compose pull` and `docker compose up --detach`. Avoid mutable tags
when repeatable rollback matters.

Inspect failures without exposing private configuration:

```bash
docker compose ps --all
docker compose logs controller scheduler bootstrap
docker compose run --rm --no-deps scheduler config validate --config /data/config.toml
```

The repository owner can publish an AMD64/ARM64 GHCR image from trusted `main`
with the owner-only **publish controller container** workflow. Pull requests
build the image in CI but cannot publish packages or receive deployment
credentials.
