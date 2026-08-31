from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PIL import Image

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


def test_readme_native_checks_use_the_managed_runtime() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Living with the frame", 1)[1].split("\n## ", 1)[0]

    assert 'IBF="$HOME/Services/inky-bird-frame/.venv/bin/inky-bird-frame"' in section
    assert re.search(r"^inky-bird-frame(?:\s|$)", section, re.MULTILINE) is None


def test_homepage_dashboard_examples_are_public_and_metadata_free() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Homepage dashboard", 1)[1].split("\n## ", 1)[0]

    assert "The screenshots use a deterministic v0.7.0 demo" in section
    assert "162 public plates published in that release and a fixed Eastern Bluebird" in section
    for name, expected_size in (
        ("homepage-dashboard-compact.png", (900, 300)),
        ("homepage-dashboard-featured-plate.png", (900, 920)),
    ):
        path = ROOT / "docs/images" / name
        assert name in section
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            assert image.size == expected_size
            assert image.mode == "RGB"
            assert image.info == {}
            assert not image.getexif()


def test_plate_problem_form_requires_supporting_sources() -> None:
    form = (ROOT / ".github/ISSUE_TEMPLATE/plate_problem.yml").read_text(encoding="utf-8")
    evidence = form.split("    id: evidence", 1)[1].split("\n  - type:", 1)[0]

    assert "    validations:\n      required: true" in evidence


def test_private_history_removal_documents_retained_state_and_refresh() -> None:
    guide = (ROOT / "docs/backup.md").read_text(encoding="utf-8")
    section = guide.split("## Private-data lifecycle and removal", 1)[1]

    for retained_path in (
        "generation-queue.json",
        "collection.json",
        "generation-retries.json",
        "runs/",
        "workspace_dir",
    ):
        assert retained_path in section
    assert '"$IBF" refresh --config /path/to/config.toml' in section
    assert (
        "docker compose run --rm --no-deps scheduler refresh --config /data/config.toml" in section
    )
    assert "clearing all controller observation and generation state" in section
    for retained_store in ("config.toml", "GitHub authentication", "service logs", "backups"):
        assert retained_store in section


def test_backup_restores_only_the_previously_active_native_services() -> None:
    guide = (ROOT / "docs/backup.md").read_text(encoding="utf-8")
    backup = guide.split("## Native controller backup", 1)[1].split(
        "\n## Native controller restore", 1
    )[0]

    assert 'SYSTEMD_STATE="$BACKUP/native-active-systemd-units.txt"' in backup
    assert 'LAUNCHD_STATE="$BACKUP/native-loaded-launchagents.txt"' in backup
    assert 'done < "$SYSTEMD_STATE"' in backup
    assert 'done < "$LAUNCHD_STATE"' in backup
    assert "Restore only the services recorded before the backup" in backup
    assert backup.index('done < "$SYSTEMD_STATE"') < backup.index('sudo systemctl start "$unit"')
    assert backup.index('done < "$LAUNCHD_STATE"') < backup.index("launchctl bootstrap")
    assert "Unexpected unit in $SYSTEMD_STATE" in backup
    assert "Unexpected label in $LAUNCHD_STATE" in backup
    assert "Recorded LaunchAgent is missing" in backup
    assert "One-shot service is not quiesced" in backup
    assert "Could not determine stable pre-backup state" in backup
    assert "Could not confirm recorded unit stopped" in backup
    quiesce = backup.split("Then quiesce exactly the recorded agents", 1)[1].split(
        "\n### 3. Copy", 1
    )[0]
    assert 'launchctl bootout "gui/$(id -u)/com.inky-bird-frame.$label" || exit 1' in quiesce
    assert 'done < "$LAUNCHD_STATE"' in quiesce
    assert "|| true" not in quiesce
    assert "Recorded LaunchAgent is still loaded after bootout" in quiesce


def test_docker_backup_restores_only_the_previously_running_services() -> None:
    guide = (ROOT / "docs/backup.md").read_text(encoding="utf-8")
    section = guide.split("## Docker backup", 1)[1].split("\n## Docker restore", 1)[0]

    capture = 'docker compose ps --status running --services > "$DOCKER_STATE"'
    stop = "docker compose stop scheduler controller bootstrap"
    restart = section.split("Validate the complete saved service set", 1)[1]
    validate = 'done < "$DOCKER_STATE"'
    controller_start = "docker compose start controller"
    scheduler_start = "docker compose start scheduler"
    assert section.index(capture) < section.index(stop)
    assert restart.index(validate) < restart.index(controller_start)
    assert section.index('STOPPED_STATE="$BACKUP/docker-running-after-stop.txt"') < section.index(
        "docker compose run --rm --no-deps -T --entrypoint tar controller"
    )
    assert "Bootstrap is still running; wait for it to exit before backup" in section
    assert "docker compose start bootstrap" not in section
    assert "docker compose up --detach" not in section
    assert 'if grep -Fxq controller "$DOCKER_STATE"; then' in section
    assert '"$HEALTH_URL" || exit 1' in section
    assert restart.index(controller_start) < restart.index(scheduler_start)
    assert "Compose writer is still running" in section
    assert "Recorded Compose service did not restart" in section


def test_sole_provider_removal_fails_closed_until_a_replacement_exists() -> None:
    guide = (ROOT / "docs/backup.md").read_text(encoding="utf-8")
    section = guide.split("## Private-data lifecycle and removal", 1)[1]
    normalized = " ".join(section.split())

    assert "rejects an empty `discovery.sources` list" in normalized
    assert "deleting the setting selects the default iNaturalist source" in normalized
    assert (
        "Do not restart the controller until a valid replacement source is configured" in normalized
    )
    assert "Only after the replacement configuration validates" in normalized
