#!/usr/bin/env python3
"""The session side of a converted pool worker's own folders (`dirs: "own"`).

- `spawn_worker.inbox_env`, shared by the spawn plan and the remedy's supervisor: an own-dir worker gets
  its own tasks/ and results/, a plain inbox (kind and resolver set empty) and the workspace unchanged;
  any other worker keeps its deliveries folder, the resolver and the shared results/, as before.
- `result_publish.worker_published_path`: a target in the shared results/ moves into the session's
  worker's own results/ (resolved paths compared); the core, a shared-dir worker and other dirs are unchanged.
- `watcher_rearm.target`: an own-dir worker watches its launcher-named inbox; a deliveries worker as before.

Run: python3 tests/worker-own-dirs-session.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "worker-pool" / "scripts"
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "packages" / "ag2-sparrow"), str(SCRIPTS)]
import connections as C, result_publish, watcher_rearm  # noqa: E401,E402
from ag2_sparrow import agent_identity  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sw, rem = _load("spawn_worker"), _load("pool_remedy")
W = "7c700e11" + "0" * 24
WORKER_AGENT = "@reviewer:alpha-fixture.net"
WORKER_INST = C.worker_instance_for("alpha-fixture.net", W)


def row(instance, *, primary=False, **extra):
    return {"server": "alpha-fixture.net", "instance": instance, "gateway_url": "https://chat.alpha-fixture.net",
            "token_ref": f"AG2_REMOTE_TOKEN_{instance.upper().replace('-', '_')}", "primary": primary,
            "added_by": "owner", "added_at": "2026-11-16T00:00:00Z", "verified": True,
            "server_source": "gateway", **extra}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name) / "ws"
        self.own = self.ws / "agents" / agent_identity.root_segment(WORKER_AGENT)
        self.write(dirs="own")

    def write(self, **worker_extra):
        path = C.record_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "connections": [
            row("alpha-fixture-net", primary=True),
            row(WORKER_INST, worker=W, agent_mxid=WORKER_AGENT, **worker_extra)]}))


class InboxEnv(Base):
    def test_own_dirs_are_a_plain_inbox(self):
        self.assertEqual(sw.inbox_env(self.ws, W), {
            "SUTANDO_TASKS_DIR": str(self.own / "tasks"), "SUTANDO_RESULTS_DIR": str(self.own / "results"),
            "SUTANDO_INBOX_KIND": "", "SUTANDO_INBOX_RESOLVER": "", "SUTANDO_WORKSPACE_DIR": str(self.ws)})

    def test_shared_dirs_are_unchanged(self):
        self.write()
        self.assertEqual(sw.inbox_env(self.ws, W), {
            "SUTANDO_TASKS_DIR": str(self.ws / "deliveries" / W), "SUTANDO_INBOX_KIND": "deliveries",
            "SUTANDO_WORKSPACE_DIR": str(self.ws), "SUTANDO_RESULTS_DIR": str(self.ws / "results"),
            "SUTANDO_INBOX_RESOLVER": str(SCRIPTS / "resolve-inbox-entry")})

    def test_the_spawn_plan_carries_it(self):
        env = sw.plan(self.ws, ROOT, worker_id=W, socket="/tmp/sock-test")["env"]
        self.assertEqual((env["SUTANDO_TASKS_DIR"], env["SUTANDO_INBOX_KIND"], env["SUTANDO_INBOX_RESOLVER"]),
                         (str(self.own / "tasks"), "", ""))
        self.write()
        env = sw.plan(self.ws, ROOT, worker_id=W, socket="/tmp/sock-test")["env"]
        self.assertEqual(env["SUTANDO_INBOX_KIND"], "deliveries")

    def test_the_remedy_supervisor_carries_it(self):
        state = self.ws / "state"
        (state / "workers" / W).mkdir(parents=True)
        (state / "workers" / W / "incarnations.json").write_text(
            json.dumps({"incarnations": [{"tmux": {"socket": "/tmp/sock-test"}}]}))
        (state / "roster.json").write_text(json.dumps({"workers": {W: {"runtime": "claude", "state": "live"}}}))
        calls = []
        out = rem.ensure_supervisor(self.ws, ROOT, W, runner=lambda argv, **kw: calls.append(kw["env"])
                                    or subprocess.CompletedProcess(argv, 0, "", ""))
        self.assertEqual(out["outcome"], rem.SUPERVISED)
        self.assertEqual((calls[0]["SUTANDO_TASKS_DIR"], calls[0]["SUTANDO_RESULTS_DIR"],
                          calls[0]["SUTANDO_INBOX_KIND"], calls[0]["SUTANDO_INBOX_RESOLVER"]),
                         (str(self.own / "tasks"), str(self.own / "results"), "", ""))


class PublishedPath(Base):
    def published(self, path, worker=W):
        return result_publish.worker_published_path(path, {C.SESSION_WORKER_ENV: worker} if worker else {}, self.ws)

    def test_a_shared_results_target_moves_into_its_own(self):
        self.assertEqual(self.published(self.ws / "results" / "task-1.txt"), self.own / "results" / "task-1.txt")
        stamped = self.published(self.ws / "results" / "proactive-1.txt")
        self.assertEqual(stamped.parent, self.own / "results")
        self.assertIn(f".by-{W}", stamped.name)

    def test_through_a_symlinked_workspace_too(self):
        link = self.ws.parent / "ws link"
        link.symlink_to(self.ws)
        self.assertEqual(self.published(link / "results" / "task-1.txt").parent, self.own / "results")

    def test_unchanged_for_the_core_a_shared_worker_and_other_dirs(self):
        self.assertEqual(self.published(self.ws / "results" / "task-1.txt", worker=""), self.ws / "results" / "task-1.txt")
        self.assertEqual(self.published(self.ws / "notes" / "task-1.txt"), self.ws / "notes" / "task-1.txt")
        self.write()
        self.assertEqual(self.published(self.ws / "results" / "task-1.txt"), self.ws / "results" / "task-1.txt")

    def test_main_publishes_into_its_own_folder_and_says_so(self):
        err = io.StringIO()
        env = {C.SESSION_WORKER_ENV: W}
        with mock.patch.dict(os.environ, env), mock.patch.object(C, "_workspace", lambda ws: self.ws), \
                mock.patch.object(sys, "stdin", io.StringIO("[no-send]\n")), contextlib.redirect_stderr(err):
            self.assertEqual(result_publish.main(["result_publish.py", str(self.ws / "results" / "task-1.txt")]), 0)
        self.assertEqual((self.own / "results" / "task-1.txt").read_text(), "[no-send]\n")
        self.assertFalse((self.ws / "results" / "task-1.txt").exists())
        self.assertIn("its own results folder", err.getvalue())


class RearmTarget(unittest.TestCase):
    def test_an_own_dir_worker_watches_its_named_inbox(self):
        env = {"SUTANDO_INBOX_KIND": "", "SUTANDO_TASKS_DIR": "/w/agents/a/tasks"}
        self.assertEqual(watcher_rearm.target("/r", "/w", W, env), ("/w/agents/a/tasks", watcher_rearm.WORKER_REARM))
        self.assertEqual(watcher_rearm.watch_argv("/r", "/w", W, dict(env, SUTANDO_WATCHER_CMD="/c"))[2],
                         "/w/agents/a/tasks")

    def test_a_deliveries_worker_and_the_core_are_unchanged(self):
        env = {"SUTANDO_INBOX_KIND": "deliveries", "SUTANDO_TASKS_DIR": "/w/deliveries/x"}
        self.assertEqual(watcher_rearm.target("/r", "/w", W, env)[0], f"/w/deliveries/{W}")
        self.assertEqual(watcher_rearm.target("/r", "/w", W, {})[0], f"/w/deliveries/{W}")
        self.assertEqual(watcher_rearm.target("/r", "/w", None, {"SUTANDO_INBOX_KIND": ""})[0], "/w/tasks")


if __name__ == "__main__":
    unittest.main()
