"""Fail CI when Codex has flagged unresolved blocking-severity findings.

Codex posts findings as PR review comments rather than as CheckRuns, so
GitHub's standard `required_status_checks` mechanism cannot gate on those
findings directly. This script bridges that gap.

Severity markers (discovered empirically in PR #70):
- Codex (`chatgpt-codex-connector[bot]`): markdown image badges
  `![P0 Badge](...badge/P0-red...)` and `![P1 Badge](...badge/P1-...)`.

Gating model (hybrid; see PR #71 discussion):

  A finding gates the PR if BOTH:
    1. Its review thread is unresolved (no "Resolve conversation" click), AND
    2. The bot has not effectively superseded it. A bot supersedes its own
       earlier findings when it issues a fresh review pinned to the current
       head SHA — at that point only comments from that fresh review count;
       older comments re-anchored by GitHub to the new head no longer gate.

  Either signal alone clears the finding. This matches how human code review
  actually works: either the bot reconsiders, or you say "I've handled it."

Engagement (polling): after the repository owner requests Codex review and
names the exact head SHA, the script waits up to POLL_BUDGET_SECONDS for Codex
to engage via review, clean comment, or reaction. Contributor activity and
automatic review alone cannot satisfy the gate.

Run locally for debugging:
    GH_TOKEN=$(gh auth token) \\
    GITHUB_REPOSITORY=owner/repository \\
    PR_NUMBER=71 \\
    python .github/scripts/review_gate.py

Environment:
- `GH_TOKEN` or `GITHUB_TOKEN`: token with `pull-requests: read`, `issues: read`,
  `checks: read`, `contents: read`.
- `GITHUB_REPOSITORY`: `owner/repo`.
- `PR_NUMBER`: integer PR number.
- `PR_HEAD_SHA` (optional): the head SHA the workflow run was dispatched for.
  If set and the PR head has already moved past it, the run exits 0 as
  superseded instead of duplicating the new head's run.
- `GITHUB_STEP_SUMMARY` (optional): if set, writes a markdown summary table.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Final

CODEX_LOGIN: Final = "chatgpt-codex-connector"

# GraphQL `author.login` returns the bot login *without* the trailing `[bot]`
# suffix that the REST API includes — both forms are normalized below.
BOT_LABELS: Final[dict[str, str]] = {
    CODEX_LOGIN: "Codex",
}

CODEX_BLOCKING = re.compile(r"!\[P[01]\s*Badge\]|badge/P[01]-", re.IGNORECASE)
CODEX_CLEAN_COMMENT = re.compile(
    r"Codex Review:\s*Did(?:n't| not) find any major issues", re.IGNORECASE
)
CODEX_SETUP_REQUIRED = re.compile(r"To use Codex here,", re.IGNORECASE)
CODEX_REVIEWED_COMMIT = re.compile(
    r"(?:\*\*)?Reviewed commit:(?:\*\*)?\s*`?([0-9a-f]{7,40})`?", re.IGNORECASE
)
CODEX_FULL_REVIEW = re.compile(r"###\s+(?:💡\s+)?Codex Review\b", re.IGNORECASE)

POLL_INTERVAL_SECONDS: Final = 15
# Codex's re-review latency is not stable. It has been ~8.5 min in PR #71
# and >15 min during SPA-271 after Codex started review with an eyes reaction
# but had not submitted the completed review yet. The gate still fails
# closed, but the budget needs to reflect real service latency so valid PRs
# do not get stranded by a too-short wait.
POLL_BUDGET_SECONDS: Final = 1800

GRAPHQL_QUERY: Final = """
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      headRefOid
      commits(last: 1) {
        nodes {
          commit { oid pushedDate committedDate }
        }
      }
      reviews(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          databaseId
          submittedAt
          body
          author { login }
          commit { oid }
        }
      }
      reviewThreads(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          isResolved
          comments(first: 100) {
            pageInfo { hasNextPage }
            nodes {
              databaseId
              author { login }
              body
              path
              line
              originalLine
              pullRequestReview { databaseId }
            }
          }
        }
      }
      comments(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          body
          createdAt
          updatedAt
          author { login }
        }
      }
      reactions(first: 100, content: THUMBS_UP) {
        pageInfo { hasNextPage }
        nodes {
          createdAt
          user { login }
        }
      }
    }
  }
}
""".strip()


@dataclass(frozen=True)
class Finding:
    bot: str
    path: str
    line: int
    severity: str
    title: str


# GitHub's API intermittently fails during incidents (2026-06-10: transient
# GraphQL 401s crashed three gate runs on PR #83). A required check must not
# go red on a single blip, so gh_api retries with backoff before raising.
GH_API_RETRY_DELAYS: Final = (10, 30, 60)


def gh_api(args: list[str]) -> object:
    """Run `gh api <args>` and return parsed JSON, retrying transient
    failures with backoff. Surfaces stderr on every failure so permission/
    auth errors are visible in the CI log."""
    cmd = ["gh", "api", *args]
    label = " ".join(args)[:120]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    for delay in GH_API_RETRY_DELAYS:
        if result.returncode == 0:
            break
        print(
            f"gh api {label} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}; retrying in {delay}s",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(delay)
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(
            f"gh api {label} failed (exit {result.returncode}) after "
            f"{len(GH_API_RETRY_DELAYS) + 1} attempts: {result.stderr.strip()}",
            file=sys.stderr,
            flush=True,
        )
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return json.loads(result.stdout) if result.stdout.strip() else None


def fetch_pr_state(repo: str, pr: int) -> dict[str, object]:
    """One GraphQL round-trip pulls reviews, threads, comments, reactions, and
    the head SHA. Replaces the prior N REST calls. The query is intentionally
    capped for runtime, but fails closed if GitHub reports more pages, since a
    required review gate must not silently ignore bot findings."""
    owner, name = repo.split("/", 1)
    raw = gh_api(
        [
            "graphql",
            "-f",
            f"query={GRAPHQL_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={name}",
            "-F",
            f"pr={pr}",
        ]
    )
    if not isinstance(raw, dict):
        raise RuntimeError("GraphQL returned non-dict")
    data = raw.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"GraphQL response had no data: {raw}")
    repo_data = data.get("repository")
    if not isinstance(repo_data, dict):
        raise RuntimeError(f"GraphQL response had no repository: {raw}")
    pr_data = repo_data.get("pullRequest")
    if not isinstance(pr_data, dict):
        raise RuntimeError(f"PR #{pr} not found")
    pagination_overflows = _pagination_overflows(pr_data)
    if pagination_overflows:
        joined = ", ".join(pagination_overflows)
        raise RuntimeError(f"GraphQL pagination overflow in review gate: {joined}")
    return pr_data


def _has_next_page(connection: object) -> bool:
    if not isinstance(connection, dict):
        return False
    page_info = connection.get("pageInfo")
    return isinstance(page_info, dict) and page_info.get("hasNextPage") is True


def _pagination_overflows(state: dict[str, object]) -> list[str]:
    overflows: list[str] = []
    for key in ("reviews", "reviewThreads", "comments", "reactions"):
        if _has_next_page(state.get(key)):
            overflows.append(key)

    threads_obj = state.get("reviewThreads")
    if not isinstance(threads_obj, dict):
        return overflows
    for index, thread in enumerate(threads_obj.get("nodes") or []):
        if not isinstance(thread, dict):
            continue
        if _has_next_page(thread.get("comments")):
            overflows.append(f"reviewThreads[{index}].comments")
    return overflows


def _author_login(node: dict[str, object]) -> str:
    """Extract author login from a GraphQL node, stripping a `[bot]` suffix
    so we can compare against `CODEX_LOGIN` uniformly."""
    author = node.get("author")
    if not isinstance(author, dict):
        return ""
    login = author.get("login")
    if not isinstance(login, str):
        return ""
    return login.removesuffix("[bot]")


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _comment_request_time(comment: dict[str, object]) -> datetime | None:
    """Return when the comment most recently authorized its current body."""
    updated = comment.get("updatedAt")
    if isinstance(updated, str):
        parsed = _parse_iso(updated)
        if parsed is not None:
            return parsed
    created = comment.get("createdAt")
    return _parse_iso(created) if isinstance(created, str) else None


def _head_time(state: dict[str, object], head_sha: str) -> datetime | None:
    """Return the timestamp for when the head commit became reviewable.

    Only `pushedDate` is safe for reaction freshness. `committedDate` can be
    arbitrarily old after a force-push or reused commit, which would let a
    thumbs-up from a prior head satisfy the current head's engagement gate. If
    GitHub omits `pushedDate`, fall back to the latest `@codex review` request
    comment that names this exact head SHA.
    """
    commits_obj = state.get("commits")
    if not isinstance(commits_obj, dict):
        return None
    nodes = commits_obj.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return None
    last = nodes[-1]
    if not isinstance(last, dict):
        return None
    commit = last.get("commit")
    if not isinstance(commit, dict):
        return None
    pushed = commit.get("pushedDate")
    if isinstance(pushed, str):
        parsed = _parse_iso(pushed)
        if parsed is not None:
            return parsed
    return _latest_codex_request_time(state, head_sha)


def _latest_codex_request_time(state: dict[str, object], head_sha: str) -> datetime | None:
    comments_obj = state.get("comments")
    if not isinstance(comments_obj, dict):
        return None
    candidates: list[datetime] = []
    for c in comments_obj.get("nodes") or []:
        if not isinstance(c, dict):
            continue
        body = c.get("body")
        if not isinstance(body, str) or "@codex review" not in body.lower():
            continue
        if head_sha not in body:
            continue
        request_time = _comment_request_time(c)
        if request_time is not None:
            candidates.append(request_time)
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1]


def _owner_codex_request_time(
    state: dict[str, object], owner_login: str, head_sha: str
) -> datetime | None:
    comments_obj = state.get("comments")
    if not isinstance(comments_obj, dict):
        return None
    requests: list[datetime] = []
    for comment in comments_obj.get("nodes") or []:
        if not isinstance(comment, dict) or _author_login(comment) != owner_login:
            continue
        body = comment.get("body")
        if isinstance(body, str) and "@codex review" in body.lower() and head_sha in body:
            request_time = _comment_request_time(comment)
            if request_time is not None:
                requests.append(request_time)
    return max(requests) if requests else None


def _owner_review_cutoff(
    state: dict[str, object],
    owner_login: str,
    head_sha: str,
    head_time: datetime | None,
) -> datetime | None:
    request_time = _owner_codex_request_time(state, owner_login, head_sha)
    if request_time is None:
        return None
    return max(request_time, head_time) if head_time is not None else request_time


def _codex_setup_required(
    state: dict[str, object], head_time: datetime | None, head_sha: str | None = None
) -> bool:
    comments_obj = state.get("comments")
    if head_time is not None and isinstance(comments_obj, dict):
        for comment in comments_obj.get("nodes") or []:
            if not isinstance(comment, dict) or _author_login(comment) != CODEX_LOGIN:
                continue
            body = comment.get("body")
            created = _parse_iso(comment.get("createdAt") or "")
            if (
                isinstance(body, str)
                and CODEX_SETUP_REQUIRED.search(body)
                and created is not None
                and created >= head_time
            ):
                return True

    if head_sha is None or head_time is None:
        return False
    latest_review_id = _latest_review_id_on_head(
        state,
        CODEX_LOGIN,
        head_sha,
        not_before=head_time,
        require_full_review=False,
    )
    if latest_review_id is None:
        return False
    threads_obj = state.get("reviewThreads")
    if not isinstance(threads_obj, dict):
        return False
    for thread in threads_obj.get("nodes") or []:
        if not isinstance(thread, dict):
            continue
        thread_comments = thread.get("comments")
        if not isinstance(thread_comments, dict):
            continue
        for comment in thread_comments.get("nodes") or []:
            if not isinstance(comment, dict) or _author_login(comment) != CODEX_LOGIN:
                continue
            review = comment.get("pullRequestReview")
            review_id = review.get("databaseId") if isinstance(review, dict) else None
            body = comment.get("body")
            if (
                review_id == latest_review_id
                and isinstance(body, str)
                and CODEX_SETUP_REQUIRED.search(body)
            ):
                return True
    return False


def engaged_bots(
    state: dict[str, object],
    repo: str,
    head_sha: str,
    head_time: datetime | None,
    owner_login: str | None = None,
) -> set[str]:
    """Return bot logins that have engaged with the PR on the current head.

    - Codex: after an exact-head owner request, review with commit.oid ==
      head_sha, clean issue comment for the head SHA, OR thumbs-up reaction.
      Every signal must be at or after the latest exact-head owner request.
    """
    freshness_time = head_time
    if owner_login is not None:
        freshness_time = _owner_review_cutoff(state, owner_login, head_sha, head_time)
        if freshness_time is None:
            return set()

    engaged: set[str] = set()
    codex_blocking_review_ids = _blocking_review_ids(state, CODEX_LOGIN)

    reviews_obj = state.get("reviews")
    if isinstance(reviews_obj, dict):
        for r in reviews_obj.get("nodes") or []:
            if not isinstance(r, dict):
                continue
            login = _author_login(r)
            review_id = r.get("databaseId")
            codex_review_is_substantive = _is_full_codex_review(r, head_sha) or (
                isinstance(review_id, int) and review_id in codex_blocking_review_ids
            )
            commit = r.get("commit")
            commit_sha = commit.get("oid") if isinstance(commit, dict) else None
            submitted = _parse_iso(r.get("submittedAt") or "")
            if (
                login in BOT_LABELS
                and commit_sha == head_sha
                and (login != CODEX_LOGIN or codex_review_is_substantive)
                and submitted is not None
                and (freshness_time is None or submitted >= freshness_time)
            ):
                engaged.add(login)

    if CODEX_LOGIN not in engaged and _codex_thumbed_up_head(state, freshness_time):
        engaged.add(CODEX_LOGIN)

    if CODEX_LOGIN not in engaged and _codex_clean_comment_on_head(state, head_sha, freshness_time):
        engaged.add(CODEX_LOGIN)

    return engaged


def _latest_review_id_on_head(
    state: dict[str, object],
    bot_login: str,
    head_sha: str,
    not_before: datetime | None = None,
    require_full_review: bool = True,
) -> int | None:
    """Return the bot's latest substantive review ID on ``head_sha``.

    A Codex review is substantive when it is a stamped full review or carries
    a blocking inline finding. Empty task reviews do not supersede findings.
    """
    reviews_obj = state.get("reviews")
    if not isinstance(reviews_obj, dict):
        return None
    blocking_review_ids = _blocking_review_ids(state, bot_login)
    candidates: list[tuple[datetime, int]] = []
    for r in reviews_obj.get("nodes") or []:
        if not isinstance(r, dict):
            continue
        if _author_login(r) != bot_login:
            continue
        rid = r.get("databaseId")
        has_blocking_findings = isinstance(rid, int) and rid in blocking_review_ids
        if (
            require_full_review
            and bot_login == CODEX_LOGIN
            and not _is_full_codex_review(r, head_sha)
            and not has_blocking_findings
        ):
            continue
        commit = r.get("commit")
        commit_sha = commit.get("oid") if isinstance(commit, dict) else None
        if commit_sha != head_sha:
            continue
        when = _parse_iso(r.get("submittedAt") or "")
        if when is not None and isinstance(rid, int) and (not_before is None or when >= not_before):
            candidates.append((when, rid))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def _is_full_codex_review(review: dict[str, object], head_sha: str) -> bool:
    body = review.get("body")
    if not isinstance(body, str) or not CODEX_FULL_REVIEW.search(body):
        return False
    match = CODEX_REVIEWED_COMMIT.search(body)
    return match is not None and head_sha.lower().startswith(match.group(1).lower())


def _blocking_review_ids(state: dict[str, object], bot_login: str) -> set[int]:
    """Return review IDs containing blocking inline findings from this bot."""
    review_ids: set[int] = set()
    threads_obj = state.get("reviewThreads")
    if not isinstance(threads_obj, dict):
        return review_ids
    for thread in threads_obj.get("nodes") or []:
        if not isinstance(thread, dict):
            continue
        comments_obj = thread.get("comments")
        if not isinstance(comments_obj, dict):
            continue
        for comment in comments_obj.get("nodes") or []:
            if not isinstance(comment, dict) or _author_login(comment) != bot_login:
                continue
            body = comment.get("body")
            review = comment.get("pullRequestReview")
            review_id = review.get("databaseId") if isinstance(review, dict) else None
            if isinstance(body, str) and CODEX_BLOCKING.search(body) and isinstance(review_id, int):
                review_ids.add(review_id)
    return review_ids


def _codex_thumbed_up_head(state: dict[str, object], head_time: datetime | None) -> bool:
    """True iff Codex added a 👍 reaction newer than the head commit's
    pushed timestamp — Codex's documented signal for "evaluated this
    commit, no concerns."

    The earlier implementation also accepted "reaction newer than Codex's
    most recent review on the PR" as a defensive secondary signal. Both
    Prior bot review flagged this as wrong: a reaction added in response to a
    *prior* commit's @codex-review request can be newer than the most recent
    review even when Codex hasn't evaluated the current head at all. Removing
    the secondary check; rely solely on head_time. If head_time is null, we
    conservatively don't infer cleanliness.
    """
    return _codex_thumbed_up_time(state, head_time) is not None


def _codex_thumbed_up_time(state: dict[str, object], head_time: datetime | None) -> datetime | None:
    if head_time is None:
        return None
    candidates: list[datetime] = []
    reactions_obj = state.get("reactions")
    if not isinstance(reactions_obj, dict):
        return None
    for reaction in reactions_obj.get("nodes") or []:
        if not isinstance(reaction, dict):
            continue
        user = reaction.get("user")
        if not isinstance(user, dict):
            continue
        login = user.get("login")
        if not isinstance(login, str) or login.removesuffix("[bot]") != CODEX_LOGIN:
            continue
        created = _parse_iso(reaction.get("createdAt") or "")
        if created is not None and created >= head_time:
            candidates.append(created)
    if not candidates:
        return None
    return max(candidates)


def _codex_clean_comment_time_on_head(
    state: dict[str, object], head_sha: str, head_time: datetime | None
) -> datetime | None:
    """Return the newest Codex clean issue-comment time for this head."""
    if head_time is None:
        return None
    comments_obj = state.get("comments")
    if not isinstance(comments_obj, dict):
        return None
    candidates: list[datetime] = []
    for c in comments_obj.get("nodes") or []:
        if not isinstance(c, dict):
            continue
        if _author_login(c) != CODEX_LOGIN:
            continue
        body = c.get("body")
        if not isinstance(body, str) or not CODEX_CLEAN_COMMENT.search(body):
            continue
        created = _parse_iso(c.get("createdAt") or "")
        if created is None or created < head_time:
            continue
        match = CODEX_REVIEWED_COMMIT.search(body)
        if match is None:
            continue
        reviewed = match.group(1).lower()
        if head_sha.lower().startswith(reviewed):
            candidates.append(created)
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1]


def _codex_clean_comment_on_head(
    state: dict[str, object], head_sha: str, head_time: datetime | None
) -> bool:
    """True iff Codex posted its clean issue-comment format for this head."""
    return _codex_clean_comment_time_on_head(state, head_sha, head_time) is not None


def _latest_review_time_on_head(
    state: dict[str, object], bot_login: str, head_sha: str
) -> datetime | None:
    reviews_obj = state.get("reviews")
    if not isinstance(reviews_obj, dict):
        return None
    blocking_review_ids = _blocking_review_ids(state, bot_login)
    candidates: list[datetime] = []
    for r in reviews_obj.get("nodes") or []:
        if not isinstance(r, dict):
            continue
        if _author_login(r) != bot_login:
            continue
        review_id = r.get("databaseId")
        has_blocking_findings = isinstance(review_id, int) and review_id in blocking_review_ids
        if (
            bot_login == CODEX_LOGIN
            and not _is_full_codex_review(r, head_sha)
            and not has_blocking_findings
        ):
            continue
        commit = r.get("commit")
        commit_sha = commit.get("oid") if isinstance(commit, dict) else None
        if commit_sha != head_sha:
            continue
        when = _parse_iso(r.get("submittedAt") or "")
        if when is not None:
            candidates.append(when)
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1]


def collect_findings(
    state: dict[str, object], head_sha: str, head_time: datetime | None
) -> list[Finding]:
    """Apply the hybrid filter.

    For each bot, choose one of three modes based on the bot's signal on
    the current head:

    1. **Clean** — bot has acknowledged this commit with no findings (Codex
       👍 reaction on the PR newer than the head commit). Skip ALL of that
       bot's comments; they're superseded by the clean signal.
    2. **Fresh review** — bot has posted a review pinned to `head_sha`.
       Scope to comments from THAT review only; older re-anchored comments
       don't count.
    3. **Not evaluated** — bot hasn't engaged with this commit yet. Fall
       back to counting all of the bot's comments (conservative — better
       to gate than miss a real concern).

    Resolved threads (`isResolved == true`) clear the finding under any mode.
    """
    codex_clean_comment_time = _codex_clean_comment_time_on_head(state, head_sha, head_time)
    latest_codex_review_time = _latest_review_time_on_head(state, CODEX_LOGIN, head_sha)
    codex_clean_comment_latest = codex_clean_comment_time is not None and (
        latest_codex_review_time is None or codex_clean_comment_time >= latest_codex_review_time
    )
    codex_reaction_time = _codex_thumbed_up_time(state, head_time)
    codex_reaction_latest = codex_reaction_time is not None and (
        latest_codex_review_time is None or codex_reaction_time >= latest_codex_review_time
    )
    codex_clean = codex_reaction_latest or codex_clean_comment_latest
    latest_per_bot: dict[str, int | None] = {
        login: _latest_review_id_on_head(state, login, head_sha) for login in BOT_LABELS
    }

    findings: list[Finding] = []
    threads_obj = state.get("reviewThreads")
    if not isinstance(threads_obj, dict):
        return findings
    for thread in threads_obj.get("nodes") or []:
        if not isinstance(thread, dict):
            continue
        if thread.get("isResolved") is True:
            continue
        comments_obj = thread.get("comments")
        if not isinstance(comments_obj, dict):
            continue
        for c in comments_obj.get("nodes") or []:
            if not isinstance(c, dict):
                continue
            login = _author_login(c)
            if login not in BOT_LABELS:
                continue
            # Mode 1: Codex declared head clean via 👍 reaction.
            if login == CODEX_LOGIN and codex_clean:
                continue
            # Mode 2: bot has a fresh review on head — scope to its comments.
            latest_rid = latest_per_bot.get(login)
            if latest_rid is not None:
                review_ref = c.get("pullRequestReview")
                comment_rid = review_ref.get("databaseId") if isinstance(review_ref, dict) else None
                if comment_rid != latest_rid:
                    continue
            # Mode 3: no fresh signal — fall through, count all of bot's comments.

            body = c.get("body")
            if not isinstance(body, str):
                continue
            if not CODEX_BLOCKING.search(body):
                continue

            path = c.get("path")
            line = c.get("line") or c.get("originalLine") or 0
            findings.append(
                Finding(
                    bot=BOT_LABELS[login],
                    path=path if isinstance(path, str) else "",
                    line=line if isinstance(line, int) else 0,
                    severity="P0/P1",
                    title=_first_line(body),
                )
            )
    return findings


def _first_line(body: str) -> str:
    for raw in body.splitlines():
        stripped = raw.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return "(no title)"


def write_summary(findings: list[Finding], engaged: set[str], timed_out: bool) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines: list[str] = ["## review-gate", ""]
    labels = sorted(BOT_LABELS[b] for b in engaged)
    lines.append(f"**Bots engaged:** {', '.join(labels) if labels else '(none)'}")
    if timed_out:
        missing = sorted(BOT_LABELS[b] for b in (set(BOT_LABELS) - engaged))
        lines.append(f"⚠️ Timed out waiting for: {', '.join(missing)}")
    lines.append("")
    if findings:
        lines.append(f"❌ **{len(findings)} blocking finding(s):**")
        lines.append("")
        lines.append("| Bot | File:Line | Severity | Summary |")
        lines.append("|---|---|---|---|")
        for f in findings:
            esc = f.title.replace("|", "\\|")
            lines.append(f"| {f.bot} | `{f.path}:{f.line}` | {f.severity} | {esc} |")
        lines.append("")
        lines.append(
            "_Findings clear when the bot freshly re-reviews without re-flagging, "
            "or when you click **Resolve conversation** on the thread._"
        )
    else:
        lines.append("✅ No unresolved blocking findings.")
    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> int:
    try:
        repo = os.environ["GITHUB_REPOSITORY"]
        pr = int(os.environ["PR_NUMBER"])
    except (KeyError, ValueError) as exc:
        print(f"missing/invalid env: {exc}", file=sys.stderr)
        return 2

    state = fetch_pr_state(repo, pr)
    owner_login = repo.split("/", 1)[0]
    head_sha = state.get("headRefOid")
    if not isinstance(head_sha, str):
        print("PR state missing headRefOid", file=sys.stderr)
        return 2
    # A run can START already superseded: with cancel-in-progress off, a run
    # dispatched for an older commit may begin after the PR head has moved.
    # Without this check it would pin the live head and duplicate the new
    # head's run in full. PR_HEAD_SHA is the SHA the workflow was dispatched
    # for; empty when running locally.
    dispatched_sha = os.environ.get("PR_HEAD_SHA", "")
    if dispatched_sha and dispatched_sha != head_sha:
        print(
            f"Dispatched for {dispatched_sha[:10]} but head is {head_sha[:10]}; "
            "this run is superseded by the new head's run. Exiting without gating.",
            flush=True,
        )
        return 0
    head_time = _head_time(state, head_sha)
    print(f"PR #{pr} head: {head_sha[:10]} @ {head_time}", flush=True)

    deadline = time.monotonic() + POLL_BUDGET_SECONDS
    engaged: set[str] = set()
    while True:
        review_cutoff = _owner_review_cutoff(state, owner_login, head_sha, head_time)
        if _codex_setup_required(state, review_cutoff, head_sha):
            print(
                "Codex review is unavailable because this repository is not connected. ",
                "Connect it in Codex settings, then push a new head to rerun the gate.",
                file=sys.stderr,
            )
            return 1
        engaged = engaged_bots(state, repo, head_sha, head_time, owner_login)
        missing = set(BOT_LABELS) - engaged
        if not missing:
            print(f"All bots engaged on {head_sha[:10]}: {sorted(engaged)}", flush=True)
            break
        if time.monotonic() >= deadline:
            print(
                f"Timed out after {POLL_BUDGET_SECONDS}s. "
                f"Engaged: {sorted(engaged) or 'none'}. Missing: {sorted(missing)}",
                flush=True,
            )
            break
        print(
            f"Engaged: {sorted(engaged) or 'none'}. "
            f"Waiting for: {sorted(missing)} (sleeping {POLL_INTERVAL_SECONDS}s)",
            flush=True,
        )
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            state = fetch_pr_state(repo, pr)
        except subprocess.CalledProcessError:
            # Outage outlasting gh_api's retries: keep the previous state and
            # let the deadline bound the loop — if the API never recovers and
            # engagement can't be confirmed, the timeout fails closed.
            print("PR state fetch failed; retrying next interval.", flush=True)
            continue
        # A new push supersedes this run: the workflow fires a fresh run for
        # the new head (concurrency no longer cancels, so this early exit is
        # what bounds CI minutes). Exiting 0 is safe — required checks are
        # evaluated on the current head SHA, which gets its own full run.
        live_head = state.get("headRefOid")
        if isinstance(live_head, str) and live_head != head_sha:
            print(
                f"Head moved {head_sha[:10]} -> {live_head[:10]}; this run is "
                "superseded by the new head's run. Exiting without gating.",
                flush=True,
            )
            return 0
        head_time = _head_time(state, head_sha)

    review_cutoff = _owner_review_cutoff(state, owner_login, head_sha, head_time)
    findings = collect_findings(state, head_sha, review_cutoff)
    timed_out = bool(set(BOT_LABELS) - engaged)
    write_summary(findings, engaged, timed_out)

    if findings:
        print(f"❌ {len(findings)} unresolved blocking finding(s):", flush=True)
        for f in findings:
            print(f"  [{f.bot} {f.severity}] {f.path}:{f.line} — {f.title}", flush=True)
        print(
            "\nClear by either: bot re-reviews without re-flagging "
            "(push fixes and wait for a fresh review), or click 'Resolve conversation' "
            "on the thread.",
            flush=True,
        )
        return 1

    if timed_out:
        # Fail closed (Codex P1 finding): if any bot never engaged within
        # the poll budget, refuse to certify the PR even though we found
        # no findings — bot infrastructure issues shouldn't silently pass.
        # Manual override via admin bypass on the ruleset.
        missing_labels = sorted(BOT_LABELS[b] for b in (set(BOT_LABELS) - engaged))
        print(
            f"❌ Failing closed: timed out waiting for {', '.join(missing_labels)} "
            f"after {POLL_BUDGET_SECONDS}s.",
            flush=True,
        )
        return 1

    print("✅ No unresolved blocking findings.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
