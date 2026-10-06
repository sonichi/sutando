#!/usr/bin/env python3
"""The remedy timer re-ensures each live worker's watcher supervisor.

`pool_remedy.ensure_supervisors` runs `worker-watcher-supervisor.sh` once per
worker whose session answered alive this tick, with the worker's inbox, session
name, identity and the last run's tmux socket in the env, and reads the script's
exit code as supervised / not-running / failed. A worker whose session is gone,
or whose probe could not answer, is never ensured: that is the recover rung's
business. The runner is injected, so no tmux is touched.

Run: python3 tests/skills/worker-pool/pool-remedy-ensures-supervisor.test.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


rem = _load("pool_remedy")
WID = "0123456789abcdef0123456789abcdef"


class Runner:
    """Records every invocation; answers with a fixed return code."""

    def __init__(self, rc=0, stderr=""):
        self.rc, self.stderr, self.calls = rc, stderr, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, self.rc, "", self.stderr)


class EnsureSupervisor(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "ws"
        (self.ws / "state" / "workers" / WID).mkdir(parents=True)
        # The last incarnation records the socket the worker ran on.
        (self.ws / "state" / "workers" / WID / "incarnations.json").write_text(
            json.dumps({"incarnations": [{"tmux": {"socket": "/tmp/sock-test"}}]}))
        (self.ws / "state" / "roster.json").write_text(
            json.dumps({"workers": {WID: {"runtime": "claude", "state": "live"}}}))

    def tearDown(self):
        self.td.cleanup()

    def test_a_live_worker_is_ensured_with_its_own_inbox_session_and_socket(self):
        run = Runner(rc=0)
        out = rem.ensure_supervisor(self.ws, REPO, WID, runner=run)
        self.assertEqual(out["outcome"], rem.SUPERVISED)
        self.assertEqual(len(run.calls), 1)
        argv, kw = run.calls[0]
        self.assertEqual(argv, ["bash", str(REPO / rem.SUPERVISOR_SCRIPT)])
        env = kw["env"]
        self.assertEqual(env["SUTANDO_INSTANCE_ID"], WID)
        self.assertEqual(env["SUTANDO_TASKS_DIR"], str(rem.pd.deliveries_dir(self.ws, WID)))
        self.assertEqual(env["SUTANDO_TMUX_SESSION"], rem.wi.tmux_session_name(WID))
        self.assertEqual(env["SUTANDO_INBOX_KIND"], "deliveries")
        self.assertEqual(env["SUTANDO_WORKSPACE_DIR"], str(self.ws))
        self.assertEqual(env["SUTANDO_INBOX_RESOLVER"], str(REPO / "skills" / "worker-pool" / "scripts" / "resolve-inbox-entry"))

    def test_the_last_runs_socket_is_forwarded_when_recorded(self):
        run = Runner(rc=0)
        rem.ensure_supervisor(self.ws, REPO, WID, runner=run)
        self.assertEqual(run.calls[0][1]["env"].get("SUTANDO_TMUX_SOCKET"), "/tmp/sock-test")

    def test_exit_3_reads_as_not_running_and_other_failures_carry_why(self):
        self.assertEqual(rem.ensure_supervisor(self.ws, REPO, WID, runner=Runner(rc=3))["outcome"],
                         rem.NOT_RUNNING)
        self.assertEqual(rem.ensure_supervisor(self.ws, REPO, WID, runner=Runner(rc=4))["outcome"],
                         rem.HELD)
        out = rem.ensure_supervisor(self.ws, REPO, WID, runner=Runner(rc=1, stderr="tmux could not start"))
        self.assertEqual(out["outcome"], rem.SUPERVISOR_FAILED)
        self.assertIn("tmux could not start", out["why"])

    def test_a_runner_that_cannot_run_is_a_failure_not_an_exception(self):
        def boom(argv, **kw):
            raise OSError("no bash")
        out = rem.ensure_supervisor(self.ws, REPO, WID, runner=boom)
        self.assertEqual(out["outcome"], rem.SUPERVISOR_FAILED)


class EnsureSupervisors(unittest.TestCase):
    def test_only_workers_whose_session_answered_alive_are_ensured(self):
        run = Runner(rc=0)
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            (ws / "state" / "workers").mkdir(parents=True)
            (ws / "state" / "roster.json").write_text(json.dumps({"workers": {
                worker_id: {"runtime": "claude", "state": "live"}
                for worker_id in ("a" * 32, "b" * 32, "c" * 32)
            }}))
            obs = {"a" * 32: {"beat": "live", "session_alive": True, "paused": False},
                   "b" * 32: {"beat": "stale", "session_alive": False, "paused": False},
                   "c" * 32: {"beat": "absent", "session_alive": None, "paused": False}}
            out = rem.ensure_supervisors(ws, REPO, obs, runner=run)
        self.assertEqual(sorted(out), ["a" * 32])
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(run.calls[0][1]["env"]["SUTANDO_INSTANCE_ID"], "a" * 32)


if __name__ == "__main__":
    unittest.main(verbosity=2)
