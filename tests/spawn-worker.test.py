#!/usr/bin/env python3
"""spawn-worker skill: single-worker -> multi-worker through the installer.

Runs the real skill script against a fake repo in a temp dir: a recording
stub stands in for scripts/install-core-pool.sh and writes the plists the real
installer would, so the script's mode detection is exercised end to end. A temp
$HOME keeps the live launchd domain out of it.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT_REL = Path("skills") / "spawn-worker" / "scripts" / "spawn-worker.sh"

INSTALLER_STUB = """#!/bin/bash
# Records its argv, then materialises N worker plists plus the lead plist.
printf '%s\\n' "$*" >> "$INSTALLER_LOG"
[ -n "${INSTALLER_RC:-}" ] && exit "$INSTALLER_RC"
n="$1"
d="$HOME/Library/LaunchAgents"
rm -f "$d"/com.sutando.core-*.plist
i=1
while [ "$i" -le "$n" ]; do
  : > "$d/com.sutando.core-$i.plist"
  i=$((i + 1))
done
: > "$d/com.sutando.pool-lead.plist"
exit 0
"""

CONFIG_STUB = """#!/bin/bash
case "$1" in
  workspace) echo "$STUB_WORKSPACE" ;;
  *) exit 2 ;;
esac
"""


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class SpawnWorkerTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self.repo = td / "repo"
        # The script resolves the repo from its own real path, so it must live
        # at the same depth as in the checkout.
        dst = self.repo / SCRIPT_REL
        dst.parent.mkdir(parents=True)
        shutil.copy(REPO / SCRIPT_REL, dst)
        _write_exec(self.repo / "scripts" / "install-core-pool.sh", INSTALLER_STUB)
        _write_exec(self.repo / "scripts" / "sutando-config.sh", CONFIG_STUB)
        self.home = td / "home"
        self.agents = self.home / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)
        self.ws = td / "ws"
        (self.ws / "state" / "cores").mkdir(parents=True)
        self.log = td / "installer.log"
        self.env = dict(
            os.environ,
            HOME=str(self.home),
            STUB_WORKSPACE=str(self.ws),
            INSTALLER_LOG=str(self.log),
            SUTANDO_SPAWN_WAIT_S="0",
        )
        self.env.pop("SUTANDO_CORE_ID", None)
        self.env.pop("SUTANDO_ROOT", None)

    def tearDown(self):
        self._td.cleanup()

    def run_skill(self, *args, **extra):
        env = dict(self.env, **extra)
        return subprocess.run(
            ["bash", str(self.repo / SCRIPT_REL), *args],
            env=env, capture_output=True, text=True, timeout=60)

    def installer_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def preinstall(self, n):
        for i in range(1, n + 1):
            (self.agents / f"com.sutando.core-{i}.plist").touch()
        (self.agents / "com.sutando.pool-lead.plist").touch()

    def test_status_single_worker_mode(self):
        r = self.run_skill("--status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "mode=single-worker workers=0 lead=missing")
        self.assertEqual(self.installer_calls(), [])

    def test_status_counts_installed_and_live(self):
        self.preinstall(2)
        (self.ws / "state" / "cores" / "core-1.alive").touch()
        r = self.run_skill("--status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(),
                         "mode=multi-worker workers=2 live=1 lead=installed")

    def test_dry_run_plans_without_installing(self):
        r = self.run_skill("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("plan: installed=0 target=1 mode-after=multi-worker", r.stdout)
        self.assertIn("command: bash scripts/install-core-pool.sh 1", r.stdout)
        self.assertEqual(self.installer_calls(), [])
        self.assertFalse(list(self.agents.glob("com.sutando.core-*.plist")))

    def test_spawn_leaves_single_worker_mode(self):
        r = self.run_skill()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.installer_calls(), ["1"])
        self.assertIn("done: mode=multi-worker workers=1 live=0 lead=installed", r.stdout)
        self.assertEqual(self.run_skill("--status").stdout.strip(),
                         "mode=multi-worker workers=1 live=0 lead=installed")

    def test_count_grows_an_existing_pool(self):
        self.preinstall(1)
        r = self.run_skill("--count", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.installer_calls(), ["3"])
        self.assertIn("workers=3", r.stdout)

    def test_to_sets_an_exact_size(self):
        self.preinstall(1)
        r = self.run_skill("--to=4")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.installer_calls(), ["4"])

    def test_to_below_installed_is_refused(self):
        self.preinstall(3)
        r = self.run_skill("--to", "2")
        self.assertEqual(r.returncode, 2)
        self.assertIn("scale-down is manual", r.stderr)
        self.assertEqual(self.installer_calls(), [])

    def test_to_equal_installed_is_a_noop(self):
        self.preinstall(2)
        r = self.run_skill("--to", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nothing to do", r.stdout)
        self.assertEqual(self.installer_calls(), [])

    def test_refuses_from_inside_a_worker(self):
        r = self.run_skill(SUTANDO_CORE_ID="2")
        self.assertEqual(r.returncode, 2)
        self.assertIn("core-2", r.stderr)
        self.assertEqual(self.installer_calls(), [])

    def test_status_still_works_from_inside_a_worker(self):
        r = self.run_skill("--status", SUTANDO_CORE_ID="2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("mode=single-worker", r.stdout)

    def test_installer_failure_propagates_its_code(self):
        r = self.run_skill(INSTALLER_RC="3")
        self.assertEqual(r.returncode, 3)
        self.assertIn("installer exited 3", r.stderr)

    def test_bad_arguments_exit_2(self):
        for args in (["--count", "0"], ["--count", "x"], ["--to", "-1"], ["--bogus"]):
            r = self.run_skill(*args)
            self.assertEqual(r.returncode, 2, args)
        self.assertEqual(self.installer_calls(), [])

    def test_sutando_root_overrides_path_resolution(self):
        other = Path(self._td.name) / "other"
        _write_exec(other / "scripts" / "install-core-pool.sh", INSTALLER_STUB)
        _write_exec(other / "scripts" / "sutando-config.sh", CONFIG_STUB)
        r = self.run_skill("--dry-run", SUTANDO_ROOT=str(other))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("target=1", r.stdout)


if __name__ == "__main__":
    unittest.main()
