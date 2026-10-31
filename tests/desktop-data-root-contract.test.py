#!/usr/bin/env python3
"""The desktop data root resolves the same way in Python and shell, and every
derived path (run dir, bundled node) follows it.

Each case runs under a temp HOME, with the launcher's env removed, against a
temp copy of the engine checkout so a real shipped rule cannot leak in:
SUTANDO_APP_SUPPORT set, then marker x copied-record x new dir x old dir, each
row checked against the desktop app's data-folder rule. When that rule is
shipped beside the checkout, both resolvers use it. More specific overrides
(SUTANDO_RUN_DIR, SUTANDO_APP_NODE_DIR) still win.

Run: python3 tests/desktop-data-root-contract.test.py
"""
from __future__ import annotations

import importlib.util
import itertools
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER_VARS = ("SUTANDO_APP_SUPPORT", "SUTANDO_RUN_DIR", "SUTANDO_APP_NODE_DIR",
                 "SUTANDO_NODE", "XDG_RUNTIME_DIR")
IS_DARWIN = sys.platform == "darwin"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(REPO / "src"))
ddr = _load("desktop_data_root_under_test", REPO / "src" / "desktop_data_root.py")
rundir = _load("rundir", REPO / "src" / "runtime-api" / "rundir.py")
rundir_ddr = sys.modules["desktop_data_root"]

# Reference: the desktop app's data-folder rule, copied verbatim.
DESKTOP_RULE = r'''
# ag2_app_support_dir <home> [<uname -s>]
# macOS: space.ag2.desktop once the app moved there (its marker, or the copy's
# own record when the marker could not be written) or on a new install, else
# the old space.ag2.app, which macOS types as an application bundle.
ag2_app_support_dir() {
  local base="$1/Library/Application Support" os="${2:-$(uname -s)}"
  if [ "$os" = Darwin ] && { [ -e "$base/.space.ag2.desktop.ready" ] || [ -f "$base/space.ag2.desktop/.copied-from-space.ag2.app" ] || [ ! -d "$base/space.ag2.app" ]; }; then
    printf '%s\n' "$base/space.ag2.desktop"
  else
    printf '%s\n' "$base/space.ag2.app"
  fi
}
'''

# A shipped rule that disagrees with every built-in answer.
FOREIGN_RULE = '''ag2_app_support_dir() { printf '%s\\n' "$1/foreign-root"; }
'''


class _TempHome(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        tmp = Path(self._td.name)
        self.home = tmp / "home"
        self.support = self.home / "Library" / "Application Support"
        self.support.mkdir(parents=True)
        self.desktop = self.support / "space.ag2.desktop"
        self.legacy = self.support / "space.ag2.app"
        self.marker = self.support / ".space.ag2.desktop.ready"
        self.copied = self.desktop / ".copied-from-space.ag2.app"
        self.engine = tmp / "engine"
        (self.engine / "sutando" / "scripts").mkdir(parents=True)
        for name in ("sutando-config.sh", "python-binary.sh"):
            shutil.copy2(REPO / "scripts" / name, self.engine / "sutando" / "scripts" / name)
        self.cfg = self.engine / "sutando" / "scripts" / "sutando-config.sh"
        self.shipped = self.engine / "app-support.sh"
        self._saved = {k: os.environ.get(k) for k in LAUNCHER_VARS + ("HOME",)}
        for k in LAUNCHER_VARS:
            os.environ.pop(k, None)
        os.environ["HOME"] = str(self.home)
        self._saved_rule = rundir_ddr.SHIPPED_RULE
        rundir_ddr.SHIPPED_RULE = self.shipped

    def tearDown(self):
        rundir_ddr.SHIPPED_RULE = self._saved_rule
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._td.cleanup()

    def shell(self, sub: str, **extra) -> str:
        env = {k: v for k, v in os.environ.items() if k not in LAUNCHER_VARS}
        env.update(HOME=str(self.home), **extra)
        return subprocess.run(["bash", str(self.cfg), sub], env=env, capture_output=True,
                              text=True, check=True).stdout

    def py_root(self, **extra) -> Path:
        return ddr.desktop_data_root(env=extra, home=self.home, shipped=self.shipped)

    def assert_both(self, expected: Path, **extra):
        self.assertEqual(self.py_root(**extra), expected)
        self.assertEqual(self.shell("desktop-data-root", **extra), str(expected))

    def reference(self) -> str:
        return subprocess.run(
            ["bash", "-c", DESKTOP_RULE + '\nag2_app_support_dir "$1" Darwin', "_", str(self.home)],
            capture_output=True, text=True, check=True).stdout.rstrip("\n")

    def build(self, marker: bool, copied: bool, new: bool, old: bool):
        for f in (self.marker, self.copied):
            if f.exists():
                f.unlink()
        for d in (self.desktop, self.legacy):
            if d.is_dir():
                d.rmdir()
        if new or copied:
            self.desktop.mkdir()
        if copied:
            self.copied.write_text("")
        if marker:
            self.marker.write_text("")
        if old:
            self.legacy.mkdir()


# (marker, copied record, new dir, old dir) -> expected root; a copied record implies the new dir.
STATES = [
    ((False, False, False, False), "desktop"),
    ((False, False, False, True), "legacy"),
    ((False, False, True, False), "desktop"),
    ((False, False, True, True), "legacy"),
    ((False, True, True, False), "desktop"),
    ((False, True, True, True), "desktop"),
    ((True, False, False, False), "desktop"),
    ((True, False, False, True), "desktop"),
    ((True, False, True, False), "desktop"),
    ((True, False, True, True), "desktop"),
    ((True, True, True, False), "desktop"),
    ((True, True, True, True), "desktop"),
]


class BuiltInRuleMatchesTheDesktop(_TempHome):
    def test_table_covers_every_meaningful_state(self):
        meaningful = {s for s in itertools.product((False, True), repeat=4) if not (s[1] and not s[2])}
        self.assertEqual({s for s, _ in STATES}, meaningful)

    def test_every_state_matches_table_and_desktop_rule(self):
        for state, want in STATES:
            with self.subTest(marker=state[0], copied=state[1], new=state[2], old=state[3]):
                self.build(*state)
                expected = self.desktop if want == "desktop" else self.legacy
                self.assert_both(expected)
                self.assertEqual(self.reference(), str(expected))

    def test_marker_without_new_folder_picks_new(self):
        self.build(True, False, False, True)
        self.assert_both(self.desktop)

    def test_copied_but_not_marked_picks_new(self):
        self.build(False, True, True, True)
        self.assert_both(self.desktop)

    def test_copied_record_as_directory_is_not_a_record(self):
        self.build(False, False, True, True)
        self.copied.mkdir()
        self.assert_both(self.legacy)
        self.assertEqual(self.reference(), str(self.legacy))

    def test_app_support_env_wins(self):
        self.build(True, False, True, True)
        self.shipped.write_text(FOREIGN_RULE)
        custom = self.home / "custom-root"
        self.assert_both(custom, SUTANDO_APP_SUPPORT=str(custom))

    def test_empty_app_support_env_is_unset(self):
        self.build(True, False, True, True)
        self.assert_both(self.desktop, SUTANDO_APP_SUPPORT="")


class ShippedRuleIsTheSource(_TempHome):
    def test_shipped_rule_wins_over_built_in(self):
        self.build(False, False, False, True)
        self.shipped.write_text(FOREIGN_RULE)
        self.assert_both(self.home / "foreign-root")

    def test_shipped_desktop_rule_agrees_with_built_in(self):
        self.shipped.write_text(DESKTOP_RULE)
        for state, want in STATES:
            with self.subTest(state=state):
                self.build(*state)
                self.assert_both(self.desktop if want == "desktop" else self.legacy)

    def test_broken_shipped_rule_falls_back(self):
        self.build(False, False, False, True)
        for body in ("", "exit 3\n", "ag2_app_support_dir() { return 1; }\n",
                     "ag2_app_support_dir() { :; }\n",
                     "ag2_app_support_dir() { printf 'a\\nb\\n'; }\n"):
            with self.subTest(body=body):
                self.shipped.write_text(body)
                self.assert_both(self.legacy)

    def test_hung_shipped_rule_falls_back_in_python(self):
        self.build(False, False, False, True)
        self.shipped.write_text("ag2_app_support_dir() { sleep 30; }\n")
        saved = ddr.SHIPPED_TIMEOUT_S
        ddr.SHIPPED_TIMEOUT_S = 0.5
        try:
            self.assertEqual(self.py_root(), self.legacy)
        finally:
            ddr.SHIPPED_TIMEOUT_S = saved

    def test_rundir_py_uses_shipped_rule(self):
        self.shipped.write_text(FOREIGN_RULE)
        saved = rundir.sys.platform
        rundir.sys.platform = "darwin"
        try:
            self.assertEqual(rundir.run_dir(), self.home / "foreign-root" / "run")
        finally:
            rundir.sys.platform = saved


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
        self.legacy.mkdir()
        self.assertEqual(self.shell("run-dir"), str(self.legacy / "run"))

    def test_rundir_py_follows_root_and_matches_shell(self):
        saved = rundir.sys.platform
        rundir.sys.platform = "darwin"
        try:
            self.assertEqual(rundir.run_dir(), self.desktop / "run")
            self.legacy.mkdir()
            self.assertEqual(rundir.run_dir(), self.legacy / "run")
            self.desktop.mkdir()
            self.assertEqual(rundir.run_dir(), self.legacy / "run")
            (self.support / ".space.ag2.desktop.ready").write_text("")
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
