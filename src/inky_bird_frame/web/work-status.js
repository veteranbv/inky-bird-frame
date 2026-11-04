// Owner-only presentation of recorded activity. Never infer a live connection
// from a successful saved snapshot, or a promised start time from eligibility.
export function sourceState(source, complete) {
  if (!source.enabled) return 'Off';
  const state = source.activity?.status;
  if (state === 'running') return 'Checking now';
  if (state === 'unknown') return 'Current activity unknown · last heartbeat is stale';
  if (state === 'failed') return 'Last check failed · needs attention';
  if (state === 'succeeded') return complete ? 'Idle · last check succeeded' : 'Idle · saved results are stale';
  if (source.status === 'error') return 'Last saved check failed';
  if (source.status === 'ok') return complete ? 'Last saved check succeeded · activity not yet recorded' : 'Saved results are stale';
  return 'No recorded check';
}

export function nextCheck(job, enabled = true, now = Date.now()) {
  if (!enabled) return 'Generation is off; no attempt is scheduled.';
  if (job?.status === 'running') return 'A cycle is running. New requests wait behind it.';
  const date = Date.parse(job?.next_run_at);
  if (!Number.isFinite(date)) return 'Next scheduler check is not recorded yet.';
  if (date < now) return 'Last recorded check time has passed; a start is not confirmed. The scheduler may be busy or stopped.';
  return 'Next scheduler check: ' + new Date(date).toLocaleString() + '. This is not a promised start or finish time.';
}

export function requestState(request, work) {
  if (request.status === 'approved') return 'Approved · no regeneration';
  if (request.status === 'queued') return 'Queued for safe retry preparation';
  if (request.status === 'preparing') return 'Preparing retry between generation cycles';
  if (request.status === 'blocked' && request.outcome === 'pending_review') return 'Inspect the existing pending candidate. Approve or reject it before requesting retry.';
  if (request.status === 'blocked' && request.outcome === 'retained_state') return 'Retained identity or correction evidence needs operator inspection; it was not discarded or bypassed.';
  if (request.status === 'blocked' && request.outcome === 'not_retryable') return 'No permitted failed, rejected or deferred state could be prepared. Check the current plate and operator evidence before requesting again.';
  if (request.status === 'blocked') return request.outcome === 'interrupted'
    ? 'Preparation was interrupted. Inspect retained state before submitting a new request; it was not replayed.'
    : 'Preparation needs attention. A pending candidate, missing identity, or invalid correction evidence may need operator review. No review guard was bypassed.';
  if (work.needs_attention?.includes(request.taxon_id)) return 'Preparation completed; a later attempt is held. See its error below.';
  if (work.waiting?.includes(request.taxon_id)) return 'Preparation completed; automatic retry is waiting on backoff.';
  return 'Preparation completed · eligible under the current policy';
}
