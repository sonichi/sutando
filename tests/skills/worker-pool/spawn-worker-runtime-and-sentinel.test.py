#!/usr/bin/env python3
"""Two guards that used to answer a question they never asked.

RUNTIME: the record named one runtime and the launcher chose another, because
the dispatcher rereads the CORE's configuration. The selector is now in the
argv, and a runtime whose adapter has no worker mode is refused BEFORE any
identity state exists — a record for a worker that cannot run is worse than no
worker.

SENTINEL: the guard asked whether `util_paths.py` CONTAINS `def
watcher_sentinel_path`. A checkout that resolves one shared sentinel passes
that, and its second watcher erases the core's liveness stamp. It now asks the
resolver what it RESOLVES, under two identities.

Run: python3 tests/skills/worker-pool/spawn-worker-runtime-and-sentinel.test.py
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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

    def __call__(self, argv, **kw):
        if argv[0] == "bash" and argv[1].endswith("sutando-config.sh"):
            return subprocess.CompletedProcess(argv, 0, self.runtime + "\n", "")
        if argv[0] == "bash" and argv[1].endswith("start-cli.sh"):
            self.launches.append(argv)
            self.existing.add((kw.get("env") or {}).get("SUTANDO_TMUX_SESSION", ""))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if len(argv) > 3 and argv[3] == "has-session":
            hit = argv[-1].lstrip("=") in self.existing
            return subprocess.CompletedProcess(argv, 0 if hit else 1, "", "")
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

    def test_the_plan_hands_the_launcher_the_runtime_it_recorded(self):
        p = sw.plan(self.ws, REPO, runtime="claude")
        argv = p["launcher_argv"]
        self.assertEqual(argv[argv.index("--runtime") + 1], "claude")

    def test_the_spawn_launches_with_the_selector_present(self):
        r = Runner()
        got = sw.spawn(self.ws, REPO, runner=r, require_sentinel=False)
        self.assertEqual(len(r.launches), 1)
        argv = r.launches[0]
        self.assertIn("--runtime", argv)
        self.assertEqual(argv[argv.index("--runtime") + 1], got["runtime"])

    def test_a_runtime_without_worker_mode_is_refused(self):
        with self.assertRaises(sw.SpawnRefused) as e:
            sw.spawn(self.ws, REPO, runtime="codex", runner=Runner(),
                     require_sentinel=False)
        self.assertIn("worker mode", str(e.exception))

    def test_the_refusal_lands_before_any_identity_state(self):
        """A record naming a worker that no launcher can bring up is the false
        success this gate exists to prevent."""
        r = Runner()
        with self.assertRaises(sw.SpawnRefused):
            sw.spawn(self.ws, REPO, runtime="codex", runner=r, require_sentinel=False)
        self.assertFalse((self.ws / "state" / "workers").exists())
        self.assertFalse((self.ws / "deliveries").exists())
        self.assertEqual(r.launches, [])

    def test_a_codex_configured_core_refuses_rather_than_running_claude(self):
        """The probe keweichen ran: requested=codex, dispatcher_selected=claude.
        With no explicit runtime the core's config decides, and it must not
        silently resolve to the one adapter that does have worker mode."""
        with self.assertRaises(sw.SpawnRefused):
            sw.spawn(self.ws, REPO, runner=Runner(runtime="codex"),
                     require_sentinel=False)


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
