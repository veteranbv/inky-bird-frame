from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from inky_bird_frame.cli import build_parser, main


class CliTests(unittest.TestCase):
    def test_status_requires_explicit_config(self) -> None:
        args = build_parser().parse_args(["status", "--config", "instance.toml"])

        self.assertEqual(str(args.config), "instance.toml")

    def test_expected_error_uses_json_envelope(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = main(["status", "--config", "missing.toml"])

        self.assertEqual(exit_code, 1)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["error"]["type"], "ConfigurationError")


if __name__ == "__main__":
    unittest.main()
