#!/usr/bin/env python3
"""A bridge started from another install's engine writes tasks the core never reads.

Field case: the Slack bridge kept running from an older app's engine, so it wrote
into that install's workspace while the core watched the desktop app's. The
probe must name the bridge and both inboxes, and stay quiet when a different
checkout still resolves the core's workspace.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))  # `python3 -I` does not add the script dir

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-bridge-inbox-")
_slack_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_slack_cfg.mkdir(parents=True, exist_ok=True)
(_slack_cfg / "access.json").write_text("{}")
_discord_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_discord_cfg.mkdir(parents=True, exist_ok=True)
(_discord_cfg / "access.json").write_text("{}")
_telegram_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_telegram_cfg.mkdir(parents=True, exist_ok=True)
(_telegram_cfg / "access.json").write_text("{}")
sys.path.insert(0, str(ROOT / "src"))

import bridge_inbox  # noqa: E402
import sutando_config, workspace_default  # noqa: E401,E402


def _install(base: Path, name: str, workspace: Path) -> Path:
    """An engine checkout whose `workspace` link points at `workspace`, as a desktop install lays it out."""
    checkout = base / name / "engine" / "sutando"
    (checkout / "src").mkdir(parents=True)
    (checkout / "sutando.config.json").write_text("{}")
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

    def _probe(self, procs: dict, cwds: dict | None = None, envs: dict | None = None):
        cwds = cwds or {}
        envs = envs or {}
        def pids(script):
            return [pid for pid, argv in procs.items() if script in str(argv)]
        return bridge_inbox.check_bridge_inbox_match(
            [self.core_ws / "tasks"], pids,
            argv_of=lambda pid: procs[pid], cwd_of=lambda pid: cwds.get(pid, str(self.tmp)),
            env_of=lambda pid: envs.get(pid, {}))

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

    def test_foreign_checkout_whose_env_names_the_core_workspace_is_ok(self):
        got = self._probe({"4": ["python3", f"{self.old}/src/slack-bridge.py"]},
                          envs={"4": {"SUTANDO_DEFAULT_WORKSPACE": str(self.core_ws)}})
        self.assertEqual(got["status"], "ok", got)
        self.assertIn("1 bridge(s) write the core inbox", got["detail"])

    def test_core_checkout_whose_env_names_another_workspace_warns(self):
        got = self._probe({"6": ["python3", f"{self.core}/src/slack-bridge.py"]},
                          envs={"6": {"SUTANDO_DEFAULT_WORKSPACE": str(self.old_ws)}})
        self.assertEqual(got["status"], "warn", got)
        self.assertIn(os.path.realpath(self.old_ws / "tasks"), got["detail"])

    def test_unreadable_environment_is_unknown_not_a_guess(self):
        procs = {"8": ["python3", f"{self.old}/src/slack-bridge.py"]}
        got = bridge_inbox.check_bridge_inbox_match(
            [self.core_ws / "tasks"], lambda s: [p for p, a in procs.items() if s in str(a)],
            argv_of=procs.get, cwd_of=lambda p: "", env_of=lambda p: None)
        self.assertEqual(got["status"], "ok", got)
        self.assertIn("inbox unknown for slack-bridge pid 8 (environment unreadable)", got["detail"])

    def test_unreadable_checkout_is_unknown(self):
        got = self._probe({"2": ["python3", "src/discord-bridge.py"]}, {"2": ""})
        self.assertEqual(got["status"], "ok", got)
        self.assertIn("inbox unknown for discord-bridge pid 2 (checkout unreadable)", got["detail"])

    def _relative_config(self):
        (self.old / "sutando.config.local.json").write_text('{"workspace": {"path": "ws"}}')
        return {"5": ["python3", f"{self.old}/src/slack-bridge.py"]}

    def test_relative_config_resolves_against_the_bridge_cwd(self):
        (self.core_ws.parent / "ws").symlink_to(self.core_ws)
        got = self._probe(self._relative_config(), {"5": str(self.core_ws.parent)})
        self.assertEqual(got["status"], "ok", got)

    def test_relative_config_from_another_cwd_warns(self):
        got = self._probe(self._relative_config(), {"5": str(self.old_ws.parent)})
        self.assertEqual(got["status"], "warn", got)
        self.assertIn(os.path.realpath(self.old_ws.parent / "ws" / "tasks"), got["detail"])

    def test_unreadable_cwd_is_unknown(self):
        got = self._probe(self._relative_config(), {"5": ""})
        self.assertEqual(got["status"], "ok", got)
        self.assertIn("inbox unknown for slack-bridge pid 5 (cwd unreadable)", got["detail"])

    def test_resolving_another_process_leaves_the_cache_alone(self):
        sutando_config.load_config(self.core)
        before = (sutando_config._CACHE, sutando_config._CACHE_REPO_ROOT)
        got = workspace_default.resolve_workspace(env={}, module_dir=self.old / "src", cwd=self.tmp)
        self.assertEqual(got, self.old_ws.resolve())
        self.assertEqual((sutando_config._CACHE, sutando_config._CACHE_REPO_ROOT), before)

    def test_checkout_config_beats_the_bridge_env(self):
        # resolve_workspace ranks explicit config above the embedder default.
        (self.old / "sutando.config.local.json").write_text(
            '{"workspace": {"path": "%s"}}' % self.core_ws)
        got = self._probe({"4": ["python3", f"{self.old}/src/slack-bridge.py"]},
                          envs={"4": {"SUTANDO_DEFAULT_WORKSPACE": str(self.old_ws)}})
        self.assertEqual(got["status"], "ok", got)

    def test_env_keys_the_resolver_ignores_are_ignored(self):
        env = {"SUTANDO_TASKS_DIR": str(self.core_ws / "tasks"),
               "SUTANDO_WORKSPACE_DIR": str(self.core_ws),
               "SUTANDO_WORKSPACE": str(self.core_ws)}
        got = self._probe({"4": ["python3", f"{self.old}/src/slack-bridge.py"]}, envs={"4": env})
        self.assertEqual(got["status"], "warn", got)

    def test_unrelated_process_is_not_a_bridge(self):
        got = self._probe({"3": "vim notes-slack-bridge.py"})
        self.assertEqual(got["status"], "ok", got)
        self.assertNotIn("write the core inbox", got["detail"])


class HealthCheckDelegatesToTheResolver(unittest.TestCase):
    def test_probe_result_follows_resolve_workspace(self):
        spec = importlib.util.spec_from_file_location("health_check", ROOT / "src" / "health-check.py")
        health = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(health)
        core = Path(tempfile.mkdtemp()) / "core-ws"
        elsewhere = Path(tempfile.mkdtemp()) / "other-ws"
        argv = ["python3", "/engine/src/slack-bridge.py"]
        stubs = [
            mock.patch.object(health, "_core_inboxes", lambda: [str(core / "tasks")]),
            mock.patch.object(health, "probe_pids",
                              lambda pat: (["42"], True) if "slack" in pat else ([], True)),
            mock.patch.object(health, "_drop_launcher_parents", lambda pids: pids),
            mock.patch.object(bridge_inbox, "read_argv", lambda pid: argv),
            mock.patch.object(bridge_inbox, "read_cwd", lambda pid: "/"),
            mock.patch.object(bridge_inbox, "read_env", lambda pid: {}),
        ]
        for st in stubs:
            st.start()
            self.addCleanup(st.stop)
        # health-check's bridge_inbox is this one only if the defaults were bound late.
        self.assertIs(health.bridge_inbox, bridge_inbox)
        results = {}
        for label, ws in (("core", core), ("elsewhere", elsewhere)):
            with mock.patch.object(workspace_default, "resolve_workspace",
                                   lambda migrate=True, *, env=None, module_dir=None, cwd=None, _w=ws: _w):
                results[label] = health.check_bridge_inbox_match()
        self.assertEqual(results["core"]["status"], "ok", results)
        self.assertEqual(results["elsewhere"]["status"], "warn", results)
        self.assertIn(os.path.realpath(elsewhere / "tasks"), results["elsewhere"]["detail"])


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
