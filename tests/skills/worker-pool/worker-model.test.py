#!/usr/bin/env python3
"""Per-worker Claude models: each worker launches with its own --model.

A worker's model lives on its roster row (`pool_manage.py set-model`), else the pool default
WORKER_POOL_DEFAULT_MODEL (opus). The launcher passes it as --model, so no worker reads the
shared settings.json model, and set-model applies a change by reseating that worker only:
it never sends /model, which rewrites the shared settings.json even under --model.

Run: python3 tests/skills/worker-pool/worker-model.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pm = _load("pool_manage")
rem, pr, wi, sw = pm.rem, pm.pr, pm.wi, pm.sw

WID, OTHER, CODEX = "a" * 32, "b" * 32, "c" * 32
SEAT, OTHER_SEAT = wi.tmux_session_name(WID), wi.tmux_session_name(OTHER)


class FakeTmux:
    """has-session / kill-session against a set of live session names; records every call."""

    def __init__(self, alive=()):
        self.alive, self.calls = set(alive), []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:1] == ["tmux"] and "has-session" in argv:
            name = argv[argv.index("-t") + 1].lstrip("=")
            rc = 0 if name in self.alive else 1
            return subprocess.CompletedProcess(argv, rc, "", "" if rc == 0 else f"can't find session: {name}\n")
        if argv[:1] == ["tmux"] and "kill-session" in argv:
            self.alive.discard(argv[argv.index("-t") + 1].lstrip("="))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def killed(self):
        return [c[c.index("-t") + 1].lstrip("=") for c in self.calls if "kill-session" in c]


class Base(unittest.TestCase):
    def setUp(self):
        # The core exports manifest config into its session env; a test run inside one would inherit it.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(pr.DEFAULT_MODEL_KEY, None)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.tmp = Path(os.path.realpath(td.name))
        self.ws = self.tmp / "ws"
        (self.ws / "state").mkdir(parents=True)
        for wid, label, rt in ((WID, "w1", "claude"), (OTHER, "w2", "claude"), (CODEX, "cx", "codex")):
            wi.create_worker(self.ws, runtime=rt, tmux_socket=str(self.tmp / "s"), worker_id=wid, cwd=str(REPO))
            pr.register_worker(self.ws, wid, label, runtime=rt)
        self.tmux = FakeTmux({SEAT, SEAT + "-watcher", SEAT + "-input", OTHER_SEAT})
        self.started = []
        # The shared default every unpinned session reads; nothing here may change it.
        self.cfg = self.tmp / "claude-config"
        self.cfg.mkdir()
        (self.cfg / "settings.json").write_text('{"model": "fable", "theme": "dark"}\n')
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)

    def spawn(self, workspace, repo, **kw):
        # What the real spawn hands the launcher for a resumed worker: plan()'s env.
        wid = wi.worker_for_session(workspace, kw["resume"])
        self.started.append(sw.plan(workspace, repo, worker_id=wid))
        self.tmux.alive.add(wi.tmux_session_name(wid))
        return {"runtime_session_id": kw.get("resume")}

    def main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out), \
                mock.patch.object(rem, "ensure_supervisor", return_value={"outcome": rem.SUPERVISED}), \
                mock.patch.object(rem, "ensure_input_watch", return_value={"outcome": rem.WATCHING}), \
                mock.patch.object(sw, "spawn", self.spawn):
            rc = pm.main(["--workspace", str(self.ws), "--repo", str(REPO), *args], runner=self.tmux)
        return rc, json.loads(out.getvalue().strip().splitlines()[-1])

    def row(self, wid=WID):
        return pr.load_roster(self.ws)["workers"][wid]

    def plan_model(self, wid=WID, runtime="claude"):
        p = sw.plan(self.ws, REPO, worker_id=wid, runtime=runtime)
        return p["model"], p["env"]["SUTANDO_WORKER_MODEL"]



class Default(Base):
    def test_the_manifest_declares_opus_as_the_pool_default(self):
        cfg = json.loads((REPO / "skills" / "worker-pool" / "manifest.json").read_text())["config"]
        self.assertEqual(cfg[pr.DEFAULT_MODEL_KEY], "opus")

    def test_a_worker_with_no_recorded_model_launches_on_the_default(self):
        self.assertNotIn("model", self.row())
        self.assertEqual(self.plan_model(), ("opus", "opus"))
        self.assertEqual(self.plan_model(wid=None), ("opus", "opus"))

    def test_the_env_overrides_the_manifest_and_a_bad_value_passes_no_model(self):
        os.environ[pr.DEFAULT_MODEL_KEY] = "sonnet"
        self.assertEqual(self.plan_model(), ("sonnet", "sonnet"))
        os.environ[pr.DEFAULT_MODEL_KEY] = "opus; rm -rf /"
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(self.plan_model(), (None, ""))
        self.assertIn("not a Claude model name", err.getvalue())

    def test_a_codex_worker_gets_no_model(self):
        self.assertEqual(self.plan_model(CODEX, runtime="codex"), (None, ""))


class SetModel(Base):
    def test_changes_only_that_worker_and_reseats_only_its_seat(self):
        rc, out = self.main("set-model", "w1", "sonnet")
        self.assertEqual((rc, out["result"], out["model"], out["previous"]), (0, "applied", "sonnet", None))
        self.assertEqual(self.row()["model"], "sonnet")
        self.assertNotIn("model", self.row(OTHER))
        self.assertEqual(self.plan_model(), ("sonnet", "sonnet"))
        self.assertEqual(self.plan_model(OTHER), ("opus", "opus"))
        self.assertEqual(sorted(self.tmux.killed()), sorted([SEAT, SEAT + "-watcher", SEAT + "-input"]))
        self.assertIn(OTHER_SEAT, self.tmux.alive)
        # The restarted seat resumed with the new model on its own launch env.
        self.assertEqual([(p["worker_id"], p["env"]["SUTANDO_WORKER_MODEL"]) for p in self.started],
                         [(WID, "sonnet")])

    def test_the_shared_default_is_never_touched_and_no_slash_model_is_sent(self):
        before = (self.cfg / "settings.json").read_bytes()
        self.main("set-model", WID, "haiku")
        self.main("set-model", WID, "--clear")
        self.assertEqual((self.cfg / "settings.json").read_bytes(), before)
        self.assertFalse([c for c in self.tmux.calls if "send-keys" in c or "/model" in " ".join(c)],
                         "set-model must reseat, never type /model into a pane")

    def test_clear_returns_to_the_default_and_a_repeat_is_unchanged(self):
        self.main("set-model", WID, "sonnet")
        rc, out = self.main("set-model", WID, "--clear")
        self.assertEqual((rc, out["result"], out["model"], out["recorded"]), (0, "applied", "opus", None))
        self.assertNotIn("model", self.row())
        n = len(self.started)
        rc, out = self.main("set-model", WID, "--clear")
        self.assertEqual((rc, out["result"]), (0, "unchanged"))
        self.assertEqual(len(self.started), n, "an unchanged record must not restart the seat")

    def test_a_seat_that_owes_work_keeps_running_and_takes_the_model_at_its_next_start(self):
        with mock.patch.object(pm, "_outstanding", return_value=["task-x"]):
            rc, out = self.main("set-model", WID, "sonnet")
        self.assertEqual((rc, out["result"], out["apply"]["result"]), (0, "recorded", "deferred"))
        self.assertEqual(self.row()["model"], "sonnet")
        self.assertEqual((self.tmux.killed(), self.started), ([], []))

    def test_no_running_seat_is_recorded_for_its_next_start(self):
        self.tmux.alive.clear()
        rc, out = self.main("set-model", WID, "sonnet")
        self.assertEqual((rc, out["result"], out["apply"]["result"]), (0, "recorded", "skipped"))
        self.assertEqual(self.plan_model(), ("sonnet", "sonnet"))

    def test_this_sessions_own_seat_is_recorded_not_restarted(self):
        os.environ["SUTANDO_INSTANCE_ID"] = WID
        rc, out = self.main("set-model", WID, "sonnet")
        self.assertEqual((rc, out["result"], out["apply"]["result"]), (0, "recorded", "refused"))
        self.assertEqual(self.tmux.killed(), [])

    def test_refusals_leave_every_row_as_it_was(self):
        for args in (("w1", "gpt-5"), ("w1", "sonnet[2m]"), ("w1", "opus --chrome"), (CODEX, "opus"),
                     ("core", "opus"), ("nobody", "opus"), ("w1",), ("w1", "opus", "--clear")):
            rc, out = self.main("set-model", *args)
            self.assertEqual((rc, out["result"]), (2, "refused"), args)
        self.assertNotIn("model", self.row())
        self.assertNotIn("model", self.row(CODEX))
        self.assertEqual(self.started, [])

    def test_reregistration_keeps_the_recorded_model(self):
        self.main("set-model", WID, "sonnet[1m]")
        pr.register_worker(self.ws, WID, "w1", runtime="claude")
        self.assertEqual(self.row()["model"], "sonnet[1m]")

    def test_the_roster_boundary_refuses_a_malformed_model(self):
        for bad in ("gpt-5", "", 7, "opus\n", "--model=opus"):
            with self.assertRaises(pr.RosterError, msg=repr(bad)):
                pr.validate_workers({WID: {"state": "live", "model": bad}})


class Launcher(unittest.TestCase):
    def surface_args(self, model):
        with tempfile.TemporaryDirectory() as td:
            stub = Path(td) / "bin"
            stub.mkdir()
            for t in ("lsof", "launchctl", "pgrep", "tmux"):
                (stub / t).write_text("#!/bin/sh\nexit 1\n")
                (stub / t).chmod(0o755)
            env = {"HOME": os.environ.get("HOME", td), "PATH": f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                   "SUTANDO_TMUX_SESSION": "sutando-worker-test", "SUTANDO_INSTANCE_ID": WID,
                   "SUTANDO_TASK_WATCH_MOD_WORKER": "0", "SUTANDO_WORKER_MODEL": model}
            r = subprocess.run(["bash", str(SCRIPTS / "launch-worker-session.sh"), "--print-surface-args"],
                               env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def test_the_launcher_passes_the_planned_model_as_one_model_flag(self):
        args = self.surface_args("sonnet[1m]")
        self.assertEqual(args.count("--model"), 1, args)
        self.assertEqual(args[args.index("--model") + 1], "sonnet[1m]")

    def test_no_planned_model_passes_no_model_flag(self):
        self.assertNotIn("--model", self.surface_args(""))


if __name__ == "__main__":
    unittest.main()
