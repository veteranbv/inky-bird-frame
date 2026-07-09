"""Fail when GitHub Actions workflow `uses:` references are not SHA pinned."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

FULL_SHA_REF: Final = re.compile(r"^[0-9a-fA-F]{40}$")
USES_KEY: Final = re.compile(r"(?:^|[-{,])\s*(?:uses|'uses'|\"uses\")\s*:\s*(.+)$")
LOCAL_USES_PREFIXES: Final = ("./", "../")
NON_GITHUB_ACTION_PREFIXES: Final = ("docker://",)


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    value: str
    reason: str


def workflow_files(workflows_dir: Path) -> list[Path]:
    return sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")])


def _uses_value(line: str) -> str | None:
    match = USES_KEY.search(line)
    if match is None:
        return None
    value = match.group(1).split("#", 1)[0].strip().rstrip(",}").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value


def scan_workflows(workflows_dir: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in workflow_files(workflows_dir):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            value = _uses_value(line)
            if value is None or not value:
                continue
            if value.startswith(LOCAL_USES_PREFIXES + NON_GITHUB_ACTION_PREFIXES):
                continue
            if "@" not in value:
                violations.append(Violation(path, line_number, value, "missing @<commit-sha> ref"))
                continue
            if not FULL_SHA_REF.fullmatch(value.rsplit("@", 1)[1]):
                violations.append(
                    Violation(path, line_number, value, "ref is not a 40-character commit SHA")
                )
    return violations


def format_violations(violations: Sequence[Violation]) -> str:
    lines = ["Unpinned GitHub Actions workflow references found:"]
    lines.extend(f"- {item.path}:{item.line}: {item.value} ({item.reason})" for item in violations)
    lines.extend(("", "Use a full 40-character commit SHA for external actions."))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflows_dir", nargs="?", default=".github/workflows", type=Path)
    args = parser.parse_args(argv)
    if not args.workflows_dir.is_dir() or not workflow_files(args.workflows_dir):
        print(f"workflow directory is missing or empty: {args.workflows_dir}", file=sys.stderr)
        return 2
    violations = scan_workflows(args.workflows_dir)
    if violations:
        print(format_violations(violations), file=sys.stderr)
        return 1
    print(f"workflow action pinning OK: {len(workflow_files(args.workflows_dir))} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
