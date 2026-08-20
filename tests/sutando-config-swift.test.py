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

# Shared with tests/sutando-config-python-resolution.test.py: one warm Clang
# module cache for every Swift probe compile, instead of one cold cache each.
_MODULE_CACHE = Path(tempfile.gettempdir()) / "sutando-swift-module-cache"
_MODULE_CACHE.mkdir(parents=True, exist_ok=True)


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
        # Persistent, NOT under self.tmp: setUp runs per test method, so a per-run
        # cache rebuilt Foundation's modules on every single one of them.
        env["CLANG_MODULE_CACHE_PATH"] = str(_MODULE_CACHE)
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


@unittest.skipUnless(shutil.which("swiftc"), "swiftc not available")
class TestPersonalAssetPathSwift(unittest.TestCase):
    """Compiles the REAL SutandoConfig.swift and calls the real functions rather than
    re-implementing the resolution order, so a regression in it cannot stay green."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sutando-swift-asset-"))
        self.probe_dir = self.tmp / "probe"
        self.probe_dir.mkdir()
        self.probe = self.probe_dir / "asset-probe"
        (self.probe_dir / "main.swift").write_text(
            "import Foundation\n"
            "let mode = CommandLine.arguments[1]\n"
            "if mode == \"hostlabel\" {\n"
            "    print(SutandoConfig.hostLabel())\n"
            "} else {\n"
            "    print(SutandoConfig.personalAssetPath(CommandLine.arguments[3],\n"
            "                                          workspace: CommandLine.arguments[2]))\n"
            "}\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        # Persistent, NOT under self.tmp: setUp runs per test method, so a per-run
        # cache rebuilt Foundation's modules on every single one of them.
        env["CLANG_MODULE_CACHE_PATH"] = str(_MODULE_CACHE)
        subprocess.run(
            ["swiftc", str(SWIFT_CONFIG), str(self.probe_dir / "main.swift"), "-o", str(self.probe)],
            env=env, check=True, text=True, capture_output=True,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, args: list[str], extra_env: dict[str, str] | None = None) -> str:
        env = os.environ.copy()
        env.pop("SUTANDO_TEST_MODE", None)
        for k in ("SUTANDO_HOST_LABEL", "SUTANDO_HOST_OVERRIDE"):
            env.pop(k, None)
        env.update(extra_env or {})
        proc = subprocess.run([str(self.probe), *args], env=env, text=True,
                              capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    # --- hostLabel() precedence -------------------------------------------------
    def test_host_label_prefers_explicit_env(self) -> None:
        self.assertEqual(
            self._run(["hostlabel"], {"SUTANDO_HOST_LABEL": "PinnedHost"}), "PinnedHost"
        )

    def test_host_label_honors_legacy_override_alias(self) -> None:
        self.assertEqual(
            self._run(["hostlabel"], {"SUTANDO_HOST_OVERRIDE": "LegacyHost"}), "LegacyHost"
        )

    def test_host_label_new_name_wins_over_legacy_alias(self) -> None:
        self.assertEqual(
            self._run(["hostlabel"],
                      {"SUTANDO_HOST_LABEL": "NewName", "SUTANDO_HOST_OVERRIDE": "OldName"}),
            "NewName",
        )

    def test_host_label_ignores_whitespace_only_env(self) -> None:
        """An empty/blank override must fall through, not yield an empty label —
        an empty label would build `hosts//stand-avatar.png`."""
        label = self._run(["hostlabel"], {"SUTANDO_HOST_LABEL": "   "})
        self.assertNotEqual(label, "")
        self.assertNotIn("/", label)

    # --- personalAssetPath() selection ------------------------------------------
    def _workspace(self, *, per_host: bool, legacy: bool) -> Path:
        ws = self.tmp / f"ws-{per_host}-{legacy}"
        if per_host:
            d = ws / "hosts" / "PinnedHost"
            d.mkdir(parents=True, exist_ok=True)
            (d / "stand-avatar.png").write_bytes(b"per-host")
        if legacy:
            d = ws / "assets"
            d.mkdir(parents=True, exist_ok=True)
            (d / "stand-avatar.png").write_bytes(b"legacy")
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def test_per_host_asset_wins_when_both_exist(self) -> None:
        ws = self._workspace(per_host=True, legacy=True)
        got = self._run(["asset", str(ws), "stand-avatar.png"],
                        {"SUTANDO_HOST_LABEL": "PinnedHost"})
        self.assertEqual(got, str(ws / "hosts" / "PinnedHost" / "stand-avatar.png"))

    def test_falls_back_to_legacy_assets_when_only_legacy_exists(self) -> None:
        ws = self._workspace(per_host=False, legacy=True)
        got = self._run(["asset", str(ws), "stand-avatar.png"],
                        {"SUTANDO_HOST_LABEL": "PinnedHost"})
        self.assertEqual(got, str(ws / "assets" / "stand-avatar.png"))

    def test_returns_per_host_path_when_neither_exists(self) -> None:
        """Contract mirrors util_paths.personal_path(): return the per-host path so
        the caller's own existence check fails gracefully."""
        ws = self._workspace(per_host=False, legacy=False)
        got = self._run(["asset", str(ws), "stand-avatar.png"],
                        {"SUTANDO_HOST_LABEL": "PinnedHost"})
        self.assertEqual(got, str(ws / "hosts" / "PinnedHost" / "stand-avatar.png"))

    def test_per_host_path_tracks_the_resolved_label(self) -> None:
        """A different label must select a different per-host directory — proves the
        path is built from hostLabel() rather than a hardcoded host."""
        ws = self._workspace(per_host=False, legacy=False)
        got = self._run(["asset", str(ws), "stand-avatar.png"],
                        {"SUTANDO_HOST_LABEL": "OtherHost"})
        self.assertEqual(got, str(ws / "hosts" / "OtherHost" / "stand-avatar.png"))


if __name__ == "__main__":
    unittest.main()
