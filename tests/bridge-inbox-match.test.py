#!/usr/bin/env python3
"""A bridge started from another install's engine writes tasks the core never reads.

Field case: the Slack bridge kept running from an older app's engine, so it wrote
into that install's workspace while the core watched the desktop app's. The
probe must name the bridge and both inboxes, and stay quiet when a different
checkout still resolves the core's workspace.
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import bridge_inbox  # noqa: E402
import sutando_config  # noqa: E402


def _install(base: Path, name: str, workspace: Path) -> Path:
    """An engine checkout whose `workspace` link points at `workspace`, as a desktop install lays it out."""
    checkout = base / name / "engine" / "sutando"
    (checkout / "src").mkdir(parents=True)
    workspace.mkdir(parents=True, exist_ok=True)
    (checkout / "workspace").symlink_to(workspace)
    return checkout


class BridgeInboxMatch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.core_ws = self.tmp / "Application Support" / "desktop" / "workspace"
        self.old_ws = self.tmp / "Application Support" / "old-app" / "workspace"
        self.core = _install(self.tmp / "Application Support", "desktop", self.core_ws)
        self.old = _install(self.tmp / "Application Support", "old-app", self.old_ws)

    def _probe(self, procs: dict, cwds: dict | None = None):
        cwds = cwds or {}
        def pids(script):
            return [pid for pid, argv in procs.items() if script in str(argv)]
        return bridge_inbox.check_bridge_inbox_match(
            [self.core_ws / "tasks"], self.core, pids,
            argv_of=lambda pid: procs[pid], cwd_of=lambda pid: cwds.get(pid, ""))

    def test_bridge_from_other_engine_is_flagged_with_both_paths(self):
        argv = f"python3 {self.old}/src/slack-bridge.py"  # flattened, path has a space
        got = self._probe({"111": argv})
        self.assertEqual(got["status"], "warn", got)
        self.assertIn("slack-bridge pid 111", got["detail"])
        self.assertIn(os.path.realpath(self.old_ws / "tasks"), got["detail"])
        self.assertIn(os.path.realpath(self.core_ws / "tasks"), got["detail"])

    def test_relative_launch_resolves_through_cwd(self):
        got = self._probe({"7": ["python3", "src/telegram-bridge.py"]}, {"7": str(self.old)})
        self.assertEqual(got["status"], "warn", got)
        self.assertIn("telegram-bridge pid 7", got["detail"])

    def test_bridge_from_core_engine_is_ok(self):
        got = self._probe({"5": ["python3", f"{self.core}/src/discord-bridge.py"]})
        self.assertEqual(got["status"], "ok", got)
        self.assertIn("1 bridge(s) write the core inbox", got["detail"])

    def test_other_checkout_sharing_the_core_workspace_is_ok(self):
        shared = _install(self.tmp, "dev-clone", self.core_ws)
        got = self._probe({"9": ["python3", f"{shared}/src/slack-bridge.py"]})
        self.assertEqual(got["status"], "ok", got)

    def test_probe_env_does_not_stand_in_for_the_bridge(self):
        # The core's embedder default would make every foreign checkout look like the core.
        os.environ["SUTANDO_DEFAULT_WORKSPACE"] = str(self.core_ws)
        try:
            got = self._probe({"111": ["python3", f"{self.old}/src/slack-bridge.py"]})
        finally:
            del os.environ["SUTANDO_DEFAULT_WORKSPACE"]
        self.assertEqual(got["status"], "warn", got)

    def test_checkout_workspace_leaves_the_process_cache_alone(self):
        sutando_config.load_config(self.core)
        before = (sutando_config._CACHE, sutando_config._CACHE_REPO_ROOT)
        self.assertEqual(sutando_config.checkout_workspace(self.old), self.old_ws.resolve())
        self.assertEqual((sutando_config._CACHE, sutando_config._CACHE_REPO_ROOT), before)

    def test_checkout_config_path_wins(self):
        (self.old / "sutando.config.local.json").write_text(
            '{"workspace": {"path": "%s"}}' % self.core_ws)
        self.assertEqual(sutando_config.checkout_workspace(self.old), self.core_ws.resolve())

    def test_unrelated_process_is_not_a_bridge(self):
        got = self._probe({"3": "vim notes-slack-bridge.py"})
        self.assertEqual(got["status"], "ok", got)
        self.assertIn("no bridge running", got["detail"])


class BridgesShareOneResolver(unittest.TestCase):
    def test_each_bridge_takes_its_inbox_from_resolve_workspace(self):
        for script in bridge_inbox.BRIDGE_SCRIPTS.values():
            src = (ROOT / "src" / script).read_text()
            self.assertIn("from workspace_default import resolve_workspace", src, script)
            self.assertRegex(src, r"\nREPO = resolve_workspace\(\)\n", script)
            for line in re.findall(r"^TASKS_DIR = .*$", src, re.M):
                self.assertEqual(line, 'TASKS_DIR = REPO / "tasks"', script)

    def test_health_check_runs_the_probe(self):
        src = (ROOT / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_bridge_inbox_match())", src)


if __name__ == "__main__":
    unittest.main()
