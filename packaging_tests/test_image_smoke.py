from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SMOKE = Path(__file__).resolve().parents[1] / "scripts/smoke-image.sh"


class ImageSmokeTests(unittest.TestCase):
    def run_smoke(self, **statuses):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine, calls = root / "engine", root / "calls"
            engine.write_text(
                """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["SMOKE_CALLS"]).open("a") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] == ["volume", "create"]:
    print(args[2])
if "guild-private-connector-health" in args:
    mode = "HELP" if "--help" in args else "READY" if "--ready" in args else "LIVE"
    sys.exit(int(os.environ[mode]))
"""
            )
            engine.chmod(0o700)
            result = subprocess.run(
                ["bash", str(SMOKE), "smoke-test-image"],
                env={
                    **os.environ,
                    "CONTAINER_ENGINE": str(engine),
                    "SMOKE_CALLS": str(calls),
                    "HELP": "0",
                    "LIVE": "1",
                    "READY": "1",
                    **{key: str(value) for key, value in statuses.items()},
                },
                capture_output=True,
                text=True,
                timeout=20,
            )
            return result, [json.loads(line) for line in calls.read_text().splitlines()]

    def test_packaged_health_help_and_both_missing_state_probes(self):
        result, calls = self.run_smoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        health_calls = [
            call for call in calls if "guild-private-connector-health" in call
        ]
        self.assertEqual(len(health_calls), 3)
        self.assertEqual(health_calls[0][-1], "--help")
        self.assertEqual(
            health_calls[1][-2:], ["--state-file", "/tmp/missing-smoke-health.json"]
        )
        self.assertEqual(
            health_calls[2][-3:],
            ["--state-file", "/tmp/missing-smoke-health.json", "--ready"],
        )
        for call in health_calls:
            self.assertIn("--read-only", call)
            self.assertIn("--cap-drop=ALL", call)
            self.assertIn("--security-opt=no-new-privileges", call)
            self.assertIn("10001:10001", call)

    def test_unexpected_probe_statuses_fail_smoke(self):
        for mode in ("LIVE", "READY"):
            for status in (0, 2, 125, 126, 127):
                with self.subTest(mode=mode, status=status):
                    result, _ = self.run_smoke(**{mode: status})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(f"got {status}", result.stderr)

    def test_health_help_failure_stops_smoke(self):
        for status in (1, 2, 125, 126, 127):
            with self.subTest(status=status):
                result, calls = self.run_smoke(HELP=status)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    len(
                        [
                            call
                            for call in calls
                            if "guild-private-connector-health" in call
                        ]
                    ),
                    1,
                )
