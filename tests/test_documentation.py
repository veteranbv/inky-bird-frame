from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
FENCED_BLOCK = re.compile(r"^\s*```.*?^\s*```\s*$", re.MULTILINE | re.DOTALL)
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(?P<target><[^>]+>|[^\s)]+)(?:\s+[^)]*)?\s*\)")
HTML_LINK = re.compile(r"\b(?:href|src|srcset)\s*=\s*[\"'](?P<target>[^\"']+)[\"']")
REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(?P<target>\S+)", re.MULTILINE)
HEADING = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*#*\s*$", re.MULTILINE)


def _markdown_files() -> list[Path]:
    root_files = sorted(ROOT.glob("*.md"))
    return root_files + sorted((ROOT / "docs").rglob("*.md"))


def _without_fenced_code(text: str) -> str:
    return FENCED_BLOCK.sub("", text)


def _targets(text: str) -> list[str]:
    content = _without_fenced_code(text)
    targets = [match.group("target") for match in MARKDOWN_LINK.finditer(content)]
    targets.extend(match.group("target") for match in HTML_LINK.finditer(content))
    targets.extend(match.group("target") for match in REFERENCE_LINK.finditer(content))
    return targets


def _slug(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("`", "").strip().lower()
    value = re.sub(r"[^\w\- ]", "", value)
    return value.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    content = _without_fenced_code(path.read_text(encoding="utf-8"))
    occurrences: defaultdict[str, int] = defaultdict(int)
    anchors: set[str] = set()
    for match in HEADING.finditer(content):
        base = _slug(match.group("title"))
        index = occurrences[base]
        occurrences[base] += 1
        anchors.add(base if index == 0 else f"{base}-{index}")
    return anchors


def test_internal_documentation_links_and_anchors_resolve() -> None:
    problems: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}

    for source in _markdown_files():
        for raw_target in _targets(source.read_text(encoding="utf-8")):
            target = raw_target.removeprefix("<").removesuffix(">")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                continue

            target_path = source if not parsed.path else source.parent / unquote(parsed.path)
            target_path = target_path.resolve()
            try:
                target_path.relative_to(ROOT)
            except ValueError:
                problems.append(f"{source.relative_to(ROOT)}: link escapes repository: {target}")
                continue

            if not target_path.exists():
                problems.append(f"{source.relative_to(ROOT)}: missing target: {target}")
                continue

            if parsed.fragment and target_path.suffix.lower() == ".md":
                anchors = anchor_cache.setdefault(target_path, _anchors(target_path))
                fragment = unquote(parsed.fragment).lower()
                if fragment not in anchors:
                    problems.append(
                        f"{source.relative_to(ROOT)}: missing anchor #{fragment} in "
                        f"{target_path.relative_to(ROOT)}"
                    )

    assert not problems, "\n" + "\n".join(problems)
