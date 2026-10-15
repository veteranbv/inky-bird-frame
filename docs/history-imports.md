# History imports and personal trips

This guide covers the unfinished browser candidate. The release is not yet
qualified for production. The [CLI import paths](discovery.md) remain available.

## Your eBird checklists

Open **Manage → Sources → eBird Archive → Import or review history**. You can
also start from **Collections → Make a trip from your eBird checklists**.

1. Choose the complete official Download My Data ZIP or CSV. Do not cut the
   archive down to one trip: this import replaces retained eBird history.
   If history is already current, choose **Use retained history without an
   upload**. That path has no original location labels.
2. Check the file. Inky validates all rows and shows additions, corrections,
   removals and affected collections. Checking does not update history.
3. Review the history changes and generation policy. Confirm the history update
   separately. An unchanged archive needs no history write. Removing records or
   invalidating a trip's evidence requires explicit acknowledgment.
4. Choose inclusive calendar dates and individual checklists. No checklist is
   selected for you on a new trip. Location names come from the private staged
   file; Inky does not save them in the collection. Review matched, ready and
   unresolved species. The discovery species limit does not truncate this trip.
5. Save the trip. It has fixed membership. Saving does not add Saved birds,
   queue plates, enable generation or change the wall.

Inky labels personal evidence only when a retained checklist still supports the
species inside the trip's original dates. A later removal, reclassification or
date correction can show **Source record changed**. Your curated membership
stays intact. Birds added through ordinary collection editing are manual
selections, not invented personal sightings.

Imported history can affect an already enabled source at its next refresh.
The preview shows names entering or leaving that source's date window, before
provider limits and cross-source matching. Those are not individual birds or
a promise that every name will become generation work. Inky shows whether the
source and automatic generation are enabled; an import enables neither.

## Analyzer detections

Open **Manage → Sources → BirdNET Analyzer → Import or review history**. Choose
an Analyzer CSV file. If you know the recording date, provide it for that batch.
Otherwise leave it blank. Unknown dates stay unknown and cannot enter finite
date windows. Review new, changed, duplicate, dated and undated records before
confirming. Imports preserve the existing segment deduplication and conflict
rules. They do not control the detector or download recordings.

## Interrupted work

An [operation receipt](owner-operations.md) survives browser reloads. Inky can
prove a completed history write from a marker in the same atomic history file.
It does not infer success from an uploaded filename or an in-memory flag.

**History updated. Trip not saved yet** means exactly that. Resume checklist
selection while the staged file remains available. Cancelling at this point
removes only the temporary file; confirmed history stays. The history import
and collection save are not one transaction.

If a different tab changes the journal, trip saving refuses the stale edit.
Choose **Check saved state → Edit trip selection**, review the current
checklists, then preview again. The staged file and your earlier choices stay
available. A failed network response does not replay work automatically.
Use the saved outcome or check Collections before making a new trip.

## Private files, retention and backup

Only one import preview can exist at a time. Raw upload bytes and preview state
live under `state_dir/owner-import/`, mode `0700`; files use `0600`. They can
contain private locations and travel history. Treat controller backups as
private. No raw checklist ID, location, credential or source file enters the
reusable public catalog or portable collection export.

The browser wire limit is 512 MiB. eBird ZIP contents have a separate existing
512 MiB decoded CSV limit. These are rejection bounds, not measured promises
that every maximum-size archive is practical on every controller. An offline
synthetic test checked 900,000 rows in 90,000 checklists: 509.7 MiB decoded CSV,
4.1 MiB ZIP, and 108.3 MiB resulting private history. Complete checking and
commit took about 22 seconds and peaked at 731 MiB of process memory on the
test Mac. A smaller 100,000-row case peaked at 116 MiB. These are measured
input shapes, not speed or memory guarantees. Unique records and names can
use more memory than repeated rows. Keep headroom beyond the import process
for the serving application and other services; the smallest recommended
controller still has 4 GB RAM. Proxy upload limits and timeouts must also
permit the complete source file.

Inky requires 2 GiB free before admitting an upload: four upload ceilings leave
headroom for the source, replacement history, previous history and parsing.
Only one foreground upload or owner operation can use the reserved slot.
An upload times out after 30 minutes; it cannot block the slot indefinitely.

Preview retention defaults to 24 hours. Set `owner_import_retention_hours` in
`[controller]` to 1–168 hours; restart after changing it. The maximum matches the
seven-day receipt recovery period. While the service runs, cleanup removes
expired temporary files within one minute, unless an operation is using them.
It retries after that operation. If the service is stopped, cleanup runs at its
next start. It never removes committed history or saved collections. Signing
out removes uncommitted staging; an already confirmed commit can finish.

Before replacing existing history, browser imports save one exact private
`.json.previous` backup. That file is not a full controller rollback. Follow
the [stopped-controller backup and restore guide](backup.md) for a consistent
runtime, state, configuration and catalog restore.
