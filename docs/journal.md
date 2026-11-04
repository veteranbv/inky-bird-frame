# Browser journal

The browser uses your controller's approved art and private selections. It does
not run another discovery service or keep a second copy of your history in
browser storage. Open `/app` and [set up your owner account](browser-access.md).

This guide describes the app on `main`. Start with [approved art](settings.md#start-with-approved-art)
if you want to try it without generation, connected sources, or a physical display.

## Find your way around

The tabs sit at the bottom on phones and at the top on wide screens. If you
already run Inky, follow the [migration guide](migration-v1.md) to keep your
configuration and data.

- **Home** opens your current selection of approved plates. If it is empty,
  you can still browse the available artwork.
- **Birds** lets you search by common or scientific name. Open a plate to read
  its reviewed facts and sources. Older plates with incomplete profiles say so.
- **Collections** keeps named selections and puts the art before the tools.
  The missing-plate review appears when a collection needs it.
- **Frame** plays complete portrait plates in your browser, one a minute.
  For a shared screen, [pair a read-only view](presentation.md#pair-a-read-only-screen).
- **Manage** shows sources, plate work, notification counts and the last
  confirmed panel update. Use [owner actions](owner-operations.md) to refresh
  a source, retry plate work or test notifications.

### What you keep and what you saw

Available artwork is yours to browse. It isn't a list of personal sightings.
Birds separates the artwork from retained history and the current and saved
selection. Each source keeps its own counts: eBird checklists and station
detections stay separate. Retained eBird, Analyzer and selected-feeder Bird
Buddy history can go back beyond the live window and species limit. Unknown
or ambiguous names stay private and unmatched.

Add a Favorite from a bird's page, or save birds to a named collection for
playback. Neither changes the default wall selection, writes `collection.json`
or queues generation. The optional [selected wall view](presentation.md#choose-the-physical-wall-view)
is configured separately. Missing plates stay visible in the collection, and requests for
art have their own preview and confirmation. Reload to see what the controller
saved. If another tab saved first, Inky rejects your stale edit.

**Saved birds** is the existing shared list managed by the CLI collection
commands. **Current and saved** combines that list with currently observed species for
the active catalog. Preview changes to Saved birds before you confirm them:
they can affect the active selection and physical wall.

Portable collections carry labels, dates, species and declared origins.
Private checklist links and credentials stay out. Import previews matches and
leaves unknown names unresolved. For personal records, [import an eBird
archive or Analyzer results](history-imports.md) through the separate history
flow. Saving a trip selects complete checklists and changes neither generation
work nor the wall selection.

For a place you want to explore, use **Birds reported near this place**. The
search sends the location and dates to iNaturalist. Review the species before
saving; if the search finds over 500 species, narrow it and try again. These
community reports stay separate from your own sightings.

### Playing plates and managing your setup

In Frame, **Details** pauses playback. **Zoom** lets you pan around the image
without changing the plate. In fullscreen the title and notes disappear.
Controls fade between interactions, and **Bird name** toggles a small placard.
Fullscreen and wake lock depend on the browser. See [Frame and screen
setup](presentation.md#owner-frame) for display options.

[App-managed settings](settings.md) let you preview source and policy changes
and keep a private backup. Existing file-managed installations keep their
configuration. Follow [discovery setup](discovery.md) for Bird Buddy permission
and login requirements. In Manage, **Displays** pairs screens and selects the
wall view. **Integrations** has scoped artwork and separate status credentials
for Home Assistant and Homepage. **Health** shows redacted checks. Backups
and advanced tools explain what to do on the controller; host commands stay
with the operator.

Frame never records a successful physical panel update. It checks its view and
authority once a minute without renewing owner idle time. A successful empty
view stays empty. A network failure keeps the last approved image with an
offline message. When the owner session ends, Frame clears private labels and
controls, freezes the last visible approved image and asks for sign-in. Paired
screens have separate expiry and revocation; they never renew owner authority.

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

## Verification and release boundary

For contributors: browser tests cover the flows below. Release qualification
also needs evidence from real phones, providers and hardware.

The maintained browser acceptance test is
[`tests/web/journal.e2e.cjs`](../tests/web/journal.e2e.cjs). It runs a temporary
controller with random credentials, synthetic history and public approved art.
It covers normal tasks, stale edits, interrupted responses, explicit work,
scope withdrawal and revocation. Its community query is deliberately mocked;
it is not a live iNaturalist contract test. See [Contributing](../CONTRIBUTING.md#full-browser-acceptance).

Chromium, Firefox and WebKit journeys and automated accessibility checks are
separate from physical-phone, hardware and live-provider qualification. A narrow
viewport is not proof of a particular phone. The README screenshots use two
public approved plates and manual selections in a fresh art-only demo, not
personal history. Refresh those images from the actual interface; never use
private controller screenshots as convenient documentation fixtures.

The following remain release gates, not automatic claims from a passing test:

- complete live qualification of generation-optional installation and ownership
  migration across native and Docker service restarts;
- final large-file, mobile and live qualification of history import paths;
- live qualification of source/work/notification actions, paired screens,
  selected wall routing and scoped dashboard permissions;
- complete new-install and upgrade journeys, final mobile/browser qualification,
  independent security review and repository review on the final exact head.

No release or production deployment is implied by these implementation notes.
Provider access remains subject to each service's permissions and limits.
