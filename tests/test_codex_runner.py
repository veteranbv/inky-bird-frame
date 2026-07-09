from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from inky_bird_frame.codex_runner import CodexRunner


class CodexRunnerTests(unittest.TestCase):
    def test_noninteractive_command_allows_deployment_workspace(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "codex"
            executable.touch()
            runner = CodexRunner(executable, root)

            command = runner._base_command(writable=False)

        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("read-only", command)


if __name__ == "__main__":
    unittest.main()
