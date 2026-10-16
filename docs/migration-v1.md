# Moving an existing controller to the browser journal

This is the migration contract for the 1.0 candidate, not an instruction to
deploy an unreviewed branch. Wait for the qualified release. Keep the exact old
runtime and a matching complete private backup until the new installation passes
your checks. No migration requires a public account, reverse proxy or e-paper frame.

## What stays the same

- Existing source choices, credentials, location, windows and schedules remain.
- Existing generation policy remains. Upgrade never enables AI generation.
- Saved birds keeps its current membership and CLI meaning.
- Approved plates remain immutable unless an explicit reviewed catalog migration
  already accompanies the release.
- `/v1/catalog`, active assets and the physical display's controller URL keep
  their compatible meaning. The wall does not opt into a new collection.
- Existing configuration remains managed by its TOML file and environment.
- HTTPS-only installations remain HTTPS-only. Single sign-on is optional.

The journal adds private named collections, Favorites, typed trip links, owner
access, saved operation outcomes and optional presentation grants. Downloading
public artwork does not claim a sighting or join your Saved birds.

## Before updating

1. Record the running release, configuration ownership and active services.
2. Check controller and display doctor. Resolve storage trouble first.
3. Follow [Backup and restore](backup.md). Stop serving and scheduled writers
   before the consistent copy; do not back up a live state folder as a ZIP.
4. Keep configuration, catalog, private state and working data with the matching
   runtime. Backups may contain provider tokens and private bird/travel history.
5. Confirm a working local recovery route before changing authentication.

## Native macOS or Linux

Follow the reviewed release's [native update procedure](installation.md#update).
Use the same configuration path and installation role. The installer preserves
managed ownership. Run setup's preview first, inspect the service changes, then
apply. Do not start a second scheduler alongside existing one-shot timers.

After updating, run both doctors. Existing owners keep their account. If none
exists, native interactive setup asks you to create one locally, with hidden
password entry. If you skipped that step, run `inky-bird-frame access setup
--config <config.toml>` in a private terminal. Then open the actual controller
address at `/app` and sign in. Restart the service and check the same account,
collections and source settings again. The browser needs no provider reconnection
or history reimport merely because you upgraded.

## Docker or a NAS

Follow [Docker update](docker.md#update), using the reviewed image tag or digest
and the existing persistent volume. Keep a private copy of the old image reference
and the stopped volume backup. A fresh Compose volume is a new installation,
not an upgrade of your existing birds.

Review and adopt the target release's Compose changes as well as its image.
Preserve your private files and local overrides. An image-only update cannot add
the new controller-side provider or optional SSO environment wiring. It keeps
the older scheduled path running; it does not replace this migration step.

Managed `config-sync` continues to validate and install the host configuration.
An app-managed runtime file is deliberately preserved on later starts. Do not
restore only the original host seed after browser edits; back up the authoritative
runtime file in the persistent volume. Both serving and scheduled operations
need access to any applicable configured environment secrets. Inject only the
specific provider/destination values, never a vault service-account token.

If this is the first version with an owner account, run this after startup:

```bash
docker compose exec controller inky-bird-frame access setup --config /data/config.toml
```

Do not run new-installation preparation on an upgrade. A pull of `:latest` or
another unattended upgrade does not pause for account entry. Existing display,
catalog and scheduled operations continue; only new owner controls remain
locked. The browser shows local setup instructions rather than letting a
visitor claim the installation. Account setup does not enable generation or
change your configuration ownership. See [access and recovery](browser-access.md).

## Choose configuration ownership deliberately

You may keep file management indefinitely. Locked browser fields explain which
operator-owned file or environment value to change.

To adopt app management, use its separate preview and confirmation in Settings.
Inky keeps the exact previous private file and preserves source/generation policy.
Rerun native setup and restart to replace fixed timers with the dynamic scheduler.
Browser edits remain locked until that transition finishes. Docker config-sync
preserves the adopted runtime file. See [Settings](settings.md).

Port binding, process identity, certificates, trusted proxies, OIDC and package
updates stay installer-owned in either mode. No browser setting can safely
pretend to perform those host changes.

## Check the result

Use real saved state, not just a green HTTP response:

1. Compare configured providers and generation policy with the pre-upgrade record.
2. Compare Saved birds and approved catalog counts. Open several actual plates.
3. Check retained history coverage. A library may now show birds outside the
   live window; that does not make them new generation work.
4. Open a personal trip and inspect its evidence labels. Imported collections
   without local checklist links must not claim verified personal sightings.
5. Create a small named selection, reload it, then restart the controller and
   confirm it remains. It must not silently join Saved birds or change the wall.
6. Inspect Sources, Plate work and Notifications separately. Old failed artifacts
   are not automatically runnable backlog.
7. Verify a scheduled discovery cycle, service restart recovery and a real panel
   success through the existing doctor and telemetry paths.
8. If using advanced access, test local recovery, exact SSO identity rejection,
   screen revocation and status credential revocation before relying on them.

## State versions and rollback

Journal schema 1 is read without rewriting it. Its first deliberate edit keeps
an exact mode-`0600` `journal-v1-backup.json`, then writes schema 2. Schema 2
retains explicit member origins and the minimum hashed checklist evidence needed
to detect later source changes. Reading old manual choices never turns them into
personal trips.

Newer state and older runtimes are not interchangeable. For a failed trial,
stop the candidate, restore the matching stopped backup and old runtime, restore
only the services that were previously active, and run the doctors again.
Do not restore a single old JSON file over a live newer controller. Collection
export is useful for a selection, but is not a full rollback.

You can stop a new presentation feature without a runtime rollback: revoke its
screen/status credential, turn off dashboard art, or restore the physical display's
previous controller URL. None of these deletes history or plates.
