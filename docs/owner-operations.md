# Owner actions and saved outcomes

This describes the unfinished browser candidate. It does not claim that all
planned import, trip or display actions are available yet.

Sources offers **Refresh observations**. Plate work offers the existing normal
retry for waiting or stopped species. Notifications offers a real delivery test
and, when needed, retry of existing dead letters. Each action has a separate
confirmation. Host repair, advanced correction-source selection and replacement
of approved art remain operator workflows.

A refresh uses the same controller operation and notification bookkeeping as
the CLI. Partial provider failures remain isolated. Existing generation policy
can act on newly eligible birds later. A refresh does not start a model itself.

A plate retry makes another attempt eligible. It retains correction guidance and
archives earlier work through the same typed recovery function as the CLI. It
does not start an immediate generation or bypass review. Pending complete
candidates and approved plates do not become ordinary retries. Read the existing
work state before confirming a retry; enabling generation can use AI allowance
or incur costs.

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
There is no unbounded waiting queue and no second generation queue. Scheduled
CLI cycles still use their existing state and cycle locks. The worker never
accepts shell text, executable paths or a request to run a model.

If the service stops before saving a confirmed outcome, the receipt becomes
**interrupted**. Inky does not infer success or automatically replay the action.
History imports are the narrow exception: a matching commit marker stored in
the same atomic history write proves that step succeeded. The receipt can then
report the confirmed history outcome, not a successful trip save. See
[history imports](history-imports.md) for the separate confirmation steps,
source-file retention and trip recovery.
Inspect the source, work or notification state before an explicit retry.
Generation-off mode, review holds and existing retry rules remain in force.

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
