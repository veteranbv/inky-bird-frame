# Owner actions and saved outcomes

This describes the 1.0 browser candidate. Implementation and local checks are
not a release or production-deployment claim.

Sources offers **Refresh observations**. Plate work accepts a selection of up
to 32 birds for retry. Notifications offers a real delivery test
and, when needed, retry of existing dead letters. Each action has a separate
confirmation. Host repair, advanced correction-source selection and replacement
of approved art remain operator workflows.

A refresh uses the same controller operation and notification bookkeeping as
the CLI. Partial provider failures remain isolated. Existing generation policy
can act on newly eligible birds later. A refresh does not start a model itself.

A browser retry saves a durable request without waiting for the generation
lock. The next generation cycle prepares requests in order, using the CLI's
typed recovery function to retain correction guidance and archive earlier work.
If discovery is missing, stale or from older source settings, requests stay
queued without changing their recovery state. They continue after a usable
refresh; you do not need to submit them again.
Repeated pending requests for the same bird are deduplicated. Approved artwork
is not replaced, and complete pending candidates still require review.

Choose **Queue selected for the next scheduled check** to wait for the normal
schedule. **Retry selected as soon as possible** asks the existing scheduler to
refresh sources and check generation after its current job finishes. This action
is available only while generation is enabled and Inky's scheduler is running.
Installations that use separate system timers can use the scheduled queue.
Neither action starts a model in the web service or interrupts an active plate.
Generation must be enabled, discovery must be healthy, and ordinary attempt,
research and sourced-review limits still apply. Generation can use your AI
allowance or incur costs.

Plate work updates automatically. It separates the active cycle and stage,
runnable order, automatic backoff, explicit holds, pending review and saved
retry-request outcomes. A next-check time is a scheduler observation, not a
promised start or finish time. Recorded failures show safe categories and, when
available, retained attempt counts and numeric review scores. Raw provider or
model errors stay in private operator evidence.

Sources also updates automatically. Each card separates checking now, idle,
failed and unknown activity. First recorded run, last start/finish and last
success come from recorded checks. Earlier first-run history is unknown; no
timestamp is inferred from file creation. A stale heartbeat is not shown as
running. Recent detections and reports show the latest dated record per species
in the selected window, up to 100 species. They are not a continuous event
stream or proof that a station is recording. Imported archives remain separate.
If discovery merged several providers, the latest timestamp does not identify
which provider supplied it. The UI labels that timestamp as merged and omits it
from provider-specific detection times rather than inventing provenance.

Named collections also offer **Review missing-plate requests**. The preview
separates approved art, existing requests, holds and unresolved identities. Confirming
adds only new requests to the existing queue. It does not add sightings, join
Saved birds, clear a hold, start a model or change the wall.

Complete pending candidates open for private inspection. The browser shows the
uncropped plate, readable facts, saved review scores and cited sources. Local
approval requires the same mandatory publication validation as the catalog and
applies only to the exact files inspected. Changed files need a new review.
Approval can make art eligible for the existing publisher; published copies
cannot be recalled. Rejection requires a reason and retains the artifact privately.
It does not start another attempt. Incomplete assets remain visible as recovery
work with operator guidance, not a false empty queue.

Saved birds has explicit add/remove and import-approved-art previews. The
confirmation binds both the configuration and the saved-membership/catalog
revision. Another tab or a newly approved plate invalidates an old preview.
These actions use the existing collection operation, including its compatible
legacy seed migration, and can change the active catalog and wall. The preview
shows that migration separately. It never claims a personal sighting or starts
a model. Named collections and Favorites remain separate.

A notification test sends an actual external message to each configured
destination. Its outcome reports failed destinations separately from successful
deliveries. A dead-letter retry uses the existing outbox and preserves its
per-destination delivery history. A completed operation does not mean that every
destination accepted a message.

## Browser disconnection and restart

Inky writes a private receipt before starting work. The receipt moves through
pending and running to succeeded or failed. You can reload its outcome page or
open Recent owner operations without repeating the request. The browser sends a
new random request identifier for a deliberately new action. Reusing an
identifier with the same choices returns the original operation. Different
choices with that identifier fail.

Only one owner operation can run at once, including across service processes.
There is no unbounded waiting queue for arbitrary operations. The separate,
bounded retry-intent list feeds the existing generation queue at a safe cycle
boundary. Scheduled CLI cycles retain their state and cycle locks. The worker
never accepts shell text, executable paths or a model command.

If the service stops before saving a confirmed outcome, the receipt becomes
**interrupted**. Inky does not infer success or automatically replay the action.
History imports are the narrow exception: a matching commit marker stored in
the same atomic history write proves that step succeeded. The receipt can then
report the confirmed history outcome, not a successful trip save. See
[history imports](history-imports.md) for the separate confirmation steps,
source-file retention and trip recovery.
Inspect the source, work or notification state before an explicit retry.
Generation-off mode, review holds and existing retry rules remain in force.
An interrupted receipt can coexist with an already-saved retry intent. Check
Plate work before submitting again. If retry preparation itself was interrupted,
Inky holds the species for inspection rather than replaying archive operations
or starting generation from ambiguous state. A new explicit request can resolve
that hold after inspection.
Successful operator CLI recovery also clears the matching browser hold. A
malformed retry-state file still needs operator repair; generation fails closed
without stopping unrelated scheduler jobs.

Each action validates and uses one captured configuration. If that file has
already changed when the worker loads it, Inky refuses the old request. A later
save cannot redirect an in-flight action to a different source or destination.
Reload and review a refused action again. Environment-backed credentials must also
be available to the browser service for manual source refresh or delivery. A
managed deployment that injects those credentials only into the scheduler can
continue its scheduled work, but cannot claim browser tests have access to them.
Configure that service environment through the operator workflow. Never copy a
vault service token into Inky.

## Private storage and limits

Receipts live in `state_dir/owner-operations.json` with mode `0600`, under the
existing catalog-state lock. A separate private operation lock prevents two
workers from running at once. Symlinks, insecure permissions and malformed state
fail closed. Raw exceptions, server payloads, secrets and bootstrap credentials
are not saved in receipts or shown in operation outcomes.

Completed and interrupted receipts are retained for seven days. That gives time
to inspect an interrupted task after a weekend. Storage is bounded to 256
receipts and 1 MiB of serialized state; a single result is bounded to 16 KiB.
Full storage refuses new actions instead of evicting active work. Cleanup removes
only expired receipts, never source history, generation work or plates. An
expired identifier is not a permanent exactly-once guarantee.

Back up receipts with the rest of the stopped controller state. A receipt is
evidence of an operation outcome, not a replacement for its source data or a
complete rollback backup.

Retry intents live in `state_dir/retry-requests.json`, bounded to 256 records;
completed records can be evicted, unresolved blocked requests cannot. A new
explicit request replaces that bird's prior outcome while retaining its hold
until preparation completes. Activity lives in
`state_dir/activity.json`. Both files use atomic mode-`0600` writes and private
locks. Activity is advisory; it does not authorize work. Back up both with the
rest of controller state. Removing a receipt does not cancel an accepted retry.
