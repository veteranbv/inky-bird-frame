# Browser journal: development candidate

The browser uses your controller's approved art and private selections. It does
not run another discovery service or keep a second copy of your history in
browser storage. Open `/app` and [set up your owner account](browser-access.md).

This is an unfinished 1.0 candidate, not a published release. Do not replace a
working deployment with this branch. The complete acceptance scope remains in
[issue #310](https://github.com/veteranbv/inky-bird-frame/issues/310).

## What works in this checkpoint

- Home opens the current approved selection. If it is empty, it offers available
  artwork without pretending you observed those birds.
- Birds separates your current/saved selections from all available artwork.
  Search by common or scientific name. Open a plate for readable reviewed facts
  and source links. Older incomplete profiles show an explicit fallback.
- Collections creates, edits and removes private named selections. Reloading the
  page reads the saved controller state. A stale edit from another tab fails
  before it overwrites a newer save.
- Favorites are separate bookmarks. Add or remove one on a bird's page. They
  do not join Saved birds or change your wall automatically.
- Portable collection export includes labels, inclusive dates, species and
  declared origins, but no private checklist links or credentials. Import
  previews matches and keeps unknown names unresolved. It does not turn an
  imported label into verified personal evidence.
- [History imports](history-imports.md) checks official eBird archives and
  Analyzer CSV files before a separate confirmed history update. Trip saving
  selects complete checklists, retains unresolved names and changes neither
  generation work nor wall selection.
- Frame shows complete portrait plates without cropping. Play changes plates
  once a minute. Previous, Next, Pause, Details and Exit remain available. Reading
  details stops playback. Fullscreen depends on browser support.
- Manage shows all seven source types, current work partitions, notification
  counts and the last confirmed physical panel update. Existing configuration
  stays managed. [App-managed settings](settings.md) add source and policy edits
  with preview, private backup and stale-edit protection.
- New setup can start with approved art, no sources and generation off. Native
  service definitions, the dynamic scheduler and doctor understand that mode.
- [Owner actions](owner-operations.md) provide confirmed source refresh, normal
  plate retry, notification tests and dead-letter retry with private outcomes.
- Bird Buddy's source setup includes owner-only guest login, explicit permission
  attestation, feeder choice, redacted saved-login status and local logout.
  Bootstrap needs HTTPS or genuine loopback access. It stores no email or password.

Named selections do not write `collection.json`, request generation or change
the wall. Existing CLI collection commands still mean Saved birds. Current and
saved keeps the existing active-catalog meaning. Available artwork is not a
list of personal sightings. Missing plates remain visible in selected groups.

Frame never records a successful physical panel update. It checks its view and
authority once a minute without renewing owner idle time. A successful empty
view stays empty. A network failure keeps the last approved image with an
offline message. When the owner session ends, Frame clears private labels and
controls, freezes the last visible approved image and asks for sign-in. It is
not yet a continuously paired read-only screen.

## Private state and recovery

Named selections live in `state_dir/journal.json`, schema version 2. The file
contains a revision and groups with opaque IDs, names, kinds, inclusive dates
and typed members. Personal trip members retain hashed checklist links and
their original source scientific names. Source corrections can mark evidence
as changed without silently editing your curated membership.
Its mode must be `0600`. Inky rejects a symlink, insecure permissions, invalid
state or a file larger than 1 MiB. Individual edit requests retain the existing
8 KiB owner JSON bound. Portable import and trip selection allow up to 1 MiB
plus the small request envelope. Collection names accept 1–128 visible Unicode characters.
These are storage and request bounds, not truncation rules. Oversized changes
fail without committing a partial selection.

Writes use the existing catalog-state lock and atomic private replacement.
Two tabs cannot silently replace each other's edits. Removing a group does not
remove source history, approved plates or Saved birds. Group names can reveal
travel or personal interests. Keep the file and its backups private.

Version 1 selections remain manual selections, not inferred personal trips.
Reading them does not migrate or write state. The first edit creates an exact
private `journal-v1-backup.json`, then atomically writes version 2. Do not feed
that new schema to an older candidate. Use its matching backup and runtime.

Use a stopped, consistent [controller backup](backup.md), not a live ZIP of
the state folder. An older release ignores this new file, but that does not
prove every newer state file is backward compatible. Roll back with the old
runtime and its matching complete backup.

## Still required before the full 1.0 candidate

The paths above do not stand in for these remaining acceptance gates:

- complete live qualification of generation-optional installation and ownership
  migration across native and Docker service restarts;
- final large-file, mobile and live qualification of history import paths;
- complete pending-candidate recovery and live qualification of
  source/work/notification actions;
- separately paired read-only screens, selected wall routing and scoped
  Homepage/Home Assistant views and service credentials;
- complete new-install and upgrade journeys, final mobile/browser qualification,
  independent security review and repository review on the final exact head.

No button in this checkpoint claims to perform one of those unfinished tasks.
Provider access remains subject to each service's permissions and limits.
