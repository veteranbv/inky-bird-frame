"""Conformance matrix for exact-version Codex review qualification.

The positive fixture is real PR547 evidence (task links redacted); every
counterexample mutates it, so each test isolates one failure mode against
actual receipt formats rather than hand-written approximations.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

_CONTRACT = Path(__file__).resolve().parents[1] / ".github/scripts/review_contract.py"
if TYPE_CHECKING:
    import review_contract as rc  # resolved through mypy_path = .github/scripts
else:
    # The CI job loads the contract by path, uninstalled; test it the same way.
    _spec = importlib.util.spec_from_file_location("review_contract", _CONTRACT)
    assert _spec is not None and _spec.loader is not None
    rc = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = rc  # dataclasses resolve annotations via sys.modules
    _spec.loader.exec_module(rc)

FIXTURES = Path(__file__).parent / "fixtures" / "codex_review"
HEAD = "5c2cd1412a42a909a1d4e00746cc72559d31e8f9"
OTHER = "0" * 40


def _base() -> rc.Snapshot:
    return cast(rc.Snapshot, json.loads((FIXTURES / "qualified_homelab_547.json").read_text()))


def _shapes() -> dict[str, str]:
    return cast(dict[str, str], json.loads((FIXTURES / "receipt_shapes.json").read_text()))


def _comment(snapshot: rc.Snapshot, prefix: str) -> rc.Comment:
    return next(c for c in snapshot["comments"] if c["body"].startswith(prefix))


def _summary(snapshot: rc.Snapshot) -> rc.Comment:
    return _comment(snapshot, "<!-- codex-pull-request-review-summary")


def _request(snapshot: rc.Snapshot) -> rc.Comment:
    return _comment(snapshot, "@codex")


def _receipts(snapshot: rc.Snapshot) -> list[rc.Comment]:
    return [c for c in snapshot["comments"] if c["body"].startswith("<!-- codex-review-receipt")]


def _set_state(snapshot: rc.Snapshot, kind: str, **values: object) -> None:
    summary = _summary(snapshot)
    prefix, _, rest = summary["body"].partition("<!-- codex-review-state:v1 ")
    payload, _, tail = rest.partition(" -->")
    state = json.loads(payload)
    state[kind].update(values)
    summary["body"] = f"{prefix}<!-- codex-review-state:v1 {json.dumps(state)} -->{tail}"


def _verdict(snapshot: rc.Snapshot) -> rc.Verdict:
    return rc.evaluate(snapshot)


def _blocked(snapshot: rc.Snapshot, reason: str) -> None:
    verdict = _verdict(snapshot)
    assert not verdict.qualified
    assert reason in verdict.reasons, verdict.reasons


def test_real_exact_owner_request_with_both_receipts_qualifies() -> None:
    verdict = _verdict(_base())
    assert verdict.qualified, verdict.reasons
    assert {k: r.state for k, r in verdict.kinds.items()} == {
        "code_review": "completed",
        "security_review": "completed",
    }
    assert verdict.evidence_time == "2026-10-27T05:13:33.587046Z"


def test_contract_module_is_standard_library_only() -> None:
    """The CI job loads this file by path from trusted base code, uninstalled."""
    tree = ast.parse(_CONTRACT.read_text())
    modules = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    }
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}


# --- Authorization --------------------------------------------------------


def test_no_request_blocks() -> None:
    snapshot = _base()
    snapshot["comments"].remove(_request(snapshot))  # also covers a deleted request
    _blocked(snapshot, "code_review:request_missing")


@pytest.mark.parametrize(
    ("author", "author_type"),
    [("github-actions[bot]", "Bot"), ("someone-else", "User"), ("veteranbv", "Bot")],
)
def test_request_from_anyone_but_exact_owner_user_blocks(author: str, author_type: str) -> None:
    snapshot = _base()
    request = _request(snapshot)
    request["author"], request["author_type"] = author, author_type
    _blocked(snapshot, "code_review:request_missing")


def test_edited_request_blocks_even_with_receipts() -> None:
    snapshot = _base()
    _request(snapshot)["edited"] = True
    _blocked(snapshot, "request_edited")


def test_edited_request_for_an_older_head_does_not_block() -> None:
    snapshot = _base()
    old = copy.deepcopy(_request(snapshot))
    old["id"], old["edited"], old["body"] = 4, True, f"@codex review {OTHER}"
    snapshot["comments"].append(old)
    assert _verdict(snapshot).qualified


def test_edited_request_before_lifecycle_reset_does_not_block() -> None:
    snapshot = _base()
    old = copy.deepcopy(_request(snapshot))
    old.update({"id": 4, "edited": True, "created_at": "2026-10-27T04:00:00Z"})
    snapshot["comments"].append(old)
    snapshot["lifecycle_events"] = [{"type": "ReopenedEvent", "created_at": "2026-10-27T05:00:00Z"}]
    assert _verdict(snapshot).qualified


def test_request_must_name_full_current_head() -> None:
    snapshot = _base()
    _request(snapshot)["body"] = f"@codex review {HEAD[:12]}"
    _blocked(snapshot, "code_review:request_missing")
    _request(snapshot)["body"] = f"@codex review {OTHER}"
    _blocked(snapshot, "code_review:request_missing")


@pytest.mark.parametrize(
    "body", [f"note: @codex review {HEAD} later", f"@codex review-please {HEAD}"]
)
def test_non_command_text_is_not_a_request(body: str) -> None:
    snapshot = _base()
    _request(snapshot)["body"] = body
    _blocked(snapshot, "code_review:request_missing")


def test_either_command_authorizes_both_kinds() -> None:
    """Observed: `@codex security review` starts a code review on this account;
    security runs through the owner's setting. Either request opens the
    generation; each kind still needs its own completion and receipt."""
    snapshot = _base()
    _request(snapshot)["body"] = f"@codex security review {HEAD}"
    assert _verdict(snapshot).qualified


def test_generated_request_body_is_a_valid_request() -> None:
    snapshot = _base()
    _request(snapshot)["body"] = rc.request_body(HEAD, "main", "d" * 40)
    verdict = _verdict(snapshot)
    assert verdict.qualified, verdict.reasons
    for kind in rc.KINDS:
        request = verdict.requests[kind]
        assert request is not None and request["base_sha"] == "d" * 40


# --- Lifecycle and generation freshness ---------------------------------


@pytest.mark.parametrize(
    "event",
    [
        "ReadyForReviewEvent",
        "ReopenedEvent",
        "HeadRefForcePushedEvent",  # includes force-push back to a reviewed SHA
        "BaseRefChangedEvent",
        "BaseRefForcePushedEvent",
        "ConvertToDraftEvent",
        "AutomaticBaseChangeSucceededEvent",
    ],
)
def test_lifecycle_reset_after_request_invalidates_checkpoint(event: str) -> None:
    snapshot = _base()
    snapshot["lifecycle_events"] = [{"type": event, "created_at": "2026-10-27T05:20:00Z"}]
    _blocked(snapshot, "code_review:request_missing")


def test_lifecycle_event_before_request_does_not_invalidate() -> None:
    snapshot = _base()
    snapshot["lifecycle_events"] = [
        {"type": "ReadyForReviewEvent", "created_at": "2026-10-27T05:00:00Z"}
    ]
    assert _verdict(snapshot).qualified


def test_same_head_retry_requires_new_completion() -> None:
    """A newer request supersedes completions that predate it."""
    snapshot = _base()
    retry = copy.deepcopy(_request(snapshot))
    retry["id"], retry["created_at"] = 1, "2026-10-27T06:00:00Z"
    snapshot["comments"].append(retry)
    _blocked(snapshot, "code_review:completed_before_request")
    assert _verdict(snapshot).kinds["security_review"].state == "stale"  # advisory


def test_mutable_summary_edit_does_not_create_freshness() -> None:
    """Touching the summary (lastEditedAt) never moves per-kind completion."""
    snapshot = _base()
    _summary(snapshot)["created_at"] = "2026-10-27T09:00:00Z"
    retry = copy.deepcopy(_request(snapshot))
    retry["id"], retry["created_at"] = 2, "2026-10-27T08:00:00Z"
    snapshot["comments"].append(retry)
    _blocked(snapshot, "code_review:completed_before_request")


def test_new_head_invalidates_prior_review() -> None:
    snapshot = _base()
    snapshot["head_sha"] = OTHER
    _request(snapshot)["body"] = f"@codex review {OTHER}"
    _blocked(snapshot, "code_review:summary_other_commit")


def test_security_lagging_on_older_commit_is_advisory() -> None:
    snapshot = _base()
    _set_state(snapshot, "security_review", commit_sha=OTHER)
    verdict = _verdict(snapshot)
    assert verdict.qualified, verdict.reasons
    assert verdict.kinds["security_review"].state == "stale"


# --- Receipt provenance and kind ----------------------------------------


def test_running_code_review_blocks() -> None:
    snapshot = _base()
    _set_state(snapshot, "code_review", status="running", has_unresolved_findings=None)
    verdict = _verdict(snapshot)
    assert not verdict.qualified
    assert verdict.kinds["code_review"].state == "running"


def test_running_security_review_is_not_awaited() -> None:
    snapshot = _base()
    _set_state(snapshot, "security_review", status="running", has_unresolved_findings=None)
    verdict = _verdict(snapshot)
    assert verdict.qualified, verdict.reasons
    assert verdict.kinds["security_review"].state == "running"


def test_real_running_summary_shape_blocks() -> None:
    snapshot = _base()
    _summary(snapshot)["body"] = _shapes()["summary_running"]
    verdict = _verdict(snapshot)
    assert not verdict.qualified  # other commit, and security still running


@pytest.mark.parametrize("status", ["failed", "cancelled", "dismissed", "", None])
def test_unknown_status_blocks(status: object) -> None:
    snapshot = _base()
    _set_state(snapshot, "code_review", status=status)
    _blocked(snapshot, "code_review:summary_kind_unknown")


@pytest.mark.parametrize("value", [True, None, "false"])
def test_unresolved_or_unknown_findings_block(value: object) -> None:
    snapshot = _base()
    _set_state(snapshot, "security_review", has_unresolved_findings=value)
    _blocked(snapshot, "security_review:unresolved_findings")


def test_unknown_state_version_blocks() -> None:
    snapshot = _base()
    summary = _summary(snapshot)
    summary["body"] = summary["body"].replace("codex-review-state:v1", "codex-review-state:v2")
    _blocked(snapshot, "summary_state_unknown_version")


def test_malformed_state_blocks() -> None:
    snapshot = _base()
    summary = _summary(snapshot)
    summary["body"] = summary["body"].replace('{"code_review"', '{"code_review"::', 1)
    _blocked(snapshot, "summary_state_malformed")


def test_missing_kind_in_state_blocks() -> None:
    snapshot = _base()
    summary = _summary(snapshot)
    start = summary["body"].index("<!-- codex-review-state:v1 ") + len(
        "<!-- codex-review-state:v1 "
    )
    end = summary["body"].index(" -->", start)
    state = json.loads(summary["body"][start:end])
    del state["code_review"]
    summary["body"] = summary["body"][:start] + json.dumps(state) + summary["body"][end:]
    _blocked(snapshot, "code_review:summary_kind_missing")


def test_row_contradicting_typed_state_blocks() -> None:
    snapshot = _base()
    summary = _summary(snapshot)
    summary["body"] = summary["body"].replace("**Completed**", "**Running**", 1)
    _blocked(snapshot, "code_review:summary_row_contradicts_state")


def test_duplicate_summaries_are_ambiguous() -> None:
    snapshot = _base()
    extra = copy.deepcopy(_summary(snapshot))
    extra["id"] = 3
    snapshot["comments"].append(extra)
    _blocked(snapshot, "summary_ambiguous")


def test_summary_from_impostor_user_is_ignored() -> None:
    snapshot = _base()
    _summary(snapshot)["author_type"] = "User"
    _blocked(snapshot, "code_review:summary_missing")


def test_missing_receipt_blocks() -> None:
    """Typed state alone, without an immutable receipt, does not qualify."""
    snapshot = _base()
    for receipt in _receipts(snapshot):
        snapshot["comments"].remove(receipt)
    _blocked(snapshot, "code_review:receipt_missing")
    assert _verdict(snapshot).kinds["security_review"].state == "blocked"


def test_code_twice_is_not_security_review() -> None:
    snapshot = _base()
    code, security = _receipts(snapshot)
    security["body"] = code["body"]
    verdict = _verdict(snapshot)
    assert verdict.kinds["security_review"].state == "blocked"  # no security receipt
    assert verdict.kinds["code_review"].state == "completed"


def test_receipt_claiming_both_kinds_counts_as_neither() -> None:
    snapshot = _base()
    code, security = _receipts(snapshot)
    code["body"] += "\n### 🛡️ Codex Security Review"
    _blocked(snapshot, "code_review:receipt_missing")


@pytest.mark.parametrize(
    "mutation", ["edited", "early", "other_commit", "short_prefix", "impostor"]
)
def test_untrustworthy_receipt_blocks(mutation: str) -> None:
    snapshot = _base()
    receipt = _receipts(snapshot)[0]
    if mutation == "edited":
        receipt["edited"] = True
    elif mutation == "early":
        receipt["created_at"] = "2026-10-27T05:10:00Z"
    elif mutation == "other_commit":
        receipt["body"] = receipt["body"].replace("5c2cd1412a", "0000000000")
    elif mutation == "short_prefix":
        receipt["body"] = receipt["body"].replace("5c2cd1412a", "5c2cd14")
    else:
        receipt["author_type"] = "User"
    _blocked(snapshot, "code_review:receipt_missing")


def test_findings_review_receipt_on_exact_head_corroborates() -> None:
    """Real findings shape: a PR review whose immutable commit is the head."""
    snapshot = _base()
    code = _receipts(snapshot)[0]
    snapshot["comments"].remove(code)
    review = rc.Review(
        id=code["id"],
        author=code["author"],
        author_type=code["author_type"],
        created_at=code["created_at"],
        edited=False,
        body=_shapes()["findings_code_review"],
        state="COMMENTED",
        commit_sha=HEAD,
    )
    snapshot["reviews"].append(review)
    assert _verdict(snapshot).kinds["code_review"].state == "completed"
    review["state"] = "DISMISSED"
    _blocked(snapshot, "code_review:receipt_missing")


def test_unknown_receipt_version_blocks() -> None:
    snapshot = _base()
    _receipts(snapshot)[0]["body"] = _receipts(snapshot)[0]["body"].replace(
        "codex-review-receipt:v1", "codex-review-receipt:v9"
    )
    _blocked(snapshot, "receipt_unknown_version")


@pytest.mark.parametrize("shape", ["something_went_wrong", "usage_limit"])
def test_failure_reply_is_never_a_receipt(shape: str) -> None:
    snapshot = _base()
    for receipt in _receipts(snapshot):
        receipt["body"] = _receipt_marked(_shapes()[shape])
    _blocked(snapshot, "code_review:receipt_missing")
    assert _verdict(snapshot).kinds["security_review"].state != "completed"


def _receipt_marked(body: str) -> str:
    """Worst case: a failure reply that also carries receipt markers."""
    return f"<!-- codex-review-receipt:v1 -->\n{body}\n**Reviewed commit:** `{HEAD[:10]}`"


def _failure(snapshot: rc.Snapshot, created_at: str, shape: str = "usage_limit") -> None:
    failure = copy.deepcopy(_receipts(snapshot)[0])
    failure["id"], failure["created_at"], failure["body"] = 7, created_at, _shapes()[shape]
    snapshot["comments"].append(failure)


def test_failure_after_request_blocks_until_a_later_receipt() -> None:
    snapshot = _base()
    _failure(snapshot, "2026-10-27T05:11:00Z")  # before both receipts: superseded
    assert _verdict(snapshot).qualified
    _failure(snapshot, "2026-10-27T05:13:00Z")  # after code receipt, before security
    _blocked(snapshot, "code_review:review_error_reported")
    assert _verdict(snapshot).kinds["security_review"].state == "completed"


@pytest.mark.parametrize(
    ("author", "author_type"), [("github-actions[bot]", "Bot"), ("someone-else", "User")]
)
def test_requests_not_in_the_owners_name_do_not_authorize(author: str, author_type: str) -> None:
    """Only owner requests authorize; others (Codex ignored bot requests) add nothing."""
    snapshot = _base()
    trigger = copy.deepcopy(_request(snapshot))
    trigger.update({"id": 6, "author": author, "author_type": author_type})
    trigger.update({"body": f"@codex review\n\nhead: {HEAD}", "created_at": "2026-10-27T05:10:00Z"})
    snapshot["comments"].append(trigger)
    assert _verdict(snapshot).qualified


def test_owner_request_naming_head_by_commit_url_counts() -> None:
    """Real hand-typed shape: `head: https://github.com/<repo>/commit/<sha>`."""
    snapshot = _base()
    body = f"@codex review\r\n\r\nhead: https://github.com/veteranbv/homelab/commit/{HEAD}"
    _request(snapshot)["body"] = body
    assert _verdict(snapshot).qualified


def test_security_not_started_is_advisory() -> None:
    """Real PR548 shape: code completed while security state was all null."""
    snapshot = _base()
    _set_state(
        snapshot,
        "security_review",
        commit_sha=None,
        has_any_findings=None,
        has_unresolved_findings=None,
        status=None,
    )
    verdict = _verdict(snapshot)
    assert verdict.qualified, verdict.reasons
    assert verdict.kinds["security_review"].state == "blocked"


def test_reaction_only_engagement_cannot_qualify() -> None:
    """Snapshots carry no reactions at all: a thumbs-up cannot be evidence."""
    assert "reactions" not in rc.Snapshot.__annotations__


# --- Lifecycle state and completeness -----------------------------------


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("state", "CLOSED", "pull_request_not_open"),
        ("state", "MERGED", "pull_request_not_open"),
        ("draft", True, "pull_request_draft"),
        ("unresolved_threads", 1, "unresolved_threads"),
        ("complete", False, "evidence_incomplete"),
        ("owner_type", "Organization", "owner_not_user"),
    ],
)
def test_lifecycle_and_completeness_gates(field: str, value: object, reason: str) -> None:
    snapshot = _base()
    snapshot[field] = value  # type: ignore[literal-required]  # parametrized TypedDict key
    _blocked(snapshot, reason)


# --- Live collection ----------------------------------------------------


def _page(head: str, *, comments: list[dict[str, object]], next_page: bool) -> dict[str, object]:
    connection = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}
    pull = {
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": head,
        "baseRefName": "main",
        "baseRefOid": "b" * 40,
        "comments": {
            "pageInfo": {"hasNextPage": next_page, "endCursor": "c1" if next_page else None},
            "nodes": comments,
        },
        "reviews": connection,
        "reviewThreads": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"isResolved": True}, {"isResolved": False}],
        },
        "commits": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    "commit": {
                        "oid": head,
                        "message": "Change",
                        "author": {"email": "1+veteranbv@users.noreply.github.com"},
                        "committer": {"email": "1+veteranbv@users.noreply.github.com"},
                    }
                }
            ],
        },
        "timelineItems": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"__typename": "ReopenedEvent", "createdAt": "2026-10-27T01:00:00Z"}],
        },
    }
    return {
        "data": {
            "repository": {
                "owner": {"__typename": "User", "login": "veteranbv"},
                "pullRequest": pull,
            }
        }
    }


def _node(identifier: int, login: str, typename: str) -> dict[str, object]:
    return {
        "databaseId": identifier,
        "createdAt": "2026-10-27T02:00:00Z",
        "lastEditedAt": None,
        "body": "x",
        "author": {"__typename": typename, "login": login},
    }


def test_collect_pages_and_normalizes_bot_identity() -> None:
    pages = [
        _page(HEAD, comments=[_node(1, "chatgpt-codex-connector", "Bot")], next_page=True),
        _page(HEAD, comments=[_node(2, "veteranbv", "User")], next_page=False),
    ]
    snapshot = rc.collect(lambda args: pages.pop(0), "veteranbv/homelab", 547)
    assert [c["author"] for c in snapshot["comments"]] == [rc.BOT_LOGIN, "veteranbv"]
    assert snapshot["unresolved_threads"] == 1
    assert snapshot["lifecycle_events"][0]["type"] == "ReopenedEvent"
    assert snapshot["complete"]


def test_collect_fails_closed_when_head_moves_between_pages() -> None:
    pages = [
        _page(HEAD, comments=[], next_page=True),
        _page(OTHER, comments=[], next_page=False),
    ]
    with pytest.raises(rc.EvidenceError, match="changed while reading"):
        rc.collect(lambda args: pages.pop(0), "veteranbv/homelab", 547)


def test_collect_marks_missing_cursor_incomplete() -> None:
    page = _page(HEAD, comments=[], next_page=True)
    comments = page["data"]["repository"]["pullRequest"]["comments"]  # type: ignore[index]
    comments["pageInfo"]["endCursor"] = None
    snapshot = rc.collect(lambda args: page, "veteranbv/homelab", 547)
    assert not snapshot["complete"]
    assert rc.evaluate(snapshot).reasons[0] == "evidence_incomplete"


# --- Trusted gate entrypoint ----------------------------------------------

GATE_SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/review_qualification.py"


def _gate_module() -> object:
    import importlib.util

    spec = importlib.util.spec_from_file_location("review_qualification_gate", GATE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("event_head", "mutate", "exit_code", "message"),
    [
        (HEAD, None, 0, '"qualified": true'),
        (OTHER, None, 1, "Superseded"),
        (HEAD, "unresolved_threads", 1, "Not qualified: unresolved_threads"),
    ],
)
def test_gate_entrypoint_answers_once_from_live_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    event_head: str,
    mutate: str | None,
    exit_code: int,
    message: str,
) -> None:
    gate = _gate_module()
    snapshot = _base()
    if mutate:
        snapshot["unresolved_threads"] = 1
    contract = gate._load_contract()  # type: ignore[attr-defined]  # module loaded by path
    monkeypatch.setattr(contract, "collect", lambda gh, repo, number: snapshot)
    monkeypatch.setattr(gate, "_load_contract", lambda: contract)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_REPOSITORY", "veteranbv/homelab")
    monkeypatch.setenv("PR_NUMBER", "547")
    monkeypatch.setenv("PR_HEAD_SHA", event_head)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert gate.main() == exit_code  # type: ignore[attr-defined]  # module loaded by path
    assert message in capsys.readouterr().out


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("timelineItems", None, "timelineItems_missing"),
        ("reviewThreads", {"pageInfo": {}, "nodes": [None]}, "reviewThreads_null_node"),
        ("reviews", {"nodes": []}, "reviews_malformed"),
        ("isDraft", None, "pull_request_state_malformed"),
    ],
)
def test_collect_treats_missing_or_null_data_as_incomplete(
    key: str, value: object, reason: str
) -> None:
    """Null connections must not read as 'no lifecycle resets' or 'no threads'."""
    page = _page(HEAD, comments=[], next_page=False)
    page["data"]["repository"]["pullRequest"][key] = value  # type: ignore[index]
    snapshot = rc.collect(lambda args: page, "veteranbv/homelab", 547)
    assert reason in snapshot["incomplete_reasons"]
    assert "evidence_incomplete" in rc.evaluate(snapshot).reasons


def test_later_receipt_recovers_from_an_intervening_failure() -> None:
    """receipt 05:12, failure 05:13, newer receipt and completion 05:14: recovered."""
    snapshot = _base()
    _failure(snapshot, "2026-10-27T05:12:50Z")
    later = copy.deepcopy(_receipts(snapshot)[0])
    later["id"], later["created_at"] = 8, "2026-10-27T05:14:00Z"
    snapshot["comments"].append(later)
    summary = _summary(snapshot)
    summary["body"] = summary["body"].replace(
        "2026-10-27T05:12:38.822039Z", "2026-10-27T05:14:01.000000Z"
    )
    verdict = _verdict(snapshot)
    assert verdict.kinds["code_review"].state == "completed", verdict.reasons
    assert verdict.kinds["code_review"].receipt_id == 8


def test_completion_without_commit_identity_blocks() -> None:
    """Real 2026-10-27 shape: a clean receipt with no `Reviewed commit` line and a
    summary reporting `completed` with `commit_sha: null`. Nothing binds it to the head."""
    snapshot = _base()
    _set_state(snapshot, "code_review", commit_sha=None)
    code = _receipts(snapshot)[0]
    code["body"] = code["body"].replace("**Reviewed commit:** `5c2cd1412a`", "")
    _blocked(snapshot, "code_review:summary_kind_unknown")


def test_same_head_retry_cannot_clear_reported_security_findings() -> None:
    """Security findings on the head stay blocking after a newer code-only pass."""
    snapshot = _base()
    _set_state(snapshot, "security_review", has_any_findings=True, has_unresolved_findings=True)
    retry = copy.deepcopy(_request(snapshot))
    retry["id"], retry["created_at"] = 1, "2026-10-27T06:00:00Z"
    snapshot["comments"].append(retry)
    newer = copy.deepcopy(_receipts(snapshot)[0])
    newer["id"], newer["created_at"] = 2, "2026-10-27T06:02:00Z"
    snapshot["comments"].append(newer)
    summary = _summary(snapshot)
    summary["body"] = summary["body"].replace(
        "2026-10-27T05:12:38.822039Z", "2026-10-27T06:02:01.000000Z"
    )
    verdict = _verdict(snapshot)
    assert verdict.kinds["security_review"].state == "stale"
    assert "security_review:unresolved_findings" in verdict.reasons
    assert not verdict.qualified


def test_evidence_time_covers_a_later_recovery_receipt() -> None:
    """Failure 05:12:50, recovery receipt 05:14:00 after the 05:12:38 summary row."""
    snapshot = _base()
    _failure(snapshot, "2026-10-27T05:12:50Z")
    later = copy.deepcopy(_receipts(snapshot)[0])
    later["id"], later["created_at"] = 8, "2026-10-27T05:14:00Z"
    snapshot["comments"].append(later)
    verdict = _verdict(snapshot)
    assert verdict.kinds["code_review"].receipt_id == 8
    assert verdict.evidence_time == "2026-10-27T05:14:00Z"


# --- Owner identity and no AI attribution --------------------------------

TRAILER = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


@pytest.mark.parametrize(
    "line",
    [
        TRAILER,
        "co-authored-by: Codex <codex@openai.com>",
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
        "Generated with Codex",
        "generated by ChatGPT",
        "Co-authored-by: Copilot <copilot@github.com>",
    ],
)
def test_ai_attribution_in_a_commit_blocks(line: str) -> None:
    snapshot = _base()
    snapshot["commits"][0]["message"] += f"\n\n{line}"
    _blocked(snapshot, "ai_attribution_in_commit:5c2cd14")


@pytest.mark.parametrize("field", ["title", "body"])
def test_ai_attribution_in_the_squash_description_blocks(field: str) -> None:
    snapshot = _base()
    snapshot[field] = f"Change\n{TRAILER}"  # type: ignore[literal-required]  # parametrized key
    _blocked(snapshot, "ai_attribution_in_description")


def test_human_co_author_is_allowed() -> None:
    snapshot = _base()
    snapshot["commits"][0]["message"] += "\n\nCo-authored-by: Charity <c@example.com>"
    assert _verdict(snapshot).qualified


@pytest.mark.parametrize(
    ("email", "blocked"),
    [
        ("209825114+claude[bot]@users.noreply.github.com", True),
        ("199175422+chatgpt-codex-connector[bot]@users.noreply.github.com", True),
        ("41898282+github-actions[bot]@users.noreply.github.com", True),
        ("49699333+dependabot[bot]@users.noreply.github.com", False),
        ("noreply@github.com", False),  # GitHub web-flow commits made by the owner
    ],
)
def test_only_the_owner_or_dependabot_may_author_commits(email: str, blocked: bool) -> None:
    snapshot = _base()
    snapshot["commits"][0]["author_email"] = email
    verdict = _verdict(snapshot)
    assert ("non_owner_commit:5c2cd14" in verdict.reasons) is blocked


def test_ordinary_generated_text_is_not_attribution() -> None:
    snapshot = _base()
    snapshot["commits"][0]["message"] += "\n\nGenerated with the existing pinning script."
    assert _verdict(snapshot).qualified
