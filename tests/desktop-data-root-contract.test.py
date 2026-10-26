#!/usr/bin/env python3
"""The desktop data root resolves the same way in Python and shell, and every
derived path (run dir, bundled node) follows it.

Each branch runs under a temp HOME with the launcher's env removed:
SUTANDO_APP_SUPPORT set, space.ag2.desktop present, only space.ag2.app, and
neither. More specific overrides (SUTANDO_RUN_DIR, SUTANDO_APP_NODE_DIR) still win.

Run: python3 tests/desktop-data-root-contract.test.py
"""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CFG = REPO / "scripts" / "sutando-config.sh"
LAUNCHER_VARS = ("SUTANDO_APP_SUPPORT", "SUTANDO_RUN_DIR", "SUTANDO_APP_NODE_DIR",
                 "SUTANDO_NODE", "XDG_RUNTIME_DIR")
IS_DARWIN = sys.platform == "darwin"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(REPO / "src"))
ddr = _load("desktop_data_root", REPO / "src" / "desktop_data_root.py")
rundir = _load("rundir", REPO / "src" / "runtime-api" / "rundir.py")


class _TempHome(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.home = Path(self._td.name)
        self.support = self.home / "Library" / "Application Support"
        self.support.mkdir(parents=True)
        self.desktop = self.support / "space.ag2.desktop"
        self.legacy = self.support / "space.ag2.app"
        self._saved = {k: os.environ.get(k) for k in LAUNCHER_VARS + ("HOME",)}
        for k in LAUNCHER_VARS:
            os.environ.pop(k, None)
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()

    def shell(self, sub: str, **extra) -> str:
        env = {k: v for k, v in os.environ.items() if k not in LAUNCHER_VARS}
        env.update(HOME=str(self.home), **extra)
        return subprocess.run(["bash", str(CFG), sub], env=env, capture_output=True,
                              text=True, check=True).stdout

    def py_root(self, **extra) -> Path:
        return ddr.desktop_data_root(env=extra, home=self.home)


class ResolverAgreesAcrossLanguages(_TempHome):
    def assert_both(self, expected: Path, **extra):
        self.assertEqual(self.py_root(**extra), expected)
        self.assertEqual(self.shell("desktop-data-root", **extra), str(expected))

    def test_neither_folder_falls_back_to_legacy(self):
        self.assert_both(self.legacy)

    def test_only_legacy_folder(self):
        self.legacy.mkdir()
        self.assert_both(self.legacy)

    def test_desktop_folder_wins_once_present(self):
        self.legacy.mkdir()
        self.desktop.mkdir()
        self.assert_both(self.desktop)

    def test_desktop_folder_alone(self):
        self.desktop.mkdir()
        self.assert_both(self.desktop)

    def test_app_support_env_wins(self):
        self.desktop.mkdir()
        custom = self.home / "custom-root"
        self.assert_both(custom, SUTANDO_APP_SUPPORT=str(custom))

    def test_empty_app_support_env_is_unset(self):
        self.desktop.mkdir()
        self.assert_both(self.desktop, SUTANDO_APP_SUPPORT="")

    def test_desktop_name_as_file_is_not_a_root(self):
        self.desktop.write_text("")
        self.assert_both(self.legacy)


class DerivedPathsFollowTheRoot(_TempHome):
    def test_app_node_dir_follows_root(self):
        self.desktop.mkdir()
        self.assertEqual(self.shell("app-node-dir"),
                         str(self.desktop / "engine" / "runtime" / "node" / "bin"))
        self.assertEqual(self.shell("app-node-dir", SUTANDO_APP_NODE_DIR="/x/bin"), "/x/bin")

    def test_node_bin_finds_node_under_desktop_root(self):
        node_dir = self.desktop / "engine" / "runtime" / "node" / "bin"
        node_dir.mkdir(parents=True)
        node = node_dir / "node"
        node.write_text("#!/bin/sh\n")
        node.chmod(node.stat().st_mode | stat.S_IXUSR)
        self.assertEqual(self.shell("node-bin"), str(node))

    @unittest.skipUnless(IS_DARWIN, "the shell run-dir darwin branch keys on uname")
    def test_shell_run_dir_follows_root(self):
        self.desktop.mkdir()
        self.assertEqual(self.shell("run-dir"), str(self.desktop / "run"))
        self.assertEqual(self.shell("run-dir", SUTANDO_APP_SUPPORT="/r"), "/r/run")
        self.assertEqual(self.shell("run-dir", SUTANDO_APP_SUPPORT="/r",
                                    SUTANDO_RUN_DIR="/explicit"), "/explicit")
        self.desktop.rmdir()
        self.assertEqual(self.shell("run-dir"), str(self.legacy / "run"))

    def test_rundir_py_follows_root_and_matches_shell(self):
        saved = rundir.sys.platform
        rundir.sys.platform = "darwin"
        try:
            self.assertEqual(rundir.run_dir(), self.legacy / "run")
            self.desktop.mkdir()
            self.assertEqual(rundir.run_dir(), self.desktop / "run")
            os.environ["SUTANDO_APP_SUPPORT"] = "/r"
            self.assertEqual(rundir.run_dir(), Path("/r/run"))
            os.environ["SUTANDO_RUN_DIR"] = "/explicit"
            self.assertEqual(rundir.run_dir(), Path("/explicit"))
        finally:
            rundir.sys.platform = saved
        if IS_DARWIN:
            os.environ.pop("SUTANDO_RUN_DIR")
            os.environ.pop("SUTANDO_APP_SUPPORT")
            self.assertEqual(self.shell("run-dir"), str(rundir.run_dir()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
