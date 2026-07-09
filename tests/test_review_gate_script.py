"""Regression tests for the GitHub review-gate bridge script."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

import pytest


class ReviewGateModule(Protocol):
    CODEX_LOGIN: str

    def main(self) -> int: ...

    def _head_time(self, state: dict[str, object], head_sha: str) -> datetime | None: ...

    def _owner_codex_request_time(
        self, state: dict[str, object], owner_login: str, head_sha: str
    ) -> datetime | None: ...

    def _codex_thumbed_up_head(
        self, state: dict[str, object], head_time: datetime | None
    ) -> bool: ...

    def _codex_clean_comment_on_head(
        self, state: dict[str, object], head_sha: str, head_time: datetime | None
    ) -> bool: ...

    def _codex_clean_comment_time_on_head(
        self, state: dict[str, object], head_sha: str, head_time: datetime | None
    ) -> datetime | None: ...

    def _codex_setup_required(
        self,
        state: dict[str, object],
        head_time: datetime | None,
        head_sha: str | None = None,
    ) -> bool: ...

    def engaged_bots(
        self,
        state: dict[str, object],
        repo: str,
        head_sha: str,
        head_time: datetime | None,
        owner_login: str | None = None,
    ) -> set[str]: ...

    def collect_findings(
        self, state: dict[str, object], head_sha: str, head_time: datetime | None
    ) -> list[object]: ...

    def _pagination_overflows(self, state: dict[str, object]) -> list[str]: ...


def _load_review_gate() -> ReviewGateModule:
    script = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "review_gate.py"
    spec = importlib.util.spec_from_file_location("review_gate_under_test", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load review_gate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(ReviewGateModule, module)


def _state_with_commit(*, pushed: str | None, committed: str) -> dict[str, object]:
    return {
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": "abc123",
                        "pushedDate": pushed,
                        "committedDate": committed,
                    }
                }
            ]
        },
        "headRefOid": "abc123",
        "reviews": {"nodes": []},
        "reviewThreads": {"nodes": []},
        "comments": {"nodes": []},
        "reactions": {
            "nodes": [
                {
                    "createdAt": "2026-06-15T12:00:00Z",
                    "user": {"login": "chatgpt-codex-connector"},
                }
            ]
        },
    }


def _full_codex_review_body(head_sha: str) -> str:
    return (
        "\n### 💡 Codex Review\n\n"
        "Here are some automated review suggestions.\n\n"
        f"**Reviewed commit:** `{head_sha[:10]}`"
    )


def _plain_codex_review_body(head_sha: str) -> str:
    return f"### Codex Review\n\n**Reviewed commit:** `{head_sha[:10]}`"


def test_codex_reaction_does_not_engage_when_pushed_date_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed=None, committed="2026-06-01T12:00:00Z")

    head_time = review_gate._head_time(state, "abc123")

    assert head_time is None
    assert not review_gate._codex_thumbed_up_head(state, head_time)
    assert review_gate.engaged_bots(state, "owner/repo", "abc123", head_time) == set()


def test_codex_reaction_can_engage_when_newer_than_pushed_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T11:59:00Z", committed="2026-06-01T12:00:00Z")

    head_time = review_gate._head_time(state, "abc123")

    assert head_time is not None
    assert review_gate._codex_thumbed_up_head(state, head_time)
    assert review_gate.engaged_bots(state, "owner/repo", "abc123", head_time) == {
        review_gate.CODEX_LOGIN
    }


def test_codex_engagement_requires_owner_request_for_exact_head() -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T11:59:00Z", committed="2026-06-01T12:00:00Z")
    head_time = review_gate._head_time(state, "abc123")

    assert (
        review_gate.engaged_bots(
            state,
            "owner/repo",
            "abc123",
            head_time,
            owner_login="owner",
        )
        == set()
    )

    state["comments"] = {
        "nodes": [
            {
                "body": "@codex review\n\nhead: abc123",
                "createdAt": "2026-06-15T11:59:30Z",
                "author": {"login": "owner"},
            }
        ]
    }

    assert review_gate.engaged_bots(
        state,
        "owner/repo",
        "abc123",
        head_time,
        owner_login="owner",
    ) == {review_gate.CODEX_LOGIN}


def test_codex_engagement_must_follow_latest_owner_request() -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T11:59:00Z", committed="2026-06-01T12:00:00Z")
    state["reviews"] = {
        "nodes": [
            {
                "databaseId": 10,
                "submittedAt": "2026-06-15T12:01:00Z",
                "body": _full_codex_review_body("abc123"),
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": "abc123"},
            }
        ]
    }
    state["comments"] = {
        "nodes": [
            {
                "body": "Codex Review: Didn't find any major issues.\n\nReviewed commit: abc123",
                "createdAt": "2026-06-15T12:02:00Z",
                "author": {"login": "chatgpt-codex-connector"},
            },
            {
                "body": "@codex review\n\nhead: abc123",
                "createdAt": "2026-06-15T12:03:00Z",
                "author": {"login": "owner"},
            },
        ]
    }
    head_time = review_gate._head_time(state, "abc123")

    assert (
        review_gate.engaged_bots(
            state,
            "owner/repo",
            "abc123",
            head_time,
            owner_login="owner",
        )
        == set()
    )

    state["reactions"] = {
        "nodes": [
            {
                "createdAt": "2026-06-15T12:04:00Z",
                "user": {"login": "chatgpt-codex-connector"},
            }
        ]
    }
    assert review_gate.engaged_bots(
        state,
        "owner/repo",
        "abc123",
        head_time,
        owner_login="owner",
    ) == {review_gate.CODEX_LOGIN}


def test_empty_codex_task_review_does_not_satisfy_full_review_request() -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T11:59:00Z", committed="2026-06-01T12:00:00Z")
    state["comments"] = {
        "nodes": [
            {
                "body": "@codex review\n\nhead: abc123",
                "createdAt": "2026-06-15T12:00:00Z",
                "author": {"login": "owner"},
            }
        ]
    }
    state["reactions"] = {"nodes": []}
    state["reviews"] = {
        "nodes": [
            {
                "databaseId": 10,
                "submittedAt": "2026-06-15T12:01:00Z",
                "body": "",
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": "abc123"},
            }
        ]
    }
    head_time = review_gate._head_time(state, "abc123")

    assert (
        review_gate.engaged_bots(
            state,
            "owner/repo",
            "abc123",
            head_time,
            owner_login="owner",
        )
        == set()
    )


def test_plain_codex_review_header_satisfies_full_review_request() -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T11:59:00Z", committed="2026-06-01T12:00:00Z")
    state["headRefOid"] = "abc1234"
    state["commits"] = {
        "nodes": [
            {
                "commit": {
                    "oid": "abc1234",
                    "pushedDate": "2026-06-15T11:59:00Z",
                    "committedDate": "2026-06-01T12:00:00Z",
                }
            }
        ]
    }
    state["comments"] = {
        "nodes": [
            {
                "body": "@codex review\n\nhead: abc1234",
                "createdAt": "2026-06-15T12:00:00Z",
                "author": {"login": "owner"},
            }
        ]
    }
    state["reactions"] = {"nodes": []}
    state["reviews"] = {
        "nodes": [
            {
                "databaseId": 10,
                "submittedAt": "2026-06-15T12:01:00Z",
                "body": _plain_codex_review_body("abc1234"),
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": "abc1234"},
            }
        ]
    }
    head_time = review_gate._head_time(state, "abc1234")

    assert review_gate.engaged_bots(
        state,
        "owner/repo",
        "abc1234",
        head_time,
        owner_login="owner",
    ) == {review_gate.CODEX_LOGIN}


def test_codex_setup_comment_blocks_reaction_engagement() -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T11:59:00Z", committed="2026-06-01T12:00:00Z")
    state["comments"] = {
        "nodes": [
            {
                "body": "To use Codex here, create a Codex account and connect to github.",
                "createdAt": "2026-06-15T12:00:01Z",
                "author": {"login": "chatgpt-codex-connector"},
            }
        ]
    }

    head_time = review_gate._head_time(state, "abc123")

    assert review_gate._codex_setup_required(state, head_time)


def test_codex_setup_inline_comment_blocks_latest_head_review() -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T11:59:00Z", committed="2026-06-01T12:00:00Z")
    state["reviews"] = {
        "nodes": [
            {
                "databaseId": 10,
                "submittedAt": "2026-06-15T12:00:00Z",
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": "abc123"},
            }
        ]
    }
    state["reviewThreads"] = {
        "nodes": [
            {
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": "To use Codex here, create an environment for this repo.",
                            "pullRequestReview": {"databaseId": 10},
                        }
                    ]
                }
            }
        ]
    }
    head_time = review_gate._head_time(state, "abc123")

    assert review_gate._codex_setup_required(state, head_time, "abc123")


def test_codex_reaction_can_engage_when_newer_than_codex_request_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed=None, committed="2026-06-01T12:00:00Z")
    state["comments"] = {
        "nodes": [
            {
                "body": "@codex review\n\nhead: abc123",
                "createdAt": "2026-06-15T11:59:00Z",
                "author": {"login": "owner"},
            }
        ]
    }

    head_time = review_gate._head_time(state, "abc123")

    assert head_time is not None
    assert review_gate._codex_thumbed_up_head(state, head_time)
    assert review_gate.engaged_bots(state, "owner/repo", "abc123", head_time) == {
        review_gate.CODEX_LOGIN
    }


def test_codex_clean_comment_can_engage_when_reviewed_commit_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T12:01:00Z", committed="2026-06-01T12:00:00Z")
    state["headRefOid"] = "abc123def456"
    state["commits"] = {
        "nodes": [
            {
                "commit": {
                    "oid": "abc123def456",
                    "pushedDate": "2026-06-15T12:01:00Z",
                    "committedDate": "2026-06-01T12:00:00Z",
                }
            }
        ]
    }
    state["comments"] = {
        "nodes": [
            {
                "body": "Codex Review: Didn't find any major issues.\n\n"
                "**Reviewed commit:** `abc123d`",
                "createdAt": "2026-06-15T12:02:00Z",
                "author": {"login": "chatgpt-codex-connector"},
            }
        ]
    }

    head_time = review_gate._head_time(state, "abc123def456")

    assert not review_gate._codex_thumbed_up_head(state, head_time)
    assert review_gate._codex_clean_comment_on_head(state, "abc123def456", head_time)
    assert review_gate.engaged_bots(state, "owner/repo", "abc123def456", head_time) == {
        review_gate.CODEX_LOGIN
    }


def test_codex_clean_comment_ignores_other_reviewed_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T12:01:00Z", committed="2026-06-01T12:00:00Z")
    state["comments"] = {
        "nodes": [
            {
                "body": "Codex Review: Didn't find any major issues.\n\n"
                "**Reviewed commit:** `old4567`",
                "createdAt": "2026-06-15T12:02:00Z",
                "author": {"login": "chatgpt-codex-connector"},
            }
        ]
    }

    head_time = review_gate._head_time(state, "abc123")

    assert not review_gate._codex_clean_comment_on_head(state, "abc123", head_time)
    assert review_gate.engaged_bots(state, "owner/repo", "abc123", head_time) == set()


def test_codex_clean_comment_ignores_stale_comment_for_same_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T12:01:00Z", committed="2026-06-01T12:00:00Z")
    state["headRefOid"] = "abc123def456"
    state["commits"] = {
        "nodes": [
            {
                "commit": {
                    "oid": "abc123def456",
                    "pushedDate": "2026-06-15T12:01:00Z",
                    "committedDate": "2026-06-01T12:00:00Z",
                }
            }
        ]
    }
    state["comments"] = {
        "nodes": [
            {
                "body": "Codex Review: Didn't find any major issues.\n\n"
                "**Reviewed commit:** `abc123d`",
                "createdAt": "2026-06-15T12:00:00Z",
                "author": {"login": "chatgpt-codex-connector"},
            }
        ]
    }

    head_time = review_gate._head_time(state, "abc123def456")

    assert not review_gate._codex_clean_comment_on_head(state, "abc123def456", head_time)
    assert review_gate.engaged_bots(state, "owner/repo", "abc123def456", head_time) == set()


def test_codex_clean_comment_accepts_unbolded_reviewed_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T12:01:00Z", committed="2026-06-01T12:00:00Z")
    state["headRefOid"] = "abc123def456"
    state["commits"] = {
        "nodes": [
            {
                "commit": {
                    "oid": "abc123def456",
                    "pushedDate": "2026-06-15T12:01:00Z",
                    "committedDate": "2026-06-01T12:00:00Z",
                }
            }
        ]
    }
    state["comments"] = {
        "nodes": [
            {
                "body": "Codex Review: Didn't find any major issues.\n\nReviewed commit: abc123d",
                "createdAt": "2026-06-15T12:02:00Z",
                "author": {"login": "chatgpt-codex-connector"},
            }
        ]
    }

    head_time = review_gate._head_time(state, "abc123def456")

    assert review_gate._codex_clean_comment_on_head(state, "abc123def456", head_time)
    assert review_gate.engaged_bots(state, "owner/repo", "abc123def456", head_time) == {
        review_gate.CODEX_LOGIN
    }


def test_codex_clean_comment_supersedes_old_blocking_threads() -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T12:01:00Z", committed="2026-06-01T12:00:00Z")
    state["headRefOid"] = "abc1234"
    state["commits"] = {
        "nodes": [
            {
                "commit": {
                    "oid": "abc1234",
                    "pushedDate": "2026-06-15T12:01:00Z",
                    "committedDate": "2026-06-01T12:00:00Z",
                }
            }
        ]
    }
    state["comments"] = {
        "nodes": [
            {
                "body": "Codex Review: Didn't find any major issues.\n\n"
                "**Reviewed commit:** `abc1234`",
                "createdAt": "2026-06-15T12:02:00Z",
                "author": {"login": "chatgpt-codex-connector"},
            }
        ]
    }
    state["reviewThreads"] = {
        "nodes": [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1,
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": "![P1 Badge](https://example.invalid/badge/P1-red.svg)\nold",
                            "path": "example.py",
                            "line": 1,
                            "pullRequestReview": {"databaseId": 10},
                        }
                    ]
                },
            }
        ]
    }
    head_time = review_gate._head_time(state, "abc1234")

    assert review_gate.collect_findings(state, "abc1234", head_time) == []


def test_newer_codex_review_overrides_older_clean_comment() -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T12:01:00Z", committed="2026-06-01T12:00:00Z")
    state["headRefOid"] = "abc1234"
    state["commits"] = {
        "nodes": [
            {
                "commit": {
                    "oid": "abc1234",
                    "pushedDate": "2026-06-15T12:01:00Z",
                    "committedDate": "2026-06-01T12:00:00Z",
                }
            }
        ]
    }
    state["comments"] = {
        "nodes": [
            {
                "body": "Codex Review: Didn't find any major issues.\n\n"
                "**Reviewed commit:** `abc1234`",
                "createdAt": "2026-06-15T12:02:00Z",
                "author": {"login": "chatgpt-codex-connector"},
            }
        ]
    }
    state["reviews"] = {
        "nodes": [
            {
                "databaseId": 10,
                "submittedAt": "2026-06-15T12:03:00Z",
                "body": "",
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": "abc1234"},
            }
        ]
    }
    state["reviewThreads"] = {
        "nodes": [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1,
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": "![P1 Badge](https://example.invalid/badge/P1-red.svg)\nnew",
                            "path": "example.py",
                            "line": 1,
                            "pullRequestReview": {"databaseId": 10},
                        }
                    ]
                },
            }
        ]
    }
    head_time = review_gate._head_time(state, "abc1234")

    assert review_gate._codex_clean_comment_time_on_head(state, "abc1234", head_time) is not None
    assert len(review_gate.collect_findings(state, "abc1234", head_time)) == 1


def test_newer_blocking_review_overrides_older_clean_reaction() -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T11:59:00Z", committed="2026-06-01T12:00:00Z")
    state["headRefOid"] = "abc1234"
    state["reviews"] = {
        "nodes": [
            {
                "databaseId": 10,
                "submittedAt": "2026-06-15T12:03:00Z",
                "body": "",
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": "abc1234"},
            }
        ]
    }
    state["reviewThreads"] = {
        "nodes": [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1,
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": "![P1 Badge](https://example.invalid/badge/P1-red.svg)\nnew",
                            "path": "new.py",
                            "line": 1,
                            "pullRequestReview": {"databaseId": 10},
                        }
                    ]
                },
            }
        ]
    }
    head_time = review_gate._head_time(state, "abc1234")

    assert len(review_gate.collect_findings(state, "abc1234", head_time)) == 1


def test_blocking_unrecognized_review_supersedes_older_full_review() -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T12:01:00Z", committed="2026-06-01T12:00:00Z")
    state["headRefOid"] = "abc1234"
    state["reviews"] = {
        "nodes": [
            {
                "databaseId": 10,
                "submittedAt": "2026-06-15T12:02:00Z",
                "body": _full_codex_review_body("abc1234"),
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": "abc1234"},
            },
            {
                "databaseId": 11,
                "submittedAt": "2026-06-15T12:03:00Z",
                "body": "",
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": "abc1234"},
            },
        ]
    }
    state["reviewThreads"] = {
        "nodes": [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1,
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": "![P1 Badge](https://example.invalid/badge/P1-red.svg)\nnew",
                            "path": "new.py",
                            "line": 1,
                            "pullRequestReview": {"databaseId": 11},
                        }
                    ]
                },
            }
        ]
    }
    head_time = review_gate._head_time(state, "abc1234")

    assert len(review_gate.collect_findings(state, "abc1234", head_time)) == 1


def test_empty_codex_task_review_does_not_supersede_full_review_findings() -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="2026-06-15T12:01:00Z", committed="2026-06-01T12:00:00Z")
    state["headRefOid"] = "abc1234"
    state["commits"] = {
        "nodes": [
            {
                "commit": {
                    "oid": "abc1234",
                    "pushedDate": "2026-06-15T12:01:00Z",
                    "committedDate": "2026-06-01T12:00:00Z",
                }
            }
        ]
    }
    state["reactions"] = {"nodes": []}
    state["reviews"] = {
        "nodes": [
            {
                "databaseId": 10,
                "submittedAt": "2026-06-15T12:02:00Z",
                "body": _full_codex_review_body("abc1234"),
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": "abc1234"},
            },
            {
                "databaseId": 11,
                "submittedAt": "2026-06-15T12:03:00Z",
                "body": "",
                "author": {"login": "chatgpt-codex-connector"},
                "commit": {"oid": "abc1234"},
            },
        ]
    }
    state["reviewThreads"] = {
        "nodes": [
            {
                "isResolved": False,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1,
                            "author": {"login": "chatgpt-codex-connector"},
                            "body": "![P1 Badge](https://example.invalid/badge/P1-red.svg)\nissue",
                            "path": "example.py",
                            "line": 1,
                            "pullRequestReview": {"databaseId": 10},
                        }
                    ]
                },
            }
        ]
    }
    head_time = review_gate._head_time(state, "abc1234")

    assert len(review_gate.collect_findings(state, "abc1234", head_time)) == 1


def test_codex_request_fallback_handles_invalid_pushed_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed="", committed="2026-06-01T12:00:00Z")
    state["comments"] = {
        "nodes": [
            {
                "body": "@codex review\n\nhead: abc123",
                "createdAt": "2026-06-15T11:59:00Z",
                "author": {"login": "github-actions"},
            }
        ]
    }

    head_time = review_gate._head_time(state, "abc123")

    assert head_time is not None
    assert review_gate._codex_thumbed_up_head(state, head_time)
    assert review_gate.engaged_bots(state, "owner/repo", "abc123", head_time) == {
        review_gate.CODEX_LOGIN
    }


def test_codex_request_fallback_ignores_other_head_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed=None, committed="2026-06-01T12:00:00Z")
    state["comments"] = {
        "nodes": [
            {
                "body": "@codex review\n\nhead: old456",
                "createdAt": "2026-06-15T11:59:00Z",
                "author": {"login": "github-actions"},
            }
        ]
    }

    head_time = review_gate._head_time(state, "abc123")

    assert head_time is None
    assert not review_gate._codex_thumbed_up_head(state, head_time)
    assert review_gate.engaged_bots(state, "owner/repo", "abc123", head_time) == set()


def test_owner_request_uses_edit_time_for_current_body() -> None:
    review_gate = _load_review_gate()
    state = _state_with_commit(pushed=None, committed="2026-06-01T12:00:00Z")
    state["comments"] = {
        "nodes": [
            {
                "body": "@codex review\n\nhead: abc1234",
                "createdAt": "2026-06-15T11:00:00Z",
                "updatedAt": "2026-06-15T12:04:00Z",
                "author": {"login": "owner"},
            }
        ]
    }

    cutoff = review_gate._owner_codex_request_time(state, "owner", "abc1234")

    assert cutoff is not None
    assert cutoff.isoformat() == "2026-06-15T12:04:00+00:00"


def test_engagement_poll_refreshes_head_time_from_updated_pr_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_gate = _load_review_gate()
    initial = _state_with_commit(pushed=None, committed="2026-06-01T12:00:00Z")
    refreshed = _state_with_commit(pushed=None, committed="2026-06-01T12:00:00Z")
    refreshed["comments"] = {
        "nodes": [
            {
                "body": "@codex review\n\nhead: abc123",
                "createdAt": "2026-06-15T11:59:00Z",
                "author": {"login": "owner"},
            }
        ]
    }
    states = iter([initial, refreshed])

    def fetch_pr_state(_repo: str, _pr: int) -> dict[str, object]:
        return next(states)

    monkeypatch.setattr(review_gate, "fetch_pr_state", fetch_pr_state)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("PR_NUMBER", "123")
    monkeypatch.setattr(review_gate, "POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(review_gate, "POLL_BUDGET_SECONDS", 1)
    times = iter([0.0, 0.0])
    monkeypatch.setattr("review_gate_under_test.time.monotonic", lambda: next(times))
    monkeypatch.setattr("review_gate_under_test.time.sleep", lambda _seconds: None)

    assert review_gate.main() == 0


def test_pagination_overflow_includes_nested_thread_comments() -> None:
    review_gate = _load_review_gate()
    state = {
        "reviews": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        "comments": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        "reviewThreads": {
            "pageInfo": {"hasNextPage": True},
            "nodes": [
                {
                    "comments": {
                        "pageInfo": {"hasNextPage": True},
                        "nodes": [],
                    }
                }
            ],
        },
        "reactions": {"pageInfo": {"hasNextPage": False}, "nodes": []},
    }

    assert review_gate._pagination_overflows(state) == [
        "reviewThreads",
        "reviewThreads[0].comments",
    ]
