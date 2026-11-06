#!/usr/bin/env python3
"""Two guards that used to answer a question they never asked.

RUNTIME: the worker's own launcher selects Claude or Codex from the runtime
the spawner recorded in its environment. An unknown adapter is refused BEFORE
any identity state exists.

SENTINEL: the guard asked whether `util_paths.py` CONTAINS `def
watcher_sentinel_path`. A checkout that resolves one shared sentinel passes
that, and its second watcher erases the core's liveness stamp. It now asks the
resolver what it RESOLVES, under two identities.

Run: python3 tests/skills/worker-pool/spawn-worker-runtime-and-sentinel.test.py
"""
from __future__ import annotations

import importlib.util
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = Path(__file__).resolve().parents[3] / "skills/worker-pool/scripts"

# From its path, not off sys.path: the module bootstraps src/ itself for
# exactly this caller, and pre-inserting it would leave that unrun.
_spec = importlib.util.spec_from_file_location("spawn_worker", SCRIPTS / "spawn_worker.py")
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)


class Runner:
    """Fakeable process ops: answers the config probe, runs everything else for
    real, and records what the launcher was asked to run."""
    def __init__(self, runtime="claude", existing=()):
        self.launches, self.existing, self.runtime = [], set(existing), runtime
        self.loaded = set()

    def __call__(self, argv, **kw):
        if argv[0] == "launchctl":
            if argv[1] == "print":
                return subprocess.CompletedProcess(argv, 0 if argv[2] in self.loaded else 113, "", "")
            if argv[1] == "bootout":
                self.loaded.discard(argv[2])
            if argv[1] == "bootstrap":
                with open(argv[3], "rb") as fh:
                    label = plistlib.load(fh)["Label"]
                self.loaded.add(f"gui/{os.getuid()}/{label}")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "bash" and argv[1].endswith("sutando-config.sh"):
            return subprocess.CompletedProcess(argv, 0, self.runtime + "\n", "")
        if argv[0] == "bash" and argv[1].endswith("launch-worker-session.sh"):
            self.launches.append(argv)
            self.existing.add((kw.get("env") or {}).get("SUTANDO_TMUX_SESSION", ""))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if len(argv) > 3 and argv[3] == "has-session":
            name = argv[-1].lstrip("=")
            return (subprocess.CompletedProcess(argv, 0, "", "") if name in self.existing
                    else subprocess.CompletedProcess(argv, 1, "", f"can't find session: {name}"))
        if argv[0] == "tmux":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return sw._run(argv, **kw)          # the sentinel probe, for real


def fake_checkout(tmp: Path, body: str) -> Path:
    """A repo whose util_paths.py behaves as `body` says."""
    repo = tmp / "checkout"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "util_paths.py").write_text(body)
    return repo


SHARED = '''import sys
def watcher_sentinel_path(state_dir, instance=None, agent=None):
    """Deliberately ONE sentinel for every watcher."""
    return state_dir + "/watch-tasks-stream.pid"
print(watcher_sentinel_path(sys.argv[2]))
'''
COMMENT_ONLY = '''import sys
# def watcher_sentinel_path(state_dir, instance=None): the real one is gone
print(sys.argv[2] + "/watch-tasks-stream.pid")
'''


class TestSentinelIsBehavioural(unittest.TestCase):
    def test_this_checkout_resolves_a_sentinel_per_instance(self):
        self.assertTrue(sw.per_instance_sentinel_supported(REPO))

    def test_a_shared_sentinel_is_refused_though_the_spelling_is_there(self):
        """The control the source-grep could not fail: the token is present and
        the behaviour is absent."""
        with tempfile.TemporaryDirectory() as t:
            repo = fake_checkout(Path(t), SHARED)
            self.assertIn("def watcher_sentinel_path",
                          (repo / "src" / "util_paths.py").read_text())
            self.assertFalse(sw.per_instance_sentinel_supported(repo))

    def test_a_comment_only_definition_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            repo = fake_checkout(Path(t), COMMENT_ONLY)
            self.assertFalse(sw.per_instance_sentinel_supported(repo))

    def test_a_resolver_that_cannot_run_is_refused_not_assumed(self):
        with tempfile.TemporaryDirectory() as t:
            repo = fake_checkout(Path(t), "raise SystemExit(3)\n")
            self.assertFalse(sw.per_instance_sentinel_supported(repo))


class TestRuntimeSelector(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        self.la = self.ws / "LaunchAgents"
        real_ensure = sw.ensure_remedy_timer
        patcher = mock.patch.object(
            sw, "ensure_remedy_timer",
            side_effect=lambda ws, repo, **kw: real_ensure(ws, repo, launch_agents=self.la, **kw))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_plan_hands_the_launcher_no_runtime_selector(self):
        """The runtime is an environment value, not a dispatcher argv flag."""
        p = sw.plan(self.ws, REPO, runtime="claude")
        argv = p["launcher_argv"]
        self.assertNotIn("--runtime", argv)
        self.assertTrue(argv[1].endswith("launch-worker-session.sh"), argv)
        self.assertEqual(p["env"]["SUTANDO_WORKER_RUNTIME"], "claude")

    def test_the_spawn_launches_the_workers_own_script(self):
        r = Runner()
        got = sw.spawn(self.ws, REPO, runner=r, require_sentinel=False)
        if sys.platform == "darwin":
            self.assertEqual(Path(got["remedy_timer"]["plist"]).parent, self.la)
        self.assertEqual(len(r.launches), 1)
        argv = r.launches[0]
        self.assertNotIn("--runtime", argv)
        self.assertEqual(got["runtime"], "claude")

    def test_a_runtime_without_worker_mode_is_refused(self):
        with self.assertRaises(sw.SpawnRefused) as e:
            sw.spawn(self.ws, REPO, runtime="nonesuch", runner=Runner(),
                     require_sentinel=False)
        self.assertIn("worker mode", str(e.exception))

    def test_the_refusal_lands_before_any_identity_state(self):
        """A record naming a worker that no launcher can bring up is the false
        success this gate exists to prevent."""
        r = Runner()
        with self.assertRaises(sw.SpawnRefused):
            sw.spawn(self.ws, REPO, runtime="nonesuch", runner=r, require_sentinel=False)
        self.assertFalse((self.ws / "state" / "workers").exists())
        self.assertFalse((self.ws / "deliveries").exists())
        self.assertEqual(r.launches, [])

    def test_a_codex_configured_core_launches_codex_without_claiming_a_session_id(self):
        r = Runner(runtime="codex")
        made = sw.spawn(self.ws, REPO, runner=r, require_sentinel=False)
        self.assertEqual(made["runtime"], "codex")
        self.assertIsNone(made["runtime_session_id"])
        self.assertEqual(made["env"]["SUTANDO_WORKER_RUNTIME"], "codex")
        self.assertEqual(sw.wi.sessions(self.ws, made["worker_id"]), [])
        self.assertIsNone(sw.wi.current(self.ws, made["worker_id"])["session_id"])
        self.assertEqual(len(r.launches), 1)


class TestDispatcherHonoursTheSelector(unittest.TestCase):
    def test_the_dispatcher_refuses_a_runtime_it_cannot_launch(self):
        r = subprocess.run(["bash", str(REPO / "src" / "agent" / "start-cli.sh"),
                            "--runtime", "nonesuch"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("unsupported core runtime: nonesuch", r.stderr)

    def test_a_selector_with_no_value_is_an_error_not_a_silent_default(self):
        r = subprocess.run(["bash", str(REPO / "src" / "agent" / "start-cli.sh"),
                            "--runtime"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=0)
