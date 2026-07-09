"""Regression tests for GitHub Actions workflow pinning enforcement."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast


class WorkflowPinningModule(Protocol):
    class Violation(Protocol):
        path: Path
        line: int
        value: str
        reason: str

    def main(self, argv: Sequence[str] | None = None) -> int: ...

    def scan_workflows(self, workflows_dir: Path) -> list[Violation]: ...


def _load_workflow_pinning() -> WorkflowPinningModule:
    script = (
        Path(__file__).resolve().parents[1] / ".github" / "scripts" / "check_workflow_pinning.py"
    )
    spec = importlib.util.spec_from_file_location("workflow_pinning_under_test", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load check_workflow_pinning.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast(WorkflowPinningModule, module)


def _write_workflow(workflows_dir: Path, name: str, body: str) -> None:
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / name).write_text(body, encoding="utf-8")


def test_scan_workflows_accepts_pinned_and_local_actions(tmp_path: Path) -> None:
    workflow_pinning = _load_workflow_pinning()
    workflows_dir = tmp_path / "workflows"
    _write_workflow(
        workflows_dir,
        "ci.yml",
        """
name: ci
jobs:
  test:
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4
      - uses: ./.github/actions/local-action
""",
    )

    assert workflow_pinning.scan_workflows(workflows_dir) == []


def test_scan_workflows_rejects_tags_missing_refs_and_short_shas(tmp_path: Path) -> None:
    workflow_pinning = _load_workflow_pinning()
    workflows_dir = tmp_path / "workflows"
    _write_workflow(
        workflows_dir,
        "ci.yml",
        """
name: ci
jobs:
  test:
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv
      - uses: owner/repo@abc123
""",
    )

    violations = workflow_pinning.scan_workflows(workflows_dir)

    assert [(v.line, v.value, v.reason) for v in violations] == [
        (6, "actions/checkout@v4", "ref is not a 40-character commit SHA"),
        (7, "astral-sh/setup-uv", "missing @<commit-sha> ref"),
        (8, "owner/repo@abc123", "ref is not a 40-character commit SHA"),
    ]


def test_scan_workflows_rejects_quoted_keys_and_flow_mappings(tmp_path: Path) -> None:
    workflow_pinning = _load_workflow_pinning()
    workflows_dir = tmp_path / "workflows"
    _write_workflow(
        workflows_dir,
        "ci.yml",
        """
name: ci
jobs:
  test:
    steps:
      - "uses": actions/checkout@v4
      - { uses: astral-sh/setup-uv }
""",
    )

    violations = workflow_pinning.scan_workflows(workflows_dir)

    assert [(v.line, v.value, v.reason) for v in violations] == [
        (6, "actions/checkout@v4", "ref is not a 40-character commit SHA"),
        (7, "astral-sh/setup-uv", "missing @<commit-sha> ref"),
    ]


def test_current_workflows_are_sha_pinned() -> None:
    workflow_pinning = _load_workflow_pinning()
    workflows_dir = Path(__file__).resolve().parents[1] / ".github" / "workflows"

    assert workflow_pinning.scan_workflows(workflows_dir) == []


def test_review_gate_retrigger_accepts_only_exact_head_owner_requests() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/review-gate-retrigger.yml"
    ).read_text(encoding="utf-8")

    assert "github.event.comment.user.login == github.repository_owner" in workflow
    assert '[[ "${COMMENT_BODY}" != *"${HEAD_SHA}"* ]]' in workflow
    assert "Skipping fork PR" not in workflow
