#!/usr/bin/env python3
"""Swift resolver parity checks for Sutando.app.

Skipped on CI hosts without swiftc; runs on macOS developer machines.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SWIFT_CONFIG = ROOT / "src" / "Sutando" / "SutandoConfig.swift"


@unittest.skipUnless(shutil.which("swiftc"), "swiftc not available")
class TestSutandoConfigSwift(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sutando-swift-config-"))
        self.probe_dir = self.tmp / "probe"
        self.probe_dir.mkdir()
        self.probe = self.probe_dir / "sutando-config-probe"
        (self.probe_dir / "main.swift").write_text(
            "import Foundation\n"
            "let repo = CommandLine.arguments[1]\n"
            "print(SutandoConfig.resolveWorkspace(repoRoot: repo))\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["CLANG_MODULE_CACHE_PATH"] = str(self.tmp / "module-cache")
        subprocess.run(
            [
                "swiftc",
                str(SWIFT_CONFIG),
                str(self.probe_dir / "main.swift"),
                "-o",
                str(self.probe),
            ],
            env=env,
            check=True,
            text=True,
            capture_output=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_probe(self, repo: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("SUTANDO_TEST_MODE", None)
        env.update(extra_env or {})
        return subprocess.run(
            [str(self.probe), str(repo)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_env_set_returns_config_path_not_env_path(self) -> None:
        repo = self.tmp / "repo-config"
        repo.mkdir()
        (repo / "sutando.config.local.json").write_text(
            json.dumps({"workspace": {"path": "/from/swift/config"}}),
            encoding="utf-8",
        )

        proc = self.run_probe(repo, {"SUTANDO_WORKSPACE": "/from/swift/env"})

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "/from/swift/config")
        self.assertIn("NO LONGER HONORED", proc.stderr)
        self.assertNotIn("/from/swift/env", proc.stderr)

    def test_test_mode_still_honors_env_path(self) -> None:
        repo = self.tmp / "repo-test-mode"
        repo.mkdir()

        proc = self.run_probe(
            repo,
            {
                "SUTANDO_WORKSPACE": "/from/swift/test-env",
                "SUTANDO_TEST_MODE": "1",
            },
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "/from/swift/test-env")
        self.assertEqual(proc.stderr, "")


if __name__ == "__main__":
    unittest.main()
