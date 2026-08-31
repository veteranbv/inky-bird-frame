# Backup and restore

Back up the controller before an update, host migration, storage change, or
manual private-data removal. The display node caches approved images and keeps
its last rendered plate without power; its state is replaceable. The controller
holds the durable data that matters.

## What to protect

The paths under `[controller]` in the private `config.toml` are the source of
truth:

| Data | Why it matters | Sensitivity |
| --- | --- | --- |
| `config.toml` | Provider credentials, notification destinations, paths, schedules, and private location | Secret and private |
| `catalog_dir` | Approved plates, manifests, and index | Reusable art; may also hold local-only approved work |
| `state_dir` | Observations, collection membership, imports, retry state, notifications, run history, and optional Bird Buddy refresh token | Private; credential-sensitive when Bird Buddy is enabled |
| `workspace_dir` | Current generated work and downloaded references | Private working data |

Relative paths resolve from the directory that contains `config.toml`. Record
the resolved source and restore paths with the backup. A source checkout,
virtual environment, container image, and display cache can be recreated and do
not replace a state backup.

Bird Buddy authentication lives in `state_dir/birdbuddy-auth.json`. It contains
a rotating refresh token and makes every backup that includes it
credential-sensitive. `birdbuddy-detections.json`, eBird Archive state, BirdNET
Analyzer state, discovery snapshots, and run records contain private species and
time history even when they contain no account credentials or precise location.

Codex and optional GitHub authentication are separate from the application
paths. Reauthentication on a restored host is safer than copying those stores.
If an operator chooses to back them up, the backup must receive the same
protection as a password or long-lived token.

## Native controller backup

### 1. Prepare a private destination

Create a backup directory on encrypted storage and restrict it to the controller
operator. Keep it outside the source checkout and outside any public issue
attachment.

```bash
BACKUP=/path/to/private-backup/inky-bird-frame
mkdir -p "$BACKUP"
chmod 700 "$BACKUP"
```

Set these paths from the deployment’s private configuration. Use absolute,
resolved paths; do not copy the placeholders.

```bash
CONFIG=/absolute/path/to/config.toml
WORKSPACE=/absolute/path/to/workspace
CATALOG=/absolute/path/to/catalog
STATE=/absolute/path/to/controller-state
IBF="$HOME/Services/inky-bird-frame/.venv/bin/inky-bird-frame"
```

`IBF` is the default managed-runtime path. Replace it when setup used a custom
application directory.

Validate the configuration before stopping services:

```bash
"$IBF" config validate --config "$CONFIG"
```

### 2. Quiesce scheduled work

Do not copy controller state while refresh, generation, notification dispatch,
or publication is writing it. Wait for any running job to finish, then stop its
timer before copying data. Record what was active first so the backup does not
silently resume work that an operator had intentionally suspended.

On systemd Linux, capture the active controller and timer set in the protected
backup directory, then stop the timers. Confirm that their corresponding
one-shot services are inactive before stopping the HTTP service:

```bash
SYSTEMD_STATE="$BACKUP/native-active-systemd-units.txt"
: > "$SYSTEMD_STATE"
for unit in \
  inky-bird-frame-controller.service \
  inky-bird-frame-refresh.timer \
  inky-bird-frame-generate.timer \
  inky-bird-frame-catalog-publish.timer \
  inky-bird-frame-notifications.timer; do
  state=$(systemctl is-active "$unit" 2>/dev/null || true)
  case "$state" in
    active) printf '%s\n' "$unit" >> "$SYSTEMD_STATE" ;;
    inactive | failed) ;;
    unknown)
      case "$unit" in
        inky-bird-frame-catalog-publish.timer | \
        inky-bird-frame-notifications.timer) ;;
        *)
          echo "Required unit state is unknown: $unit" >&2
          exit 1
          ;;
      esac
      ;;
    *)
      echo "Could not determine stable pre-backup state: $unit (${state:-no state})" >&2
      exit 1
      ;;
  esac
done
chmod 600 "$SYSTEMD_STATE"

sudo systemctl stop \
  inky-bird-frame-refresh.timer \
  inky-bird-frame-generate.timer \
  inky-bird-frame-catalog-publish.timer \
  inky-bird-frame-notifications.timer

for service in \
  inky-bird-frame-refresh.service \
  inky-bird-frame-generate.service \
  inky-bird-frame-catalog-publish.service \
  inky-bird-frame-notifications.service; do
  state=$(systemctl is-active "$service" 2>/dev/null || true)
  case "$state" in
    inactive | failed | unknown) ;;
    *)
      echo "One-shot service is not quiesced: $service ($state)" >&2
      exit 1
      ;;
  esac
done

sudo systemctl stop inky-bird-frame-controller.service

while IFS= read -r unit; do
  state=$(systemctl is-active "$unit" 2>/dev/null || true)
  case "$state" in
    inactive | failed) ;;
    *)
      echo "Could not confirm recorded unit stopped: $unit (${state:-no state})" >&2
      exit 1
      ;;
  esac
done < "$SYSTEMD_STATE"
```

Optional units can report `unknown` when the feature is disabled. Do not copy
until every installed one-shot service reports `inactive` or `failed`; an
`active`, `activating`, or `deactivating` job is still running.

On macOS, first inspect the LaunchAgents and wait for one-shot work to finish:

```bash
LAUNCHD_STATE="$BACKUP/native-loaded-launchagents.txt"
: > "$LAUNCHD_STATE"
for label in refresh generate catalog-publish notifications; do
  launchctl print "gui/$(id -u)/com.inky-bird-frame.$label" 2>/dev/null || true
done

for label in serve refresh generate catalog-publish notifications; do
  if launchctl print "gui/$(id -u)/com.inky-bird-frame.$label" >/dev/null 2>&1; then
    printf '%s\n' "$label" >> "$LAUNCHD_STATE"
  fi
done
chmod 600 "$LAUNCHD_STATE"
```

Then quiesce exactly the recorded agents. `bootout` stops scheduling and may
terminate an active job, so use it only after the inspection shows that no
one-shot job is running. Every recorded agent must unload successfully and be
absent before the copy begins:

```bash
while IFS= read -r label; do
  launchctl bootout "gui/$(id -u)/com.inky-bird-frame.$label" || exit 1
done < "$LAUNCHD_STATE"

while IFS= read -r label; do
  if launchctl print "gui/$(id -u)/com.inky-bird-frame.$label" >/dev/null 2>&1; then
    echo "Recorded LaunchAgent is still loaded after bootout: $label" >&2
    exit 1
  fi
done < "$LAUNCHD_STATE"
```

### 3. Copy a consistent snapshot

`rsync -a` preserves timestamps and permission bits. The separate destination
directories make restore targets explicit even when the original paths live in
different parent directories.

```bash
mkdir -p "$BACKUP/config" "$BACKUP/workspace" "$BACKUP/catalog" "$BACKUP/state"
rsync -a "$CONFIG" "$BACKUP/config/config.toml"
rsync -a "$WORKSPACE/" "$BACKUP/workspace/"
rsync -a "$CATALOG/" "$BACKUP/catalog/"
rsync -a "$STATE/" "$BACKUP/state/"
chmod 600 "$BACKUP/config/config.toml"
```

While writers are still stopped, checksum the copied file contents. These
commands produce no output when the backup matches the source:

```bash
cmp "$CONFIG" "$BACKUP/config/config.toml"
rsync -acni --delete "$WORKSPACE/" "$BACKUP/workspace/"
rsync -acni --delete "$CATALOG/" "$BACKUP/catalog/"
rsync -acni --delete "$STATE/" "$BACKUP/state/"
```

Record the application release or commit and the four original absolute paths
beside the backup. Do not put credentials into that note.

### 4. Restart and verify

Restore only the services recorded before the backup. On systemd Linux, reject
unexpected unit names before starting the saved active set:

```bash
SYSTEMD_STATE="$BACKUP/native-active-systemd-units.txt"
while IFS= read -r unit; do
  case "$unit" in
    inky-bird-frame-controller.service | \
    inky-bird-frame-refresh.timer | \
    inky-bird-frame-generate.timer | \
    inky-bird-frame-catalog-publish.timer | \
    inky-bird-frame-notifications.timer) ;;
    *)
      echo "Unexpected unit in $SYSTEMD_STATE: $unit" >&2
      exit 1
      ;;
  esac
done < "$SYSTEMD_STATE"

while IFS= read -r unit; do
  sudo systemctl start "$unit" || exit 1
done < "$SYSTEMD_STATE"

while IFS= read -r unit; do
  systemctl is-active "$unit" || exit 1
done < "$SYSTEMD_STATE"
```

On macOS, bootstrap only the LaunchAgents that were loaded before the backup:

```bash
LAUNCHD_STATE="$BACKUP/native-loaded-launchagents.txt"
while IFS= read -r label; do
  case "$label" in
    serve | refresh | generate | catalog-publish | notifications) ;;
    *)
      echo "Unexpected label in $LAUNCHD_STATE: $label" >&2
      exit 1
      ;;
  esac
  plist="$HOME/Library/LaunchAgents/com.inky-bird-frame.$label.plist"
  if [ ! -f "$plist" ]; then
    echo "Recorded LaunchAgent is missing: $plist" >&2
    exit 1
  fi
done < "$LAUNCHD_STATE"

while IFS= read -r label; do
  plist="$HOME/Library/LaunchAgents/com.inky-bird-frame.$label.plist"
  launchctl bootstrap "gui/$(id -u)" "$plist" || exit 1
done < "$LAUNCHD_STATE"

while IFS= read -r label; do
  launchctl print "gui/$(id -u)/com.inky-bird-frame.$label" >/dev/null || exit 1
done < "$LAUNCHD_STATE"
```

Restored systemd timers schedule their next activation. Several macOS
LaunchAgents use `RunAtLoad` and may run immediately when bootstrapped; preserving
the recorded set prevents the backup itself from resuming intentionally
suspended work.

If no installed component was deliberately suspended before the backup, finish
with `"$IBF" doctor controller --config "$CONFIG"`. When a component was already
stopped, the service-specific checks above are authoritative and `doctor` is
expected to report that intentional suspension.

## Native controller restore

Stage the same release that created the backup before restoring. Preserve the
current destination as a separate recovery copy; do not merge an unverified
backup over the only working state. The supported setup command performs a
persisted refresh immediately and enables scheduled work, so do not run it
until the restored baseline has passed the checks below. That refresh runs in
the foreground on systemd Linux and through `RunAtLoad` on macOS.

On a new host, follow the native [installation guide](installation.md) through
cloning the project, but stop before its setup command. On either a new or an
existing host, start a new shell in that source checkout and re-declare the
paths recorded with the backup. Replace every placeholder with the actual
private path before continuing:

```bash
cd /path/to/inky-bird-frame
BACKUP=/path/to/private-backup/inky-bird-frame
CONFIG=/absolute/path/to/config.toml
WORKSPACE=/absolute/path/to/workspace
CATALOG=/absolute/path/to/catalog
STATE=/absolute/path/to/controller-state
HEALTH_URL=http://127.0.0.1:8793/health
```

Set `HEALTH_URL` to the controller's actual local bind address and port. A
wildcard bind such as `0.0.0.0` is checked through `127.0.0.1`.

1. Prepare the matching tagged checkout and environment without running setup:

   ```bash
   INKY_VERSION=vX.Y.Z
   git fetch --tags --prune
   git checkout --detach "$INKY_VERSION"
   uv sync --extra controller --locked
   ```

2. On an existing host, stop the controller and scheduled jobs as described
   above. A new host has no installed services to stop.
3. Move any existing destination directories aside. Create empty replacement
   directories at the recorded paths, then restore as the controller account:

   ```bash
   for target in "$CONFIG" "$WORKSPACE" "$CATALOG" "$STATE"; do
     if [ -e "$target" ] || [ -L "$target" ]; then
       printf 'Refusing to merge restored state into existing path: %s\n' "$target" >&2
       exit 1
     fi
   done
   mkdir -p "$(dirname "$CONFIG")" "$WORKSPACE" "$CATALOG" "$STATE"
   rsync -a "$BACKUP/config/config.toml" "$CONFIG"
   rsync -a "$BACKUP/workspace/" "$WORKSPACE/"
   rsync -a "$BACKUP/catalog/" "$CATALOG/"
   rsync -a "$BACKUP/state/" "$STATE/"
   chmod 600 "$CONFIG"
   ```

4. Confirm that the controller account owns the restored paths. If a privileged
   operator performed the copy, correct ownership to the actual service account
   before starting anything; do not copy a placeholder user or group.
5. With every installed service and timer still stopped, inspect the restored
   files without contacting providers:

   ```bash
   uv run inky-bird-frame config validate --config "$CONFIG"
   uv run inky-bird-frame catalog validate --catalog "$CATALOG"
   uv run inky-bird-frame status --config "$CONFIG"
   ```

6. Test HTTP serving without installing or enabling a service. Run the server
   in one terminal, check it from another, then stop it with Control-C:

   ```bash
   uv run inky-bird-frame serve --config "$CONFIG"
   ```

   ```bash
   curl --fail --silent "$HEALTH_URL"
   ```

7. Run `uv run inky-bird-frame discover --config "$CONFIG"` as a deliberate
   live provider check and inspect its JSON output. It does not replace the
   saved observation snapshot. Bird Buddy is the narrow stateful exception:
   authenticated discovery rotates its saved refresh token and makes the saved
   session in the recovery copy stale. Keep the old controller stopped; if you
   revert to it, run an explicit `birdbuddy login` before discovery.
8. Before activation, verify Codex authentication as the controller account.
   On a new host, use one of the login methods from the installation guide
   first. When catalog publication is enabled, also install GitHub CLI and
   verify or restore its owner authentication:

   ```bash
   codex login status
   # Only when public_catalog.enabled = true:
   gh auth status --hostname github.com
   # If that status fails:
   gh auth login --hostname github.com --web
   gh auth setup-git
   ```

9. Once the offline baseline, HTTP health, provider result, and required
   credentials are correct, activate the restored controller. This is the point
   where setup triggers the persisted refresh and installs or reloads scheduled
   services:

   ```bash
   uv run inky-bird-frame setup controller \
     --config "$CONFIG" --source-dir "$PWD" --yes
   uv run inky-bird-frame doctor controller --config "$CONFIG"
   uv run inky-bird-frame status --config "$CONFIG"
   ```

If the restore moves data to different paths, edit the private configuration
before validation. Do not rewrite state-file contents or catalog manifests to
perform a path migration.

## Docker backup

Compose stores application data in three named volumes:

- `controller-data` contains `/data`, including private configuration, approved
  catalog, state, workspace, and optional Bird Buddy authentication;
- `codex-auth` contains Codex authentication; and
- `github-auth` contains optional publication authentication.

The host-side `config.toml`, `controller.env`, and `.env` are also required to
reproduce the deployment. Treat all four controller artifacts as private.

### 1. Stop writers

Run from the directory containing the release bundle. Create the protected
backup directory and record the running long-lived services before stopping
anything. A running `bootstrap` is an active catalog writer; wait for it to exit
and repeat this step instead of interrupting and later restarting it.

```bash
BACKUP=/path/to/private-backup/inky-bird-frame-docker
mkdir -p "$BACKUP"
chmod 700 "$BACKUP"
DOCKER_STATE="$BACKUP/docker-running-services.txt"
if ! docker compose ps --status running --services > "$DOCKER_STATE"; then
  echo "Could not capture running Compose services" >&2
  exit 1
fi
chmod 600 "$DOCKER_STATE"

while IFS= read -r service; do
  case "$service" in
    controller | scheduler) ;;
    bootstrap)
      echo "Bootstrap is still running; wait for it to exit before backup" >&2
      exit 1
      ;;
    *)
      echo "Unexpected running service in $DOCKER_STATE: $service" >&2
      exit 1
      ;;
  esac
done < "$DOCKER_STATE"

docker compose stop scheduler controller bootstrap
docker compose ps --all

STOPPED_STATE="$BACKUP/docker-running-after-stop.txt"
if ! docker compose ps --status running --services > "$STOPPED_STATE"; then
  echo "Could not verify stopped Compose services" >&2
  exit 1
fi
chmod 600 "$STOPPED_STATE"
for service in scheduler controller bootstrap; do
  if grep -Fxq "$service" "$STOPPED_STATE"; then
    echo "Compose writer is still running: $service" >&2
    exit 1
  fi
done
```

Do not continue unless the final loop confirms that `scheduler`, `controller`,
and `bootstrap` are not running. Do not use `docker compose down --volumes`;
that deletes the state being backed up.

### 2. Archive the volumes and host files

Reuse the protected destination from step 1 and archive the controller volume
through the project image. The temporary container runs `tar` instead of the
application and does not start the scheduler. Set `HEALTH_URL` to the published
host port from `INKY_BIRD_PORT`; the default is shown.

```bash
HEALTH_URL=http://127.0.0.1:8793/health
if [ ! -f "$BACKUP/docker-running-services.txt" ]; then
  echo "Missing pre-backup Compose service snapshot" >&2
  exit 1
fi

docker compose run --rm --no-deps -T --entrypoint tar controller \
  -C /data -czf - . > "$BACKUP/controller-data.tar.gz"

cp config.toml controller.env .env "$BACKUP/"
chmod 600 \
  "$BACKUP/controller-data.tar.gz" \
  "$BACKUP/config.toml" \
  "$BACKUP/controller.env" \
  "$BACKUP/.env"
tar -tzf "$BACKUP/controller-data.tar.gz" >/dev/null
```

Reauthenticate Codex and GitHub after restore unless policy requires backing up
their credential volumes. If those volumes are included, archive them through
the scheduler service and protect the result as password-equivalent material:

```bash
docker compose run --rm --no-deps -T --entrypoint tar scheduler \
  -C /home/inky/.codex -czf - . > "$BACKUP/codex-auth.tar.gz"
docker compose run --rm --no-deps -T --entrypoint tar scheduler \
  -C /home/inky/.config -czf - . > "$BACKUP/github-auth.tar.gz"
chmod 600 "$BACKUP/codex-auth.tar.gz" "$BACKUP/github-auth.tar.gz"
tar -tzf "$BACKUP/codex-auth.tar.gz" >/dev/null
tar -tzf "$BACKUP/github-auth.tar.gz" >/dev/null
```

Validate the complete saved service set before restarting anything. Then start
only the existing containers that were running before the backup, in dependency
order. `docker compose start` does not recreate dependencies or rerun
`bootstrap`. Check controller health only when it was previously running.

```bash
DOCKER_STATE="$BACKUP/docker-running-services.txt"
while IFS= read -r service; do
  case "$service" in
    controller | scheduler) ;;
    *)
      echo "Unexpected service in $DOCKER_STATE: $service" >&2
      exit 1
      ;;
  esac
done < "$DOCKER_STATE"

if grep -Fxq controller "$DOCKER_STATE"; then
  docker compose start controller || exit 1
  curl --fail --silent --show-error \
    --retry 12 --retry-connrefused --retry-delay 5 "$HEALTH_URL" || exit 1
fi
if grep -Fxq scheduler "$DOCKER_STATE"; then
  docker compose start scheduler || exit 1
fi

RESTARTED_STATE="$BACKUP/docker-running-after-restart.txt"
if ! docker compose ps --status running --services > "$RESTARTED_STATE"; then
  echo "Could not verify restarted Compose services" >&2
  exit 1
fi
chmod 600 "$RESTARTED_STATE"
while IFS= read -r service; do
  if ! grep -Fxq "$service" "$RESTARTED_STATE"; then
    echo "Recorded Compose service did not restart: $service" >&2
    exit 1
  fi
done < "$DOCKER_STATE"
docker compose ps --all
```

## Docker restore

Restore into a distinct Compose project so the original named volumes remain
untouched until the restored controller passes validation. Run every restore
command with the same project name.

1. Download and extract the same release bundle. Start a new shell in that
   bundle, re-declare the private backup location, then restore its `.env`,
   `controller.env`, and host-side `config.toml`:

   ```bash
   cd /path/to/inky-bird-frame-docker
   BACKUP=/path/to/private-backup/inky-bird-frame-docker
   HEALTH_URL=http://127.0.0.1:8793/health
   cp "$BACKUP/.env" "$BACKUP/controller.env" "$BACKUP/config.toml" .
   chmod 600 .env controller.env config.toml
   ```

   Set `HEALTH_URL` to the controller's published host port from
   `INKY_BIRD_PORT`; the default is shown.
2. Choose a restore project name that does not appear in `docker compose ls` or
   in the matching volume-label query. If the original deployment exists on
   this host, also copy its exact project name from `docker compose ls`; do not
   infer it from the directory or `.env`. Leave `ORIGINAL_PROJECT` empty on a
   migration host. If either query finds the restore name, choose a different
   one. Then create new project volumes without starting services:

   ```bash
   docker compose ls --all
   ORIGINAL_PROJECT=
   RESTORE_PROJECT=inky-bird-frame-restore
   if [ -n "$(docker compose -p "$RESTORE_PROJECT" ps --all --quiet)" ]; then
     printf 'Restore project already has containers: %s\n' "$RESTORE_PROJECT" >&2
     exit 1
   fi
   if [ -n "$(docker volume ls --quiet \
     --filter "label=com.docker.compose.project=$RESTORE_PROJECT")" ]; then
     printf 'Restore project already has volumes: %s\n' "$RESTORE_PROJECT" >&2
     exit 1
   fi
   for volume in controller-data codex-auth github-auth; do
     if docker volume inspect "${RESTORE_PROJECT}_${volume}" >/dev/null 2>&1; then
       printf 'Restore volume already exists: %s\n' \
         "${RESTORE_PROJECT}_${volume}" >&2
       exit 1
     fi
   done
   docker compose -p "$RESTORE_PROJECT" create
   ```

3. Restore the controller archive through the project image:

   ```bash
   docker compose -p "$RESTORE_PROJECT" run \
     --rm --no-deps -T --entrypoint tar controller \
     -C /data -xzf - < "$BACKUP/controller-data.tar.gz"
   ```

4. If credential volumes were deliberately archived, restore them through the
   scheduler service. Otherwise, authenticate Codex and optional GitHub
   publication again.

   ```bash
   docker compose -p "$RESTORE_PROJECT" run \
     --rm --no-deps -T --entrypoint tar scheduler \
     -C /home/inky/.codex -xzf - < "$BACKUP/codex-auth.tar.gz"
   docker compose -p "$RESTORE_PROJECT" run \
     --rm --no-deps -T --entrypoint tar scheduler \
     -C /home/inky/.config -xzf - < "$BACKUP/github-auth.tar.gz"
   ```

5. Run the read-only Docker baseline below before starting services. On a
   same-host restore, stop the explicitly identified original project and
   confirm it is down; the conditional is skipped on a migration host. The
   `-p "$RESTORE_PROJECT"` commands continue to target the isolated restore.
   Start only the restored controller, which runs the one-shot bootstrap first.
   Do not start the scheduler yet:

   ```bash
   docker compose -p "$RESTORE_PROJECT" run --rm --no-deps scheduler \
     config validate --config /data/config.toml
   docker compose -p "$RESTORE_PROJECT" run --rm --no-deps scheduler \
     catalog validate --catalog /data/catalog
   docker compose -p "$RESTORE_PROJECT" run --rm --no-deps scheduler \
     status --config /data/config.toml
   if [ -n "$ORIGINAL_PROJECT" ]; then
     docker compose -p "$ORIGINAL_PROJECT" stop scheduler controller bootstrap
     docker compose -p "$ORIGINAL_PROJECT" ps --all
   fi
   docker compose -p "$RESTORE_PROJECT" up --detach controller
   docker compose -p "$RESTORE_PROJECT" run --rm --no-deps scheduler \
     catalog validate --catalog /data/catalog
   docker compose -p "$RESTORE_PROJECT" ps
   curl --fail --silent "$HEALTH_URL"
   ```

Bird Buddy permits one current rotating refresh token. `birdbuddy status` reads
only the restored local state and does not rotate that token. The authenticated
`discover` in the next step does rotate it, so the original project's saved
session becomes stale even though its volume is untouched. Keep the original
controller stopped. If you return to it after validating Bird Buddy on the
restored copy, run an explicit `birdbuddy login` there; do not query Bird Buddy
from both controllers.

6. Complete the deliberate provider check below through a one-off container and
   inspect the command's JSON output. It does not replace the saved observation
   snapshot. After the restored catalog, state, provider result, and controller
   health are correct, enable scheduled work:

   ```bash
   docker compose -p "$RESTORE_PROJECT" run --rm --no-deps scheduler \
     discover --config /data/config.toml
   docker compose -p "$RESTORE_PROJECT" up --detach scheduler
   docker compose -p "$RESTORE_PROJECT" ps
   ```

7. If the restored project becomes the live deployment, add
   `COMPOSE_PROJECT_NAME=<the chosen restore project name>` to the private
   `.env` so future unprefixed Compose commands target these restored volumes.
   Keep the original project and volumes until the new deployment has passed
   the full validation and an operator has deliberately retired the old copy.

Do not restore one application-state directory into a different volume layout
or combine state from separate controllers.

## Validate a restore

Use the same application release that created the backup for the first
validation. The native and Docker procedures above run configuration, catalog,
and status checks before starting scheduled writers. `status` is read-only; it
does not rebuild the catalog or change controller state.

When Bird Buddy is enabled, also run `birdbuddy status --config
/path/to/config.toml` to inspect its local attestation, selected feeder, and
history. This does not contact Bird Buddy or prove the token remains valid.
When eBird Archive is enabled, run `ebird archive status --config
/path/to/config.toml` and compare its aggregate counts and date range with the
pre-backup record. Use `/data/config.toml` and the same Compose prefix for those
commands in Docker.

After the read-only baseline and HTTP health pass, run `discover` once as a
deliberate live provider check and inspect that command's JSON result. Discovery
does not replace the saved observation snapshot. For Bird Buddy it does detect
a revoked session and rotate the saved refresh token. `refresh`, including the
immediate refresh triggered by native setup, is the explicit persisted-state
transition. Enable native setup or the Docker scheduler only after the live
provider result is correct.

Confirm that:

- the catalog validates with the expected species count;
- collection, approved, queued, deferred, and failed counts are plausible;
- configured providers complete or return an actionable, redacted error;
- the display can fetch and show an approved plate; and
- notification and publication credentials are reauthenticated or deliberately
  restored before those optional jobs are enabled.

Only after this baseline passes should you follow the normal release upgrade
procedure.

## Private-data lifecycle and removal

Disabling a provider stops future collection but does not silently delete its
history. That makes the change reversible and prevents a configuration typo
from erasing data.

- Remove a provider from `discovery.sources` to stop querying or reading it.
- `birdbuddy logout --yes` removes local Bird Buddy authentication and
  authorization-attestation state. It deliberately preserves accumulated
  detection history.
- `collection remove TAXON_ID --dry-run` and the same command without
  `--dry-run` remove one species from persistent local membership; they do not
  delete its reusable approved plate.
- eBird Archive, BirdNET Analyzer, and Bird Buddy history remain in their named
  mode-`0600` state files until the operator deliberately removes that private
  state.

There is no safe selective command for completely purging one provider. Removing
only its named history file is **provider-owned history removal**, not complete
erasure: derived state such as `discovery.json`, `active-catalog.json`,
`generation-queue.json`, `collection.json`, `generation-retries.json`,
`profiles/`, `runs/`, `pending/`, `failed/`, `rejected/`, and `workspace_dir`
may still reveal species, counts, timestamps, research, or generated artifacts.
Once observations have been merged, shared derived state cannot be attributed
safely to one provider. Do not hand-edit those derived JSON files.

The selective removal path requires at least one configured provider that you
intend to keep. Inky rejects an empty `discovery.sources` list, and deleting the
setting selects the default iNaturalist source rather than disabling discovery.
If the provider being removed is the only source and no replacement is intended,
keep scheduled work stopped and use the full state-and-workspace reset below. Do
not restart the controller until a valid replacement source is configured.

For provider-owned history removal, first disable the provider, quiesce every
writer, and make a separately protected recovery backup if policy permits. The
history files are `ebird-archive-observations.json`,
`birdnet-analyzer-detections.json`, and `birdbuddy-detections.json`. Use
`birdbuddy logout --yes` for Bird Buddy authentication rather than editing or
selectively deleting fields from `birdbuddy-auth.json`. Remove the corresponding
`*-taxonomy-crosswalk.json` file too when cached species-resolution history is in
scope. Remove only the intended files with normal filesystem administration,
then run one successful foreground refresh while scheduled writers remain
stopped. This rebuilds `discovery.json` and `active-catalog.json` through the
controller's normal locks; it does not erase the other retained state listed
above.

```bash
# Native managed runtime
IBF="$HOME/Services/inky-bird-frame/.venv/bin/inky-bird-frame"
"$IBF" refresh --config /path/to/config.toml

# Docker Compose, from the release bundle
docker compose run --rm --no-deps scheduler refresh --config /data/config.toml
```

Inspect the refresh result and verify the expected missing-history or logged-out
state before restarting scheduled work or disposing of the recovery copy.

If policy requires clearing all controller observation and generation state,
keep writers stopped and replace both the private `state_dir` and `workspace_dir`
with fresh empty directories. This removes collection membership, queues,
retries, provider imports, Bird Buddy authentication, notification history,
research, and retained run or failure evidence. It does **not** remove private
`config.toml` values, Codex or GitHub authentication stores, service logs,
backups, or snapshots. Remove unwanted providers and credentials from the
private configuration before refresh; any enabled live source can repopulate
the fresh state. Only after the replacement configuration validates, reauthorize
or reimport the sources you intend to keep, perform a controlled refresh, and
verify the new state before discarding the protected recovery copy. Do not copy
selected derived files back into the fresh directories.

Deleting live state does not remove copies from backups, snapshots, or remote
storage. Apply the same retention decision to every copy. Public catalog plates
are location-neutral and contain no account or observation history, so private
history removal does not require deleting reusable catalog art.
