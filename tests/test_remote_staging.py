"""Execute the real remote wrapper against local, secret-free SSH/rsync doubles."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "installed,authorized",
    [(None, True), (None, False), ("inky-bird", True), ("inky-bird-frame", True)],
)
def test_local_display_preflight_preserves_active_installs_and_allows_fresh(
    tmp_path: Path, installed: str | None, authorized: bool
) -> None:
    source = tmp_path / "source"
    app, support, venv = (tmp_path / name for name in ("app", "support", "venv"))
    for directory in (
        source / "src",
        source / "catalog",
        source / "deploy",
        source / "docs",
        app,
        support,
        venv / "bin",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        ROOT / "deploy/install-display-local.sh", source / "deploy/install-display-local.sh"
    )
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "docs/browser-access.md"):
        (source / name).write_text("public-source-sentinel")
    sentinel = app / "runtime-sentinel"
    sentinel.write_text("old runtime")
    config = support / "config.toml"
    config.write_text("private-config-sentinel")
    config.chmod(0o644)
    log = tmp_path / "calls.jsonl"
    family = installed or "inky-bird"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    layout = "\n".join(
        f"{name}={shlex.quote(str(value))}"
        for name, value in (
            ("service_prefix", family),
            ("app_dir", app),
            ("support_dir", support),
            ("config_path", config),
        )
    )
    (bin_dir / "python3").write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(layout) + "\n")
    (bin_dir / "uname").write_text("#!/bin/sh\nprintf '%s\\n' Linux\n")
    (bin_dir / "systemctl").write_text(
        "#!/bin/sh\ncase \"$1\" in show) printf '%s\\n' active;; esac\n"
    )
    (bin_dir / "sudo").write_text(
        f"#!{sys.executable}\nimport json,sys\n"
        f"with open({str(log)!r},'a') as f:\n"
        " f.write(json.dumps(['sudo',*sys.argv[1:]])+'\\n')\n"
        f"sys.exit({0 if authorized else 1})\n"
    )
    (venv / "bin/python").write_text(
        f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n"
        f"with open({str(log)!r},'a') as f: f.write(json.dumps(['python',*sys.argv[1:]])+'\\n')\n"
        "if sys.argv[1:4] == ['-m','pip','show']:\n"
        f" sys.exit(0 if sys.argv[4] == {installed!r} else 1)\n"
        "if sys.argv[1] == '-':\n"
        " prefix=os.environ['INKY_BIRD_SERVICE_PREFIX']\n"
        " for suffix in ('service','timer'):\n"
        "  (Path(sys.argv[2]) / (prefix+'-display.'+suffix)).write_text('fixture unit')\n"
    )
    (bin_dir / "rsync").write_text(
        f"#!{sys.executable}\nimport shutil,sys\nfrom pathlib import Path\n"
        "a,b=map(Path,sys.argv[-2:]); shutil.copytree(a,b,dirs_exist_ok=True)\n"
    )
    for executable in [*bin_dir.iterdir(), venv / "bin/python"]:
        executable.chmod(0o755)
    result = subprocess.run(
        ["bash", str(source / "deploy/install-display-local.sh")],
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.defpath}",
            "HOME": str(tmp_path),
            "INKY_BIRD_DISPLAY_VENV": str(venv),
            "INKY_BIRD_NONINTERACTIVE_SUDO": "true",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (1 if installed or not authorized else 0), result.stderr
    assert sentinel.read_text() == "old runtime"
    assert config.read_text() == "private-config-sentinel"
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    if installed or not authorized:
        assert "No changes made" in result.stderr
        assert not any("install" in command or "uninstall" in command for command in commands)
        if installed:
            assert not any(command[0] == "sudo" for command in commands)
        assert config.stat().st_mode & 0o777 == 0o644
    else:
        assert any(command[:4] == ["python", "-m", "pip", "install"] for command in commands)
        assert not any("uninstall" in command for command in commands)


@pytest.mark.parametrize("failure", [None, "refuse", "transfer", "capacity", "bad-stage"])
@pytest.mark.parametrize("fresh", [False, True])
def test_remote_staging_never_precopies_live_targets(
    tmp_path: Path, failure: str | None, fresh: bool
) -> None:
    source = tmp_path / "source"
    for directory in ("deploy", "src", "catalog", "docs"):
        (source / directory).mkdir(parents=True)
    shutil.copyfile(
        ROOT / "deploy/install-display-node.sh", source / "deploy/install-display-node.sh"
    )
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "docs/browser-access.md"):
        (source / name).write_text("public-source-sentinel")
    receipt, calls = tmp_path / "receipt.json", tmp_path / "calls.jsonl"
    local = source / "deploy/install-display-local.sh"
    local.write_text(
        "#!/bin/sh\n"
        + (
            "exit 41\n"
            if failure == "refuse"
            else f"exec {shlex.quote(sys.executable)} -c "
            + shlex.quote(
                "import json,os; from pathlib import Path; "
                f"Path({str(receipt)!r}).write_text(json.dumps({{name:os.environ[name] for name in "
                "['INKY_BIRD_APP_DIR','INKY_BIRD_CONFIG_PATH','INKY_BIRD_DISPLAY_VENV',"
                "'INKY_BIRD_RUN_INITIAL_DISPLAY','INKY_BIRD_NONINTERACTIVE_SUDO']}))"
            )
            + "\n"
        )
    )
    local.chmod(0o755)
    home = tmp_path / "remote home"
    app = home / "Services" / "inky-bird-frame"
    home.mkdir()
    if not fresh:
        app.mkdir(parents=True)
        (app / "runtime-sentinel").write_text("unchanged runtime")
    config = home / "private config.toml"
    config.write_text("unchanged private config")
    venv = home / "private venv"
    key = tmp_path / 'synthetic key\'s "quoted" \\path'
    key.touch(mode=0o600)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_ssh = bin_dir / "ssh"
    fake_ssh.write_text(
        f"#!{sys.executable}\nimport json,shlex,subprocess,sys\n"
        f"with open({str(calls)!r},'a') as f: f.write(json.dumps(['ssh',sys.argv[-1]])+'\\n')\n"
        "if '--server' in sys.argv:\n"
        f" with open({str(calls)!r},'a') as f: "
        "f.write(json.dumps(['rsync-ssh',*sys.argv[1:]])+'\\n')\n"
        " sys.exit(0)\n"
        "command=sys.argv[-1]\n"
        + (
            "if command.startswith('bash -s --') and len(shlex.split(command)) == 6:\n"
            " print('/unrecognized/stage'); sys.exit(0)\n"
            if failure == "bad-stage"
            else ""
        )
        + "sys.exit(subprocess.run(['bash','-c',command],check=False).returncode)\n"
    )
    fake_ssh.chmod(0o755)
    fake_rsync = bin_dir / "rsync"
    real_rsync = shutil.which("rsync")
    assert real_rsync is not None, "The real rsync argument parser is required"
    fake_rsync.write_text(
        f"#!{sys.executable}\nimport json,shutil,subprocess,sys\nfrom pathlib import Path\n"
        f"with open({str(calls)!r},'a') as f: "
        "f.write(json.dumps(['rsync',sys.argv[-2],sys.argv[-1]])+'\\n')\n"
        + ("sys.exit(42)\n" if failure == "transfer" else "")
        + f"subprocess.run([{real_rsync!r},'--dry-run',*sys.argv[1:]],"
        "capture_output=True,check=False)\n"
        f"parsed=[json.loads(line)[1:] for line in Path({str(calls)!r}).read_text().splitlines() "
        "if json.loads(line)[0]=='rsync-ssh']\n"
        f"assert parsed and parsed[-1][:6]==['-i',{str(key)!r},"
        "'-o','BatchMode=yes','-o','IdentitiesOnly=yes'], parsed\n"
        + "source=Path(sys.argv[-2]); target=Path(sys.argv[-1].split(':',1)[1])\n"
        "if source.is_dir(): shutil.copytree(source,target,dirs_exist_ok=True)\n"
        "else: shutil.copy2(source,target / source.name if target.is_dir() else target)\n"
    )
    fake_rsync.chmod(0o755)
    if failure == "capacity":
        (bin_dir / "df").write_text(
            "#!/bin/sh\nprintf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n"
            "fixture 100 99 1 99%% /\\n'\n"
        )
        (bin_dir / "df").chmod(0o755)
    environment = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "INKY_BIRD_DISPLAY_HOST": "display.example.test",
        "INKY_BIRD_DISPLAY_USER": "fixture",
        "INKY_BIRD_DISPLAY_SSH_KEY": str(key),
        "INKY_BIRD_DISPLAY_APP_DIR": str(app),
        "INKY_BIRD_DISPLAY_CONFIG_PATH": str(config),
        "INKY_BIRD_DISPLAY_VENV": str(venv),
    }
    result = subprocess.run(
        ["bash", str(source / "deploy/install-display-node.sh")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (
        result.returncode
        == {None: 0, "refuse": 41, "transfer": 42, "capacity": 1, "bad-stage": 1}[failure]
    ), result.stderr
    if fresh:
        assert not app.exists()
    else:
        assert (app / "runtime-sentinel").read_text() == "unchanged runtime"
        assert list(app.iterdir()) == [app / "runtime-sentinel"]
    assert config.read_text() == "unchanged private config"
    assert not list(app.parent.glob(".inky-bird-stage.*"))
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    if failure in ("capacity", "bad-stage"):
        assert not any(call[0] == "rsync" for call in recorded)
        assert ("recognized" if failure == "bad-stage" else "Not enough") in result.stderr
    for call in recorded:
        if call[0] == "rsync":
            destination = call[-1].split(":", 1)[1]
            assert "/.inky-bird-stage." in destination
            assert not destination.startswith(str(app) + "/")
    if failure is None:
        values = json.loads(receipt.read_text())
        assert values["INKY_BIRD_APP_DIR"] == str(app)
        assert values["INKY_BIRD_CONFIG_PATH"] == str(config)
        assert values["INKY_BIRD_DISPLAY_VENV"] == str(venv)
        assert values["INKY_BIRD_RUN_INITIAL_DISPLAY"] == "false"
        assert values["INKY_BIRD_NONINTERACTIVE_SUDO"] == "true"
    else:
        assert not receipt.exists()
