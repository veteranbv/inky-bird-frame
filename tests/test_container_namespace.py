"""A repository rename must not strand existing container installations."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/container.yml"
SOURCE_SHA = "a" * 40


@pytest.mark.parametrize("repository", ["veteranbv/inky-bird-frame", "veteranbv/inky-bird"])
@pytest.mark.parametrize("event", ["workflow_run", "release", "workflow_dispatch"])
def test_image_tags_keep_the_compatibility_namespace(
    tmp_path: Path, repository: str, event: str
) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "CONTROLLER_IMAGE: ghcr.io/${{ github.repository_owner }}/inky-bird\n" in workflow
    assert "COMPATIBILITY_IMAGE: ghcr.io/${{ github.repository_owner }}/inky-bird-frame" in workflow
    step = workflow.split("      - name: Validate image tag\n", 1)[1]
    step = step.split("\n      - name:", 1)[0]
    script = dedent(step.split("        run: |\n", 1)[1])

    # No network or repository mutation: only the tag-producing shell is run.
    fake_git = tmp_path / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  fetch) exit 0 ;;\n"
        f"  rev-parse) printf '%s\\n' '{SOURCE_SHA}' ;;\n"
        "  show) printf '%s\\n' '2026-01-01T00:00:00Z' ;;\n"
        "  *) exit 90 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    output = tmp_path / "github-output"
    result = subprocess.run(
        ["bash", "-eu", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "EVENT_NAME": event,
            "GITHUB_REPOSITORY": repository,
            "REPOSITORY": repository,
            "CONTROLLER_IMAGE": "ghcr.io/veteranbv/inky-bird",
            "COMPATIBILITY_IMAGE": "ghcr.io/veteranbv/inky-bird-frame",
            "REQUESTED_TAG": "preview",
            "GITHUB_REF_NAME": "v1.2.3",
            "SOURCE_SHA": SOURCE_SHA,
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = output.read_text(encoding="utf-8").splitlines()
    tags = lines[lines.index("tags<<EOF") + 1 : lines.index("EOF")]
    expected = {
        "workflow_run": [f"sha-{SOURCE_SHA}", "latest"],
        "release": ["1.2.3"],
        "workflow_dispatch": ["preview"],
    }
    assert tags == [
        f"ghcr.io/veteranbv/{image}:{tag}"
        for tag in expected[event]
        for image in ("inky-bird", "inky-bird-frame")
    ]


def test_release_bundle_uses_the_same_image_but_current_repository_links() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    bundle = workflow.split("      - name: Build Docker deployment bundle\n", 1)[1]
    assert "INKY_BIRD_IMAGE=${CONTROLLER_IMAGE}:${VERSION}" in bundle
    assert "https://github.com/${REPOSITORY}/blob/v${VERSION}/docs/docker.md" in bundle
    assert 'image="ghcr.io/${REPOSITORY}"' not in workflow
    assert "INKY_BIRD_IMAGE=ghcr.io/${REPOSITORY}" not in workflow
    # This compatibility change does not introduce an unmerged publication path.
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.event.workflow_run.event == 'push'" in workflow
    assert "github.actor == github.repository_owner" in workflow


def test_latest_release_download_keeps_the_supported_asset_name() -> None:
    guide = (ROOT / "docs/docker.md").read_text()
    workflow = WORKFLOW.read_text()
    assert "releases/latest/download/inky-bird-frame-docker.tar.gz" in guide
    assert "releases/latest/download/inky-bird-docker.tar.gz" not in guide
    assert "inky-bird-frame-docker.tar.gz.sha256" in workflow
    assert 'cp "${bundle}.tar.gz" inky-bird-frame-docker.tar.gz' in workflow


@pytest.mark.parametrize("mismatch", [False, True])
def test_published_names_must_resolve_to_one_multiarch_attested_digest(
    tmp_path: Path, mismatch: bool
) -> None:
    import json

    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert workflow.count("uses: docker/build-push-action@") == 1
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    step = workflow.split("      - name: Verify both published names\n", 1)[1].split(
        "\n  release-bundle:", 1
    )[0]
    script = dedent(step.split("        run: |\n", 1)[1])
    expected = "sha256:" + "a" * 64
    manifest = {
        "digest": expected,
        "manifests": [
            {"platform": {"os": "linux", "architecture": arch}} for arch in ("amd64", "arm64")
        ]
        + [
            {
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
            }
        ]
        * 2,
    }
    good = json.dumps(manifest)
    manifest["digest"] = "sha256:" + "b" * 64
    bad = json.dumps(manifest)
    (tmp_path / "docker").write_text(
        "#!/bin/sh\ncase \"$4\" in\n *inky-bird-frame:*) printf '%s\\n' '"
        + (bad if mismatch else good)
        + "';;\n *) printf '%s\\n' '"
        + good
        + "';;\nesac\n"
    )
    (tmp_path / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for name in ("docker", "sleep"):
        (tmp_path / name).chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "EXPECTED_DIGEST": expected,
            "IMAGE_TAGS": "\n".join(
                (
                    "ghcr.io/veteranbv/inky-bird:latest",
                    "ghcr.io/veteranbv/inky-bird-frame:latest",
                )
            ),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode != 0) is mismatch, result.stderr
