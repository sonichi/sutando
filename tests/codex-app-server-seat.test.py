#!/usr/bin/env python3
"""The Codex app-server seat (src/agent/codex/app-server/seat.py) against a fake app-server.

No Codex is started: the seat's child is a script that speaks the JSON-RPC lines the seat needs
and records every line it is sent. The seat itself runs in this process.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SEAT_PY = REPO / "src" / "agent" / "codex" / "app-server" / "seat.py"
NOTIFIER = REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh"
POOL = REPO / "skills" / "worker-pool" / "scripts"

spec = importlib.util.spec_from_file_location("codex_seat", SEAT_PY)
seat_mod = importlib.util.module_from_spec(spec)
sys.modules["codex_seat"] = seat_mod
spec.loader.exec_module(seat_mod)
sys.path.insert(0, str(POOL))
import pool_delivery  # noqa: E402
from delivery import task_dispatch  # noqa: E402

FAKE_APP_SERVER = r'''
import json, os, re, sys

D = os.environ["FAKE_DIR"]


def rec(obj):
    with open(os.path.join(D, "requests.jsonl"), "a") as f:
        f.write(json.dumps(obj) + "\n")


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def plan():
    path = os.path.join(D, "plan.json")
    try:
        steps = json.load(open(path))
    except (OSError, ValueError):
        return "result"
    if not steps:
        return "result"
    step = steps.pop(0)
    json.dump(steps, open(path, "w"))
    return step


def threads():
    try:
        return open(os.path.join(D, "threads")).read().split()
    except OSError:
        return []


def write_result(prompt):
    path = re.search(r"write the result to (.+?\.txt)\.", prompt).group(1)
    with open(path, "w") as f:
        f.write("done by the fake\n")


rec({"argv": sys.argv[1:], "pid": os.getpid(), "cwd": os.getcwd()})
# Like Codex, the login is read once, at start; a later sign-in revokes it (D/revoked) and writes a new one.
try:
    TOKEN = open(os.path.join(os.environ["CODEX_HOME"], "auth.json")).read().strip()
except (KeyError, OSError):
    TOKEN = ""


def revoked():
    try:
        return TOKEN in open(os.path.join(D, "revoked")).read().split()
    except OSError:
        return False


UNAUTH = {"message": "Your access token could not be refreshed because your refresh token was revoked. "
                     "Please log out and sign in again.", "codexErrorInfo": "unauthorized"}
if os.path.exists(os.path.join(D, "exit-at-start")):
    sys.exit(3)
sys.stdout.write("this line is not JSON\n[1]\n")
state = {"turn": None, "thread": None, "waiting": 0, "after": None, "n": 0}


def finish(status="completed", error=None):
    out({"method": "turn/completed", "params": {"threadId": state["thread"], "turn": {
        "id": state["turn"], "status": status, "error": error}}})
    state["turn"] = None


SERVER_REQUESTS = ["item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                   "item/permissions/requestApproval", "item/tool/requestUserInput",
                   "mcpServer/elicitation/request", "item/tool/call", "execCommandApproval",
                   "applyPatchApproval", "currentTime/read", "account/chatgptAuthTokens/refresh"]

for line in sys.stdin:
    m = json.loads(line)
    rec(m)
    method = m.get("method")
    if "id" in m and method is None:
        state["waiting"] -= 1
        if state["waiting"] == 0 and state["after"]:
            state["after"]()
            state["after"] = None
        continue
    if "id" not in m:
        continue
    rid, p = m["id"], m.get("params") or {}
    if method == "initialize":
        out({"id": rid, "result": {"userAgent": "fake"}})
    elif method == "thread/start":
        state["thread"] = "thr-%d" % (len(threads()) + 1)
        with open(os.path.join(D, "threads"), "a") as f:
            f.write(state["thread"] + "\n")
        out({"id": rid, "result": {"thread": {"id": state["thread"]}}})
    elif method == "thread/resume":
        if os.path.exists(os.path.join(D, "die-on-resume")):
            os._exit(9)
        if p.get("threadId") in threads():
            state["thread"] = p["threadId"]
            out({"id": rid, "result": {"thread": {"id": state["thread"]}}})
        else:
            out({"id": rid, "error": {"code": -32600, "message": "no rollout found"}})
    elif method == "turn/start":
        try:
            refused = open(os.path.join(D, "refuse-model")).read().strip()
        except OSError:
            refused = ""
        if refused and p.get("model") == refused:
            out({"id": rid, "error": {"code": -32602, "message": "model %s is not supported" % refused}})
            continue
        step = "unauth" if TOKEN and revoked() else plan()
        if step == "refuse":
            out({"id": rid, "error": {"code": -32000, "message": "thread is busy"}})
            continue
        state["n"] += 1
        state["turn"] = "turn-%d-%d" % (os.getpid(), state["n"])
        prompt = p["input"][0]["text"]
        probe = os.environ.get("FAKE_PROBE")
        if probe:
            rec({"probe": [os.path.exists(x) for x in probe.split(":")]})
        thread, turn = state["thread"], state["turn"]
        out({"id": rid, "result": {"turn": {"id": turn, "status": "inProgress"}}})
        out({"method": "turn/started", "params": {"threadId": thread, "turn": {"id": turn}}})
        out({"method": "item/started", "params": {"threadId": "another-thread", "item": {
            "type": "commandExecution", "command": "NOT THIS SEAT"}}})
        out({"method": "item/started", "params": {"threadId": thread, "turnId": turn, "item": {
            "type": "commandExecution", "command": "cat the task"}}})
        if step == "result":
            write_result(prompt)
            out({"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": {
                "type": "agentMessage", "text": "Result written."}}})
            finish()
        elif step == "none":
            finish()
        elif step == "fail":
            out({"method": "error", "params": {"threadId": thread, "error": {"message": "usage limit reached"}}})
            finish("failed", {"message": "usage limit reached"})
        elif step == "unauth":
            out({"method": "error", "params": {"threadId": thread, "error": UNAUTH}})
            finish("failed", UNAUTH)
        elif step in ("limit", "limit-bare", "limit-retried", "limit-retry-other"):
            error = {"message": "You've hit your usage limit. Upgrade to Pro or try again at 3:05 PM.",
                     "codexErrorInfo": "usageLimitExceeded", "additionalDetails": None}
            if step == "limit":
                out({"method": "account/rateLimits/updated", "params": {"rateLimits": {
                    "limitId": "codex", "primary": {"usedPercent": 100, "windowDurationMins": 300,
                                                    "resetsAt": 4102444800}}}})
            out({"method": "error", "params": {"threadId": thread, "turnId": turn, "error": error,
                                               "willRetry": step.startswith("limit-retr")}})
            if step == "limit-retried":
                write_result(prompt)
                finish()
            elif step == "limit-retry-other":
                finish("failed", {"message": "stream disconnected"})
            else:
                finish("failed", error)
        elif step == "model-missing":
            error = {"message": "Error running remote compact task: The 'gpt-6.1-terra' model is not supported "
                                "when using Codex with a ChatGPT account.", "codexErrorInfo": "modelNotFound"}
            out({"method": "error", "params": {"threadId": thread, "turnId": turn, "error": error}})
            finish("failed", error)
        elif step == "model-missing-at-start":
            error = {"message": "The 'gpt-6-1-terra' model is not supported when using Codex with a ChatGPT account.",
                     "codexErrorInfo": "modelNotFound"}
            out({"method": "thread/status/changed", "params": {"threadId": thread, "status": {"type": "systemError"}}})
            out({"method": "error", "params": {"threadId": thread, "turnId": "another-turn-id", "error": error}})
            finish("failed", None)
        elif step == "fail-other":
            finish("failed", {"message": "stream disconnected before completion",
                              "codexErrorInfo": "responseStreamDisconnected"})
        elif step == "die":
            os._exit(7)
        elif step == "die_after_result":
            write_result(prompt)
            os._exit(7)
        elif step == "approval":
            state["waiting"] = len(SERVER_REQUESTS)
            state["after"] = lambda prompt=prompt: (write_result(prompt), finish())
            for i, name in enumerate(SERVER_REQUESTS):
                out({"id": "srv-%d" % i, "method": name, "params": {"threadId": thread}})
    elif method == "model/list":
        try:
            models = json.load(open(os.path.join(D, "models.json")))
        except (OSError, ValueError):
            out({"id": rid, "error": {"code": -32601, "message": "method not found"}})
            continue
        if isinstance(models, dict):
            out({"id": rid, "result": models})
        else:
            out({"id": rid, "result": {"data": models, "nextCursor": None}})
    elif method == "account/read":
        signed_in = TOKEN and not revoked() and not TOKEN.startswith("unreadable")
        out({"id": rid, "result": {"account": {"type": "chatgpt", "email": "owner@example.com"} if signed_in else None}})
    elif method == "turn/interrupt":
        if os.path.exists(os.path.join(D, "die-on-interrupt")):
            os._exit(8)
        out({"id": rid, "result": {}})
        if state["turn"] and not os.path.exists(os.path.join(D, "deaf")):
            finish("interrupted")
    else:
        out({"id": rid, "result": {}})
'''

STAGE_HOOK = r'''
import os, subprocess, sys
log = os.environ["STAGE_LOG"]
args = " ".join(sys.argv[1:])
with open(log, "a") as f:
    f.write(args + "\n")
if os.path.exists(log + ".fail"):
    sys.exit(1)
if args.endswith("--stage done"):
    if os.path.exists(log + ".fail-done"):
        sys.exit(1)
    if os.path.exists(log + ".fail-done-once"):
        os.unlink(log + ".fail-done-once")
        sys.exit(1)
real = os.environ.get("STAGE_REAL")
sys.exit(subprocess.run([sys.executable, real, *sys.argv[1:]], stdout=subprocess.DEVNULL).returncode if real else 0)
'''


class SeatCase(unittest.TestCase):
    worker = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.ws = root / "ws"
        self.tasks, self.results = self.ws / "tasks", self.ws / "results"
        for d in (self.tasks, self.results, self.ws / "state"):
            d.mkdir(parents=True)
        self.fake_dir = root / "fake"
        self.fake_dir.mkdir()
        script = root / "fake-app-server.py"
        script.write_text(FAKE_APP_SERVER, encoding="utf-8")
        self.fake = root / "fake-codex"
        self.fake.write_text(f'#!/bin/bash\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        self.fake.chmod(0o755)
        self.stage_log = root / "stage.log"
        self.hook = root / "stage-hook.py"
        self.hook.write_text(STAGE_HOOK, encoding="utf-8")
        self.codex_home = root / "codex-home"
        self.codex_home.mkdir()
        env = mock.patch.dict(os.environ, {"FAKE_DIR": str(self.fake_dir), "STAGE_LOG": str(self.stage_log),
                                           "CODEX_HOME": str(self.codex_home)})
        env.start()
        self.addCleanup(env.stop)
        for name in ("SUTANDO_POOL_DELIVERY_SCRIPT", "FAKE_PROBE", "STAGE_REAL"):
            os.environ.pop(name, None)
        self.out = io.StringIO()
        capture = contextlib.redirect_stdout(self.out)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        self.now = [1000.0]
        self.seats = []

    # -- fixtures --

    def declaration(self, **extra) -> Path:
        state = self.ws / "state" / "codex-seat" / "seat-under-test"
        argv = ["declare", "--out", str(state / "host.json"), "--inbox", str(extra.pop("inbox", self.tasks)),
                "--results", str(self.results), "--state", str(state), "--cwd", str(self.ws)]
        if extra.get("payloads"):
            argv += ["--payloads", str(extra["payloads"])]
        if extra.get("stage"):
            argv += ["--stage-command", *extra["stage"]]
        self.assertEqual(seat_mod.main(argv), 0)
        return state / "host.json"

    def make(self, decl=None, **kw):
        host = seat_mod.supervised_host.load(str(decl or self.declaration()))
        kw.setdefault("backoff_s", 0.0)
        kw.setdefault("clock", lambda: self.now[0])
        s = seat_mod.Seat(host, self.ws, "seat-under-test", worker=self.worker, codex=str(self.fake),
                          config=['web_search="live"', "check_for_update_on_startup=false"], **kw)
        self.seats.append(s)
        self.addCleanup(s.close_server)
        return s

    def copy_of(self, s, name: str) -> Path:
        """Where the seat keeps its own copy of a task's admitted bytes while the task runs."""
        return Path(s.host.state) / "payloads" / name

    def task(self, name: str, body: str = "task: do it\n", folder=None) -> Path:
        path = (folder or self.tasks) / name
        path.write_text(body, encoding="utf-8")
        return path

    def plan(self, *steps):
        (self.fake_dir / "plan.json").write_text(json.dumps(list(steps)), encoding="utf-8")

    def sent(self, method=None):
        try:
            lines = (self.fake_dir / "requests.jsonl").read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        rows = [json.loads(line) for line in lines]
        return rows if method is None else [r for r in rows if r.get("method") == method]

    def prompts(self):
        return [r["params"]["input"][0]["text"] for r in self.sent("turn/start")]

    def when_turn_starts(self, action, count=1):
        """Run `action` on another thread once the fake has been sent `count` turn/start requests."""
        def wait():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and len(self.sent("turn/start")) < count:
                time.sleep(0.02)
            action()
        t = threading.Thread(target=wait, daemon=True)
        t.start()
        self.addCleanup(t.join, 25)

    def ruling(self, task_id: str) -> Path:
        d = self.ws / "state" / "stop-interrupts"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{task_id}.ruling.json"
        path.write_text(json.dumps({"target": task_id, "control": "task-ctl-1-abcdef", "at": 1.0}))
        return path


class HandshakeAndThread(SeatCase):
    def test_the_app_server_argv_is_codex_then_every_c_pair_then_the_subcommand(self):
        # The desktop's liveness check reads the program up to the first flag; it must read `codex`.
        s = self.make()
        self.assertEqual(s.argv, [str(self.fake), "-c", 'web_search="live"', "-c",
                                  "check_for_update_on_startup=false", "app-server", "--listen", "stdio://"])
        bare = seat_mod.Seat(s.host, self.ws, "bare", codex="codex")
        self.assertEqual(bare.argv, ["codex", "app-server", "--listen", "stdio://"])
        before_first_flag = " ".join(s.argv).split(" -")[0]
        self.assertEqual(os.path.basename(before_first_flag), os.path.basename(str(self.fake)))

    def test_the_app_server_runs_in_the_seat_folder_not_the_one_the_seat_was_started_from(self):
        elsewhere = Path(self.tmp.name) / "elsewhere"  # stands in for an unreadable launch folder
        elsewhere.mkdir()
        here = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, here)
        s = self.make()
        s.ensure_server()
        self.assertEqual(os.path.realpath(self.sent()[0]["cwd"]), os.path.realpath(s.cwd))
        self.assertEqual(os.path.realpath(s.cwd), os.path.realpath(self.ws), "the declaration's cwd")
        self.assertNotEqual(os.path.realpath(self.sent()[0]["cwd"]), os.path.realpath(elsewhere))

    def test_handshake_then_one_durable_thread_started_with_the_tui_launch_settings(self):
        s = self.make(model="gpt-test", instructions="You are a worker.")
        s.ensure_server()
        argv = self.sent()[0]["argv"]
        self.assertEqual(argv, ["-c", 'web_search="live"', "-c", "check_for_update_on_startup=false",
                                "app-server", "--listen", "stdio://"])
        init = self.sent("initialize")[0]["params"]
        self.assertEqual(init["clientInfo"]["name"], "sutando-codex-seat")
        self.assertEqual(init["capabilities"], {"experimentalApi": True})
        self.assertEqual(len(self.sent("initialized")), 1)
        start = self.sent("thread/start")[0]["params"]
        self.assertEqual(start, {"cwd": str(self.ws), "approvalPolicy": "never", "sandbox": "danger-full-access",
                                 "model": "gpt-test", "developerInstructions": "You are a worker."})
        self.assertEqual(s.thread_file().read_text().splitlines(), ["thr-1", "seat-under-test"])
        self.assertEqual(self.sent("thread/resume"), [])

    def test_a_second_run_resumes_the_saved_thread(self):
        decl = self.declaration()
        first = self.make(decl)
        first.ensure_server()
        first.close_server()
        second = self.make(decl)
        second.ensure_server()
        self.assertEqual(second.thread_id, "thr-1")
        self.assertEqual(self.sent("thread/resume")[0]["params"]["threadId"], "thr-1")
        self.assertEqual(len(self.sent("thread/start")), 1, "no second thread")

    def test_a_thread_the_server_no_longer_knows_falls_back_to_a_new_one(self):
        decl = self.declaration()
        first = self.make(decl)
        first.ensure_server()
        first.close_server()
        (self.fake_dir / "threads").write_text("")
        second = self.make(decl)
        second.ensure_server()
        self.assertEqual(second.thread_id, "thr-1")
        self.assertEqual(len(self.sent("thread/start")), 2)
        self.assertIn("thread/resume failed", self.out.getvalue())

    def test_an_app_server_that_will_not_start_is_tried_twice_then_fatal(self):
        (self.fake_dir / "exit-at-start").touch()
        self.task("task-1.txt")
        s = self.make()
        self.assertEqual(s.serve(once=True), seat_mod.EXIT_FATAL)
        self.assertEqual(len([r for r in self.sent() if "argv" in r]), 2)
        self.assertIn("FATAL", self.out.getvalue())
        self.assertIn("codex-app-server-select off", self.out.getvalue(), "it names the way back to the TUI")
        self.assertEqual(list(task_dispatch.pending_candidates(self.tasks, self.results)), ["task-1.txt"])

    def test_a_server_that_dies_during_resume_is_a_start_failure_not_a_new_thread(self):
        decl = self.declaration()
        first = self.make(decl)
        first.ensure_server()
        first.close_server()
        (self.fake_dir / "die-on-resume").touch()
        second = self.make(decl)
        with self.assertRaises(seat_mod.SeatFatal):
            second.ensure_server()
        self.assertEqual(len(self.sent("thread/start")), 1)

    def test_only_one_seat_serves_a_state_dir(self):
        decl = self.declaration()
        first = self.make(decl)
        self.assertTrue(first.lock())
        self.addCleanup(os.close, first.lock_fd)
        self.assertEqual(self.make(decl).serve(once=True), seat_mod.EXIT_LOCKED)
        self.assertEqual(self.sent(), [], "the second seat started no app-server")

    def test_a_restarted_seat_drops_the_copies_the_seat_before_it_left(self):
        """A copy in a state dir whose lock is held reads as "a running seat took this task". The
        launcher reuses the state dir, so a replacement seat must not hold the lock over a copy only
        the interrupted seat had taken: it is gone before the new seat serves anything."""
        decl = self.declaration()
        left = decl.parent / "payloads" / "task-1.txt"
        left.parent.mkdir(parents=True)
        left.write_text("task: taken by the seat that was interrupted\n", encoding="utf-8")
        replacement = self.make(decl)
        self.assertTrue(replacement.lock())
        self.addCleanup(os.close, replacement.lock_fd)
        self.assertFalse(left.exists(), "no inherited copy under the replacement's lock")
        self.assertEqual(self.sent(), [], "and it was dropped before any app-server was started")

    def test_a_seat_refused_the_lock_leaves_the_running_seats_copies_alone(self):
        decl = self.declaration()
        running = self.make(decl)
        self.assertTrue(running.lock())
        self.addCleanup(os.close, running.lock_fd)
        copy = decl.parent / "payloads" / "task-1.txt"
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text("task: the running seat's turn\n", encoding="utf-8")
        self.assertEqual(self.make(decl).serve(once=True), seat_mod.EXIT_LOCKED)
        self.assertTrue(copy.exists())

    def test_a_copy_that_cannot_be_removed_is_logged_and_the_seat_still_starts(self):
        decl = self.declaration()
        left = decl.parent / "payloads" / "task-1.txt"
        left.parent.mkdir(parents=True)
        left.write_text("task\n", encoding="utf-8")
        s = self.make(decl)
        with mock.patch.object(seat_mod.Path, "unlink", side_effect=OSError("denied")):
            self.assertTrue(s.lock())
        self.addCleanup(os.close, s.lock_fd)
        self.assertIn("could not be removed: task-1.txt", self.out.getvalue())


class Turns(SeatCase):
    def test_one_turn_per_task_in_dispatch_order(self):
        for i, name in enumerate(("task-b.txt", "task-a.txt", "task-c.txt")):
            path = self.task(name)
            os.utime(path, (100 + i, 100 + i))
        urgent = self.task("task-z.txt", "priority: urgent\ntask: now\n")
        os.utime(urgent, (500, 500))
        expected = list(task_dispatch.pending_candidates(self.tasks, self.results))
        self.assertEqual(expected, ["task-z.txt", "task-b.txt", "task-a.txt", "task-c.txt"])
        s = self.make()
        self.assertEqual(s.serve(once=True), 0)
        ran = [re.match(r"Sutando task ready: (\S+)\. ", p).group(1) for p in self.prompts()]
        self.assertEqual(ran, expected)
        self.assertEqual(len(self.sent("thread/start")), 1, "every task ran on the seat's one thread")
        self.assertEqual({r["params"]["threadId"] for r in self.sent("turn/start")}, {"thr-1"})
        for name in expected:
            self.assertTrue(task_dispatch.has_ready_result(self.results, name))
        self.assertEqual(s.step(), None, "nothing is run twice")
        log = self.out.getvalue()
        for line in ("task task-z.txt: picked", "turn started", "  $ cat the task", "  > Result written.",
                     "turn completed in", "result written", "seat stopped"):
            self.assertIn(line, log)
        self.assertNotIn("NOT THIS SEAT", log, "another thread's items are not this seat's")

    def test_a_task_that_already_has_a_result_gets_no_turn(self):
        self.task("task-1.txt")
        (self.results / "task-1.txt").write_text("answered earlier\n")
        s = self.make()
        s.ensure_server()
        self.assertIsNone(s.step())
        self.assertEqual(s.run_task("task-1.txt", self.tasks / "task-1.txt"), "done")
        self.assertEqual(self.sent("turn/start"), [])

    def test_a_turn_that_ends_without_a_result_is_recorded_and_the_task_stays_pending(self):
        self.task("task-1.txt")
        self.plan("none", "none", "result")
        s = self.make(retry_s=30, hold_s=900, max_attempts=2)
        s.ensure_server()
        self.assertEqual(s.step(), "no-result")
        rec = json.loads(s.record_file("task-1.txt").read_text())
        self.assertEqual((rec["outcome"], rec["attempts"], rec["thread"]), ("no-result", 1, "thr-1"))
        self.assertIn("completed without writing", self.out.getvalue())
        self.assertEqual(list(task_dispatch.pending_candidates(self.tasks, self.results)), ["task-1.txt"])
        self.assertIsNone(s.step(), "not re-run before the retry delay")
        self.now[0] += 31
        self.assertEqual(s.step(), "no-result")
        self.assertEqual(json.loads(s.record_file("task-1.txt").read_text())["attempts"], 2)
        self.now[0] += 31
        self.assertIsNone(s.step(), "after the last attempt it waits the longer hold")
        self.now[0] += 900
        self.assertEqual(s.step(), "done")
        self.assertFalse(s.record_file("task-1.txt").exists(), "a finished task keeps no record")
        self.assertEqual(len(self.sent("turn/start")), 3)

    def test_a_rewritten_task_file_is_a_new_turn_at_once(self):
        path = self.task("task-1.txt")
        self.plan("none", "result")
        s = self.make()
        s.ensure_server()
        self.assertEqual(s.step(), "no-result")
        os.utime(path, (path.stat().st_mtime + 50, path.stat().st_mtime + 50))
        self.assertEqual(s.step(), "done")

    def test_a_failed_turn_is_recorded_with_its_error(self):
        self.task("task-1.txt")
        self.plan("fail")
        s = self.make()
        s.ensure_server()
        self.assertEqual(s.step(), "failed")
        rec = json.loads(s.record_file("task-1.txt").read_text())
        self.assertEqual((rec["outcome"], rec["detail"]), ("failed", "usage limit reached"))
        self.assertIn("error: ", self.out.getvalue())
        self.assertFalse(task_dispatch.has_ready_result(self.results, "task-1.txt"))

    def test_a_refused_turn_start_is_a_failed_attempt_not_a_crash(self):
        self.task("task-1.txt")
        self.plan("refuse")
        s = self.make()
        s.ensure_server()
        self.assertEqual(s.step(), "failed")
        self.assertIn("thread is busy", json.loads(s.record_file("task-1.txt").read_text())["detail"])

    def test_every_approval_and_elicitation_is_declined_and_the_turn_goes_on(self):
        self.task("task-1.txt")
        self.plan("approval")
        s = self.make()
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        replies = {r["id"]: r for r in self.sent() if str(r.get("id", "")).startswith("srv-")}
        self.assertEqual(replies["srv-0"]["result"], {"decision": "decline"})
        self.assertEqual(replies["srv-1"]["result"], {"decision": "decline"})
        self.assertEqual(replies["srv-2"]["result"], {"permissions": {}})
        self.assertEqual(replies["srv-3"]["result"], {"answers": {}})
        self.assertEqual(replies["srv-4"]["result"], {"action": "decline"})
        self.assertEqual(replies["srv-5"]["result"], {"contentItems": [], "success": False})
        self.assertEqual(replies["srv-6"]["result"], {"decision": "denied"})
        self.assertEqual(replies["srv-7"]["result"], {"decision": "denied"})
        self.assertIn("currentTimeMs", replies["srv-8"]["result"])
        self.assertEqual(replies["srv-9"]["error"]["code"], -32601)
        self.assertEqual(len(s.declined), 10)
        log = self.out.getvalue()
        self.assertIn("declined item/commandExecution/requestApproval", log)
        self.assertIn("refused account/chatgptAuthTokens/refresh", log)


class PromptParity(SeatCase):
    """The seat's prompt is the notifier's: the bash lines are run as they are written there."""

    def notifier_prompt(self, filename, payload, results, worker="", context=""):
        source = NOTIFIER.read_text(encoding="utf-8")
        lines = [ln.strip() for ln in source.splitlines() if ln.strip().startswith('prompt="')]
        self.assertEqual(len(lines), 3, "the notifier builds its prompt in three assignments")
        core, worker_line, context_line = lines
        script = "\n".join([
            'task_payload() { printf "%s" "$PAYLOAD"; }',
            core,
            f'if [ -n "$WORKER_INSTANCE" ]; then {worker_line}; fi',
            f'if [ -n "$workstream_context_file" ]; then {context_line}; fi',
            'printf "%s" "$prompt"'])
        env = {"PATH": os.environ["PATH"], "filename": filename, "PAYLOAD": str(payload), "RESULTS_DIR": str(results),
               "WORKER_INSTANCE": worker, "workstream_context_file": context}
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=True).stdout

    def test_core_worker_and_context_prompts_match_the_notifier_byte_for_byte(self):
        args = ("task-1.txt", "/ws/tasks/task-1.txt", "/ws/results")
        self.assertEqual(seat_mod.task_prompt(*args), self.notifier_prompt(*args))
        self.assertEqual(seat_mod.task_prompt(*args, worker="w1"), self.notifier_prompt(*args, worker="w1"))
        self.assertEqual(seat_mod.task_prompt(*args, context_file="/ws/state/ctx"),
                         self.notifier_prompt(*args, context="/ws/state/ctx"))
        self.assertTrue(seat_mod.task_prompt(*args).startswith("Sutando task ready: task-1.txt. Read /ws/tasks/"))

    def test_the_turn_is_started_with_exactly_that_prompt(self):
        self.task("task-1.txt")
        s = self.make(context=lambda name: "")
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        # The notifier's wording, with the seat's copy of the admitted bytes where the inbox path was.
        self.assertEqual(self.prompts(),
                         [self.notifier_prompt("task-1.txt", self.copy_of(s, "task-1.txt"), self.results)])
        turn = self.sent("turn/start")[0]["params"]
        self.assertEqual(turn["cwd"], str(self.ws))
        self.assertFalse(self.copy_of(s, "task-1.txt").exists(), "the copy is removed once the task is done")

    def test_prior_workstream_context_is_handed_over_as_the_notifier_does(self):
        self.task("task-1.txt")
        s = self.make(context=lambda name: f"earlier work for {name}\n")
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        ctx = Path(s.host.state) / "context" / "task-1.txt"
        self.assertEqual(self.prompts(), [self.notifier_prompt("task-1.txt", self.copy_of(s, "task-1.txt"),
                                                               self.results, context=str(ctx))])
        self.assertFalse(ctx.exists(), "removed once the task has its result")

    def test_workstream_context_lookup(self):
        stub = Path(self.tmp.name) / "workstreams.py"
        stub.write_text("import sys\nprint('ctx for', sys.argv[2])\nsys.exit(0 if sys.argv[2] != 'bad' else 1)\n")
        with mock.patch.object(seat_mod, "WORKSTREAMS", stub):
            self.assertEqual(seat_mod.workstream_context("task-1.txt"), "ctx for task-1.txt\n")
            self.assertEqual(seat_mod.workstream_context("bad"), "")
            with mock.patch.object(seat_mod.subprocess, "run", side_effect=OSError("no")):
                self.assertEqual(seat_mod.workstream_context("task-1.txt"), "")
        with mock.patch.object(seat_mod, "WORKSTREAMS", stub.with_name("absent.py")):
            self.assertEqual(seat_mod.workstream_context("task-1.txt"), "")


class StopAndTimeout(SeatCase):
    def test_a_stop_ruling_interrupts_the_turn_and_the_task_stays_pending(self):
        self.task("task-1.txt")
        self.task("task-2.txt")
        os.utime(self.tasks / "task-1.txt", (100, 100))
        self.plan("hang", "result")
        s = self.make()
        s.ensure_server()
        self.when_turn_starts(lambda: self.ruling("task-1"))
        self.assertEqual(s.step(), "stopped")
        interrupt = self.sent("turn/interrupt")
        self.assertEqual(len(interrupt), 1)
        self.assertEqual(interrupt[0]["params"]["threadId"], "thr-1")
        self.assertEqual(interrupt[0]["params"]["turnId"], self.out.getvalue().split("turn started (")[1].split(")")[0])
        self.assertFalse(task_dispatch.has_ready_result(self.results, "task-1.txt"))
        self.assertIn("task-1.txt", list(task_dispatch.pending_candidates(self.tasks, self.results)))
        self.assertEqual(json.loads(s.record_file("task-1.txt").read_text())["outcome"], "stopped")
        self.assertIn("stopped by Stop", self.out.getvalue())
        self.assertEqual(s.step(), "done", "the next task runs")
        self.assertIn("task-2.txt", self.prompts()[1])
        self.now[0] += 10 ** 6
        self.assertIsNone(s.step(), "a stopped task is never started again by the seat")
        self.assertEqual(len(self.sent("turn/start")), 2)

    def test_a_child_that_dies_on_the_interrupt_does_not_get_the_stopped_task_again(self):
        self.task("task-1.txt")
        self.plan("hang", "result")
        (self.fake_dir / "die-on-interrupt").touch()
        s = self.make()
        s.ensure_server()
        self.when_turn_starts(lambda: self.ruling("task-1"))
        self.assertEqual(s.step(), "stopped")
        self.assertEqual(len(self.sent("turn/interrupt")), 1)
        self.assertEqual(len(self.sent("turn/start")), 1, "the Stop outlives the child: no second turn")
        self.assertFalse(task_dispatch.has_ready_result(self.results, "task-1.txt"))
        self.assertEqual(list(task_dispatch.pending_candidates(self.tasks, self.results)), ["task-1.txt"])
        self.assertEqual(json.loads(s.record_file("task-1.txt").read_text())["outcome"], "stopped")
        self.assertIsNone(s.step())

    def test_a_child_that_dies_mid_turn_with_a_stop_open_is_one_dispatch_and_no_retry(self):
        self.task("task-1.txt")
        self.plan("die", "result")
        s = self.make()
        s.ensure_server()
        request = seat_mod.Rpc.request

        def stop_opens_as_the_turn_is_dispatched(rpc, method, params=None, timeout=120):
            result = request(rpc, method, params, timeout)
            if method == "turn/start":
                self.ruling("task-1")
            return result

        with mock.patch.object(seat_mod.Rpc, "request", stop_opens_as_the_turn_is_dispatched):
            self.assertEqual(s.step(), "stopped")
        self.assertEqual(len(self.sent("turn/start")), 1, "exactly one dispatch")
        self.assertEqual(len([r for r in self.sent() if "argv" in r]), 1, "no app-server was started for a retry")
        self.assertFalse(task_dispatch.has_ready_result(self.results, "task-1.txt"))
        self.assertEqual(list(task_dispatch.pending_candidates(self.tasks, self.results)), ["task-1.txt"])
        self.assertEqual(json.loads(s.record_file("task-1.txt").read_text())["outcome"], "stopped")

    def test_a_stop_that_arrives_during_the_restart_backoff_cancels_the_retry(self):
        self.task("task-1.txt")
        self.plan("die", "result")
        s = self.make(backoff_s=0.01)
        s.ensure_server()
        real_sleep = time.sleep

        def sleep(seconds):  # time.sleep is the one module: subprocess's wait polls through it too
            if seconds == 0.01:
                self.ruling("task-1")
            else:
                real_sleep(seconds)

        with mock.patch.object(seat_mod.time, "sleep", side_effect=sleep) as backoff:
            self.assertEqual(s.step(), "stopped")
        self.assertEqual(backoff.call_args_list.count(mock.call(0.01)), 1, backoff.call_args_list)
        self.assertEqual(len(self.sent("turn/start")), 1, "no retry turn once a Stop is open")
        self.assertEqual(self.sent("turn/interrupt"), [], "there is no turn left to interrupt")
        self.assertFalse(task_dispatch.has_ready_result(self.results, "task-1.txt"))
        self.assertEqual(list(task_dispatch.pending_candidates(self.tasks, self.results)), ["task-1.txt"])
        self.assertEqual(json.loads(s.record_file("task-1.txt").read_text())["outcome"], "stopped")
        self.assertIn("not started again", self.out.getvalue())

    def test_a_stop_seen_once_stays_seen_after_its_ruling_record_is_gone(self):
        s = self.make()
        self.assertFalse(s.stop_requested("task-1.txt"))
        ruling = self.ruling("task-1")
        self.assertTrue(s.stop_requested("task-1.txt"))
        ruling.unlink()
        self.assertTrue(s.stop_requested("task-1.txt"), "ruled meanwhile: still no new turn in this run")
        self.assertFalse(s.stop_requested("task-2.txt"))

    def test_a_task_stopped_before_its_turn_is_not_started(self):
        self.task("task-1.txt")
        self.ruling("task-1")
        s = self.make()
        s.ensure_server()
        self.assertEqual(s.step(), "stopped")
        self.assertEqual(self.sent("turn/start"), [])
        self.assertIsNone(s.step())

    def test_a_turn_past_the_timeout_is_interrupted_and_retried_later(self):
        self.task("task-1.txt")
        self.plan("hang")
        s = self.make(turn_timeout=0.2)
        s.ensure_server()
        self.assertEqual(s.step(), "timeout")
        self.assertEqual(len(self.sent("turn/interrupt")), 1)
        self.assertEqual(json.loads(s.record_file("task-1.txt").read_text())["outcome"], "timeout")

    def test_a_turn_that_ignores_the_interrupt_gets_its_app_server_replaced(self):
        self.task("task-1.txt")
        self.plan("hang", "result")
        (self.fake_dir / "deaf").touch()
        s = self.make(turn_timeout=0.2, interrupt_grace=0.3, retry_s=0)
        s.ensure_server()
        first_pid = s.rpc.p.pid
        self.assertEqual(s.step(), "timeout")
        self.assertIsNone(s.rpc)
        self.assertEqual(s.step(), "done")
        self.assertNotEqual(s.rpc.p.pid, first_pid)
        self.assertEqual(self.sent("thread/resume")[0]["params"]["threadId"], "thr-1")

    def test_a_seat_told_to_stop_mid_turn_leaves_the_task_pending(self):
        self.task("task-1.txt")
        self.plan("hang")
        s = self.make()
        self.when_turn_starts(s.request_stop)
        self.assertEqual(s.serve(), 0)
        self.assertEqual(list(task_dispatch.pending_candidates(self.tasks, self.results)), ["task-1.txt"])
        self.assertFalse(s.record_file("task-1.txt").exists())
        self.assertIn("the seat is stopping mid-turn", self.out.getvalue())


class Crash(SeatCase):
    def test_a_child_that_dies_mid_turn_is_restarted_and_the_task_retried_once_on_the_same_thread(self):
        self.task("task-1.txt")
        self.plan("die", "result")
        s = self.make()
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        self.assertEqual(len([r for r in self.sent() if "argv" in r]), 2, "one restart")
        self.assertEqual([r["params"]["threadId"] for r in self.sent("thread/resume")], ["thr-1"])
        self.assertEqual(len(self.sent("thread/start")), 1)
        prompts = self.prompts()
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[0], prompts[1])
        self.assertTrue(task_dispatch.has_ready_result(self.results, "task-1.txt"))
        self.assertEqual(s.failures, 0, "a completed turn clears the failure count")
        self.assertIn("app-server exited mid-turn", self.out.getvalue())

    def test_a_result_that_lands_while_the_server_is_restarted_means_one_turn_not_two(self):
        self.task("task-1.txt")
        self.plan("die", "result")
        s = self.make()
        s.ensure_server()
        failed = s.server_failed

        def answered_meanwhile(why):
            failed(why)
            (self.results / "task-1.txt").write_text("answered by the turn that died\n", encoding="utf-8")

        s.server_failed = answered_meanwhile
        self.assertEqual(s.step(), "done")
        self.assertEqual(len(self.sent("turn/start")), 1, "the reconnect found the answer and asked nothing")
        self.assertIsNone(s.step())

    def test_a_task_taken_away_while_the_server_is_restarted_is_not_started_again(self):
        self.task("task-1.txt")
        self.plan("die", "result")
        s = self.make()
        s.ensure_server()
        failed = s.server_failed

        def taken_meanwhile(why):
            failed(why)
            (self.tasks / "task-1.txt").unlink()

        s.server_failed = taken_meanwhile
        self.assertEqual(s.step(), seat_mod.WITHDRAWN)
        self.assertEqual(len(self.sent("turn/start")), 1)
        self.assertIn("not started again", self.out.getvalue())
        self.assertFalse(task_dispatch.has_ready_result(self.results, "task-1.txt"))

    def test_a_file_put_under_the_tasks_name_while_the_server_is_restarted_is_not_run_as_that_task(self):
        self.task("task-1.txt")
        self.plan("die", "result")
        s = self.make()
        s.ensure_server()
        failed = s.server_failed

        def replaced_meanwhile(why):
            failed(why)
            other = self.task("task-1.txt.new", "task: something else entirely\n", folder=self.ws)
            os.replace(other, self.tasks / "task-1.txt")

        s.server_failed = replaced_meanwhile
        self.assertEqual(s.step(), seat_mod.WITHDRAWN)
        self.assertEqual(len(self.sent("turn/start")), 1, "the prompt built for the first file is not sent again")

    def test_a_task_answered_or_taken_while_the_server_first_starts_is_not_dispatched(self):
        for name, meanwhile in (
                ("task-1.txt", lambda: (self.results / "task-1.txt").write_text("answered\n", encoding="utf-8")),
                ("task-2.txt", lambda: (self.tasks / "task-2.txt").unlink())):
            self.task(name)
            s = self.make()
            start = s.start_server

            def started_slowly(start=start, meanwhile=meanwhile):
                start()
                meanwhile()

            s.start_server = started_slowly
            self.assertEqual(s.step(), "done" if name == "task-1.txt" else seat_mod.WITHDRAWN)
            self.assertEqual(self.sent("turn/start"), [], name)
            s.close_server()

    def test_a_task_answered_or_taken_between_its_pick_and_a_warm_dispatch_is_not_dispatched(self):
        for name, meanwhile in (
                ("task-1.txt", lambda: (self.results / "task-1.txt").write_text("answered\n", encoding="utf-8")),
                ("task-2.txt", lambda: (self.tasks / "task-2.txt").unlink())):
            self.task(name)
            s = self.make()
            s.ensure_server()
            build = s.prompt_for

            def built_slowly(filename, payload, build=build, meanwhile=meanwhile):
                prompt = build(filename, payload)
                meanwhile()
                return prompt

            s.prompt_for = built_slowly
            self.assertEqual(s.step(), "done" if name == "task-1.txt" else seat_mod.WITHDRAWN)
            self.assertEqual(self.sent("turn/start"), [], name)
            s.close_server()

    def test_a_turn_that_reads_late_reads_the_admitted_bytes_and_settles_nothing_for_a_replacement(self):
        first = self.task("task-1.txt", "task: the admitted one\n")
        s = self.make()
        s.ensure_server()
        turn = s.run_turn
        read_late = []

        def reads_after_a_replacement(filename, prompt):
            other = self.task("task-1.txt.new", "task: something else entirely\n", folder=self.ws)
            os.replace(other, first)
            named = prompt.split("Read ", 1)[1].split(",", 1)[0]
            self.assertNotEqual(named, str(first), "the turn is never pointed at the inbox path")
            self.assertEqual(os.stat(named).st_mode & 0o777, 0o600)
            read_late.append(Path(named).read_text(encoding="utf-8"))
            return turn(filename, prompt)

        s.run_turn = reads_after_a_replacement
        self.assertEqual(s.step(), seat_mod.WITHDRAWN)
        self.assertEqual(read_late, ["task: the admitted one\n"])
        self.assertEqual(len(self.sent("turn/start")), 1)
        self.assertIn("neither in the inbox nor archived, so nothing is settled", self.out.getvalue())
        self.assertFalse(self.copy_of(s, "task-1.txt").exists())
        # The replacement gets neither the old result nor a done marker: it is a pending task again.
        self.assertFalse(task_dispatch.has_ready_result(self.results, "task-1.txt"))
        self.assertEqual(len(list((Path(s.host.state) / "unsettled-results").iterdir())), 1)
        self.assertEqual([name for name, _ in s.pending()], ["task-1.txt"])

    def test_a_result_gone_while_the_inbox_file_was_withdrawn_is_logged_as_gone(self):
        """VM 2026-11-22 22:42:16: the broker closed the task, the bridge withdrew its inbox file
        and archived the result between the seat's two reads, and the seat logged "the result is
        kept at " with no path. It now says the result was gone, and names no one as settling it."""
        first = self.task("task-1.txt")
        s = self.make()
        s.ensure_server()
        turn, aside = s.run_turn, s.set_result_aside

        def withdrawn_mid_turn(filename, prompt):
            out = turn(filename, prompt)
            first.unlink()
            return out

        def bridge_took_it_first(filename):
            (Path(self.results) / filename).unlink()
            return aside(filename)

        s.run_turn, s.set_result_aside = withdrawn_mid_turn, bridge_took_it_first
        self.assertEqual(s.step(), seat_mod.WITHDRAWN)
        self.assertIn("then gone from the results folder", self.out.getvalue())
        self.assertNotIn("bridge", self.out.getvalue())
        self.assertNotIn("the result is kept at", self.out.getvalue())

    def test_a_result_that_cannot_be_set_aside_says_so(self):
        first = self.task("task-1.txt")
        s = self.make()
        s.ensure_server()
        turn = s.run_turn

        def withdrawn_mid_turn(filename, prompt):
            out = turn(filename, prompt)
            first.unlink()
            return out

        s.run_turn, s.set_result_aside = withdrawn_mid_turn, lambda filename: ""
        self.assertEqual(s.step(), seat_mod.WITHDRAWN)
        self.assertIn("the result could not be set aside", self.out.getvalue())

    def test_a_file_that_changes_while_it_is_being_copied_is_not_started(self):
        self.task("task-1.txt")
        s = self.make()
        s.ensure_server()
        real, calls = seat_mod.fd_generation, []

        def changes_under_the_read(fd):
            calls.append(fd)
            g = real(fd)
            return g if len(calls) == 1 else g[:3] + (g[3] + 1,)

        with mock.patch.object(seat_mod, "fd_generation", changes_under_the_read):
            self.assertEqual(s.step(), seat_mod.WITHDRAWN)
        self.assertEqual(len(calls), 2, "read before and after the bytes")
        self.assertEqual(self.sent("turn/start"), [])
        self.assertFalse(self.copy_of(s, "task-1.txt").exists())
        self.assertEqual(s.step(), "done", "the file as it is now is a task like any other")

    def test_a_result_is_settled_for_an_archived_task_but_not_for_one_that_is_only_gone(self):
        for name, leaves, outcome in (
                ("task-1.txt", lambda f: os.replace(f, self.tasks / "archive" / f.name), "done"),
                ("task-2.txt", lambda f: f.unlink(), seat_mod.WITHDRAWN)):
            (self.tasks / "archive").mkdir(exist_ok=True)
            first = self.task(name)
            s = self.make()
            s.ensure_server()
            turn = s.run_turn

            def left_the_inbox(filename, prompt, turn=turn, first=first, leaves=leaves):
                out = turn(filename, prompt)
                leaves(first)
                return out

            s.run_turn = left_the_inbox
            self.assertEqual(s.step(), outcome, name)
            self.assertEqual(task_dispatch.has_ready_result(self.results, name), outcome == "done", name)
            s.close_server()

    def test_a_child_that_dies_twice_stops_the_seat_and_the_task_is_not_lost(self):
        self.task("task-1.txt")
        self.plan("die", "die")
        s = self.make()
        self.assertEqual(s.serve(once=True), seat_mod.EXIT_FATAL)
        self.assertEqual(len(self.sent("turn/start")), 2, "retried once, not more")
        self.assertEqual(list(task_dispatch.pending_candidates(self.tasks, self.results)), ["task-1.txt"])
        self.assertIsNone(s.rpc)

    def test_a_child_that_died_after_the_result_was_written_is_not_asked_again(self):
        self.task("task-1.txt")
        self.plan("die_after_result")
        s = self.make()
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        self.assertEqual(len(self.sent("turn/start")), 1)

    def test_a_child_that_died_between_tasks_is_replaced_before_the_next_turn(self):
        self.task("task-1.txt")
        s = self.make()
        s.ensure_server()
        s.rpc.p.kill()
        s.rpc.p.wait()
        self.assertEqual(s.step(), "done")
        self.assertEqual(len(self.sent("thread/resume")), 1)



def health(ws):
    """The core's account health record (core_account_health), {} when none."""
    try:
        return json.loads((Path(ws) / "state" / "core-account-health.json").read_text())
    except (OSError, ValueError):
        return {}


class AccountSwitch(SeatCase):
    """The child reads its login once, at start; a sign-in or account switch since revoked that login's refresh
    token (VM 11-22: the first turn after a switch failed with "refresh token was revoked"). A changed login
    file restarts the child before the next turn; a turn refused as unauthorized restarts it and is tried once
    more; refused again, the core seat shows the owner the account card."""

    def make(self, decl=None, **kw):
        s = super().make(decl, **kw)
        self.cards = self.fake_dir / "cards.log"
        script = self.fake_dir / "card.py"
        script.write_text("import sys\nopen(sys.argv[1], 'a').write(' '.join(sys.argv[2:]) + '\\n')\n")
        s.card_argv = [sys.executable, str(script), str(self.cards), *s.card_argv[2:]]
        return s

    def sign_in(self, token, revoke=None):
        """A sign-in: the new login on disk, the one it replaced revoked."""
        if revoke:
            with open(self.fake_dir / "revoked", "a") as f:
                f.write(revoke + "\n")
        (self.codex_home / "auth.json").write_text(token)

    def starts(self):
        return len([r for r in self.sent() if "argv" in r])

    def posted(self):
        return self.cards.read_text().splitlines() if self.cards.exists() else []

    def test_a_switch_restarts_the_app_server_before_the_next_turn(self):
        self.sign_in("tok-a")
        s = self.make()
        s.ensure_server()
        self.sign_in("tok-b", revoke="tok-a")
        self.task("task-1.txt")
        self.assertEqual(s.step(), "done")
        self.assertEqual((self.starts(), len(self.sent("turn/start"))), (2, 1), "restarted before the turn, not after a failure")
        self.assertEqual([r["params"]["threadId"] for r in self.sent("thread/resume")], ["thr-1"], "on the same thread")
        self.assertIn("login changed on disk", self.out.getvalue())
        self.task("task-2.txt")
        self.assertEqual(s.step(), "done")
        self.assertEqual(self.starts(), 2, "once per change")
        self.assertEqual(self.posted(), [])

    def test_an_unauthorized_turn_restarts_once_and_a_login_rotated_meanwhile_answers(self):
        self.sign_in("tok-a")
        self.task("task-1.txt")
        self.plan("unauth", "result")
        s = self.make()
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        self.assertEqual((self.starts(), len(self.sent("turn/start"))), (2, 2))
        self.assertEqual(s.failures, 0, "a refused login is not a server failure")
        self.assertIn("turn refused as unauthorized", self.out.getvalue())
        self.assertEqual(self.posted(), [])

    def test_still_unauthorized_after_the_restart_shows_the_owner_the_account_card_once(self):
        self.sign_in("tok-a", revoke="tok-a")     # revoked, and no new sign-in on disk
        self.task("task-1.txt")
        s = self.make(retry_s=30, max_attempts=3)
        s.ensure_server()
        self.assertEqual(s.step(), seat_mod.UNAUTHORIZED)
        self.assertEqual((self.starts(), len(self.sent("turn/start"))), (2, 2), "restarted and tried once more")
        self.assertEqual(self.posted(), [f"--workspace {self.ws} post --signed-out"])
        self.assertEqual((health(self.ws)["state"], health(self.ws)["runtime"]), ("signed_out", "codex"))
        since = health(self.ws)["since"]
        rec = json.loads(s.record_file("task-1.txt").read_text())
        self.assertEqual(rec["outcome"], seat_mod.UNAUTHORIZED)
        self.assertEqual(list(task_dispatch.pending_candidates(self.tasks, self.results)), ["task-1.txt"])
        self.now[0] += 31
        self.assertEqual(s.step(), seat_mod.UNAUTHORIZED)
        self.assertEqual(len(self.posted()), 1, "once per login on disk")
        self.assertEqual(health(self.ws)["since"], since, "rewritten only when it changes")
        self.sign_in("tok-b")                       # the owner switches from the card
        self.now[0] += 31
        self.assertEqual(s.step(), "done")
        self.assertEqual(len(self.posted()), 1)
        self.assertEqual(health(self.ws)["state"], "ok")

    def test_a_missing_login_shows_one_sign_in_card_and_can_recover(self):
        self.task("task-1.txt")
        self.plan("unauth", "unauth", "unauth", "unauth")
        s = self.make(retry_s=0)
        self.assertFalse((self.codex_home / "auth.json").exists())
        self.assertEqual(s.step(), seat_mod.UNAUTHORIZED)
        self.assertEqual(s.step(), seat_mod.UNAUTHORIZED)
        self.assertEqual(self.posted(), [f"--workspace {self.ws} post --signed-out"])
        self.sign_in("tok-a")
        s.health_auth_check()
        self.assertEqual(health(self.ws)["state"], "ok")
        self.assertEqual(len(self.signed_in_notices()), 1)

    def test_a_login_change_lifts_the_cores_limit_hold_but_a_workers_seat_leaves_it(self):
        import core_limit_hold
        held = {"status": "held", "kind": "usage-limit", "model": None, "evidence_at": 1, "set_at": 1,
                "until": 4102444800, "strikes": 1, "reason": "paused"}
        self.sign_in("tok-a")
        for worker, lifted in (("w1", False), ("", True)):
            with self.subTest(worker=worker):
                core_limit_hold._write_hold(self.ws, held)
                s = self.make()
                s.worker = worker
                s.ensure_server()
                self.sign_in("tok-b" + worker)
                s.fresh_auth()
                self.assertEqual(core_limit_hold.read_hold(self.ws)["status"], "cleared" if lifted else "held")
                s.close_server()

    def test_a_sign_in_while_idle_puts_the_health_back_to_ok_without_a_task(self):
        self.sign_in("tok-a", revoke="tok-a")
        self.task("task-1.txt")
        s = self.make(retry_s=3600, max_attempts=3)
        s.ensure_server()
        self.assertEqual(s.step(), seat_mod.UNAUTHORIZED)
        s.health_auth_check()
        self.assertEqual(health(self.ws)["state"], "signed_out", "the same login: still signed out")
        self.sign_in("tok-b")
        s.health_auth_check()
        self.assertEqual(health(self.ws)["state"], "ok")
        self.assertIn("account health: ok", self.out.getvalue())

    def signed_out_after_the_card(self):
        self.sign_in("tok-a", revoke="tok-a")
        self.task("task-1.txt")
        s = self.make(retry_s=3600, max_attempts=3)
        s.model = "gpt-5.6-terra"
        s.ensure_server()
        self.assertEqual(s.step(), seat_mod.UNAUTHORIZED)
        self.assertIsNotNone(s.told_signed_out, "the card was shown")
        return s

    def signed_in_notices(self):
        return list(self.results.glob("proactive-model-signed-in-*.txt"))

    def test_back_signed_in_the_owner_is_told_once_after_the_card(self):
        """Signed in again after the card: told once, only after Codex's own account read confirms it."""
        s = self.signed_out_after_the_card()
        self.assertEqual(self.signed_in_notices(), [])
        self.sign_in("tok-b")
        s.health_auth_check()
        notice, = self.signed_in_notices()
        self.assertIn("Signed in as", notice.read_text())
        self.assertIn("I'm back on gpt-5.6-terra.", notice.read_text())
        s.health_auth_check()
        s.health("quota")
        s.health("ok")                         # a recovery nobody was shown a card for: no notice
        self.assertEqual(len(self.signed_in_notices()), 1)

    def test_a_deleted_login_after_the_card_is_not_signed_in(self):
        s = self.signed_out_after_the_card()
        (self.codex_home / "auth.json").unlink()
        s.health_auth_check()
        self.assertEqual(self.signed_in_notices(), [])
        self.assertEqual(health(self.ws)["state"], "signed_out")
        self.sign_in("unreadable-login")       # a new file Codex names no account for
        s.health_auth_check()
        self.assertEqual(self.signed_in_notices(), [])
        self.assertEqual(health(self.ws)["state"], "signed_out")

    def test_the_same_login_rewritten_while_limited_is_no_recovery(self):
        import core_limit_hold
        held = {"status": "held", "kind": "usage-limit", "model": None, "evidence_at": 1, "set_at": 1,
                "until": 4102444800, "strikes": 1, "reason": "paused"}
        core_limit_hold._write_hold(self.ws, held)
        self.sign_in("tok-a")
        self.plan("limit")
        self.task("task-1.txt")
        s = self.make(retry_s=3600, max_attempts=3)
        s.model = "gpt-5.6-terra"
        s.ensure_server()
        s.step()
        self.assertEqual(health(self.ws)["state"], "quota")
        s.told_limit = True                    # the owner saw the limit card
        starts = self.starts()
        self.sign_in("tok-a")                  # the same token written again
        s.health_auth_check()
        s.fresh_auth()
        self.assertEqual(self.signed_in_notices(), [])
        self.assertEqual(health(self.ws)["state"], "quota")
        self.assertEqual(core_limit_hold.read_hold(self.ws)["status"], "held", "the hold was lifted")
        self.assertEqual(self.starts(), starts, "the app-server was restarted on the same login")
        self.assertNotIn("login changed on disk", self.out.getvalue())

    def test_back_after_a_limit_card_the_owner_is_told(self):
        s = self.make()
        s.model = "gpt-5.6-terra"
        s.health("quota")
        s.told_limit = True
        s.health("ok")
        notice, = self.results.glob("proactive-model-signed-in-*.txt")
        self.assertIn("I'm back on gpt-5.6-terra.", notice.read_text())
        self.assertFalse(s.told_limit)

    def test_a_card_that_could_not_be_sent_is_tried_again(self):
        self.sign_in("tok-a", revoke="tok-a")
        self.task("task-1.txt")
        s = self.make(retry_s=30, max_attempts=3)
        s.card_argv = [sys.executable, "-c", "import sys; print('no core lane'); sys.exit(1)"]
        s.ensure_server()
        self.assertEqual(s.step(), seat_mod.UNAUTHORIZED)
        self.assertIn("the account card was not sent (no core lane)", self.out.getvalue())
        self.assertIsNone(s.told_signed_out)

    def test_a_worker_seat_restarts_on_the_shared_login_but_sends_no_card(self):
        self.sign_in("tok-a", revoke="tok-a")
        self.task("task-1.txt")
        s = self.make()
        s.worker = "w1"
        s.ensure_server()
        self.assertEqual(s.step(), seat_mod.UNAUTHORIZED)
        self.assertEqual(self.starts(), 2)
        self.assertEqual(self.posted(), [])
        self.assertEqual(health(self.ws), {}, "a worker never writes the core's health")

    def test_what_reads_as_unauthorized(self):
        yes = ({"codexErrorInfo": "unauthorized"}, {"codex_error_info": {"unauthorized": {}}},
               {"message": "Your access token could not be refreshed because your refresh token was revoked. "
                           "Please log out and sign in again."}, "turn/start: {'message': '401 Unauthorized'}")
        no = (None, {"codexErrorInfo": "usageLimitExceeded"}, {"message": "stream disconnected"}, "thread is busy")
        self.assertEqual([seat_mod.unauthorized(e) for e in yes], [True] * len(yes))
        self.assertEqual([seat_mod.unauthorized(e) for e in no], [False] * len(no))

    def test_the_login_print_names_the_file_never_its_contents(self):
        path = self.codex_home / "auth.json"
        self.assertIsNone(seat_mod.auth_print())
        path.write_text("tok-SECRET")
        printed = seat_mod.auth_print()
        self.assertEqual(printed[0], os.path.realpath(path))
        self.assertNotIn("tok-SECRET", repr(printed))
        path.write_text("tok-SECRET-longer")
        self.assertNotEqual(seat_mod.auth_print(), printed)


class CoreModelSwitch(SeatCase):
    """The Codex core's switch (switch-model.sh, src/codex_core_model.py) reaches the core seat's next
    turn; it is core-only, so a worker's seat never reads it (TestWorker below)."""

    def switch(self, model, accepted=False):
        return seat_mod.codex_core_model.save(self.ws / "state", model, applied="next-turn", accepted=accepted)

    def record(self):
        return json.loads((self.ws / "state" / "model-switch.json").read_text())

    def test_a_core_seat_that_is_up_resumes_a_runtime_cards_sign_in_wait_and_a_worker_or_no_wait_does_not(self):
        s = self.launched(1, model="gpt-old")
        calls = []
        record = Path(s.workspace) / "state" / "runtime-card-signin.json"
        s.resume_runtime_wait(lambda argv, **kw: calls.append((argv, kw)))
        self.assertEqual(calls, [], "nothing is waited for")
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{}")
        s.resume_runtime_wait(lambda argv, **kw: calls.append((argv, kw)))
        (argv, kw), = calls
        self.assertEqual(argv[1:], [str(seat_mod.REPO / "skills/runtime-switch/scripts/runtime_card.py"),
                                    "--workspace", str(s.workspace), "resume"])
        self.assertTrue(kw["start_new_session"], "detached: it does not wait on the card")
        s.worker, real = "w1", s.worker
        s.resume_runtime_wait(lambda argv, **kw: calls.append((argv, kw)))
        s.worker = real
        self.assertEqual(len(calls), 1, "a worker is not the core")

        def refuses(argv, **kw):
            raise OSError("no fork")
        s.resume_runtime_wait(refuses)          # logged, never raised
        # The seat calls it once it is ready.
        with mock.patch.object(type(s), "resume_runtime_wait", autospec=True) as resumed:
            self.task("task-1.txt")
            self.assertEqual(s.serve(once=True), 0)
        resumed.assert_called_once()


    def launched(self, rev, **kw):
        """A seat whose launcher read the record at revision `rev` (SUTANDO_CORE_MODEL_REV)."""
        with mock.patch.dict(os.environ, {"SUTANDO_CORE_MODEL_REV": str(rev)}):
            return self.make(**kw)

    def test_a_pick_after_the_launch_is_the_next_turns_model_and_codex_taking_it_settles_it(self):
        self.switch("gpt-old", accepted=True)
        s = self.launched(1, model="gpt-old")
        self.switch("gpt-new")
        self.task("task-1.txt")
        self.assertEqual(s.serve(once=True), 0)
        self.assertEqual([r["params"].get("model") for r in self.sent("turn/start")], ["gpt-new"])
        self.assertEqual(s.model, "gpt-new", "a restarted child resumes the thread on it too")
        self.assertEqual(s.thread_params()["model"], "gpt-new")
        self.assertEqual((self.record()["status"], self.record()["working"]), ("accepted", "gpt-new"))

    def test_a_pick_saved_between_the_launchers_read_and_the_seats_start_is_not_dropped(self):
        self.switch("gpt-launched", accepted=True)  # rev 1: what the launcher read and passed as --model
        self.switch("gpt-during-startup")           # rev 2: saved before the seat came up
        s = self.launched(1, model="gpt-launched")
        self.task("task-1.txt")
        self.assertEqual(s.serve(once=True), 0)
        self.assertEqual([r["params"].get("model") for r in self.sent("turn/start")], ["gpt-during-startup"])

    def test_a_pick_pending_at_launch_starts_on_the_working_model_and_is_tried_on_the_first_turn(self):
        self.switch("gpt-old", accepted=True)
        self.switch("gpt-new")
        s = self.launched(0, model="gpt-old")  # the launcher passes the working model, never the pick
        self.assertEqual(s.thread_params()["model"], "gpt-old")
        self.task("task-1.txt")
        self.assertEqual(s.serve(once=True), 0)
        self.assertEqual([r["params"].get("model") for r in self.sent("thread/start")], ["gpt-old"])
        self.assertEqual([r["params"].get("model") for r in self.sent("turn/start")], ["gpt-new"])
        self.assertEqual((self.record()["status"], s.model), ("accepted", "gpt-new"))

    def test_an_explicit_launch_model_outranks_a_pick_saved_before_it(self):
        self.switch("gpt-new")
        s = self.launched(1, model="gpt-explicit")
        self.task("task-1.txt")
        self.assertEqual(s.serve(once=True), 0)
        self.assertEqual([("model" in r["params"]) for r in self.sent("turn/start")], [False])
        self.assertEqual(self.record()["status"], "pending")

    def test_a_launch_that_already_runs_the_pending_pick_settles_it_on_the_first_completed_turn(self):
        """VM 2026-11-24 06:29: the restart named terra, every turn ran on it, and the record stayed
        'pending, working luna', so the model card named luna while Settings named terra."""
        self.switch("gpt-6-luna", accepted=True)
        self.switch("gpt-6.1-terra")                       # rev 2, pending
        s = self.launched(2, model="gpt-6.1-terra")        # the restart named it
        self.task("task-1.txt")
        self.assertEqual(s.serve(once=True), 0)
        self.assertEqual([("model" in r["params"]) for r in self.sent("turn/start")], [False], "no switch sent")
        rec = self.record()
        self.assertEqual((rec["status"], rec["working"], rec["confirmed"]),
                         ("accepted", "gpt-6.1-terra", "gpt-6.1-terra"))

    def test_while_the_turn_runs_on_the_pick_the_record_says_so_and_it_is_confirmed_only_at_the_turns_end(self):
        # VM 2026-11-23: the turn that applied the pick ran on it, read the record mid-turn, and answered
        # "currently <the old model>; <the pick> pending". The record only changed at the turn's end.
        ccm = seat_mod.codex_core_model
        self.switch("gpt-old", accepted=True)
        s = self.launched(1, model="gpt-old")
        self.switch("gpt-new")
        self.task("task-1.txt")
        mid = []
        settle = s.settle_model

        def settle_seeing(accepted, reason=""):
            mid.append(self.record())           # the record as the running turn could read it
            return settle(accepted, reason)
        s.settle_model = settle_seeing
        self.assertEqual(s.serve(once=True), 0)
        (during,) = mid
        self.assertEqual((during["status"], during["working"], during["running"]["model"]), ("pending", "gpt-old", "gpt-new"))
        self.assertEqual(ccm.in_use(during), "gpt-new", "what the turn runs on, said while it runs")
        self.assertEqual(ccm.working(during), "gpt-old", "not yet what a restart would launch on")
        after = self.record()
        self.assertEqual((after["status"], after["working"], "running" in after), ("accepted", "gpt-new", False))
        self.assertEqual(ccm.in_use(after), "gpt-new")

    def test_a_second_pick_saved_while_the_first_trial_runs_does_not_change_what_that_turn_says_it_is_on(self):
        ccm = seat_mod.codex_core_model
        self.switch("gpt-old", accepted=True)
        s = self.launched(1, model="gpt-old")
        self.switch("gpt-new")
        self.task("task-1.txt")
        mid = []
        settle = s.settle_model

        def settle_seeing(accepted, reason=""):
            self.switch("gpt-newer")            # the owner picks again while the turn is still running
            mid.append(self.record())
            return settle(accepted, reason)
        s.settle_model = settle_seeing
        self.assertEqual(s.serve(once=True), 0)
        (during,) = mid
        self.assertEqual((ccm.in_use(during), during["model"], during["status"], ccm.working(during)),
                         ("gpt-new", "gpt-newer", "pending", "gpt-old"))
        after = self.record()
        self.assertNotIn("running", after, "cleared at that turn's own completion, though the record had moved on")
        self.assertEqual((after["status"], after["model"], after["confirmed"], ccm.in_use(after)),
                         ("pending", "gpt-newer", "gpt-new", "gpt-new"), "Codex did run gpt-new; gpt-newer is next")

    def test_a_pick_saved_before_codex_answers_turn_start_does_not_hide_what_the_turn_runs_on(self):
        ccm = seat_mod.codex_core_model
        self.switch("gpt-old", accepted=True)
        s = self.launched(1, model="gpt-old")
        self.switch("gpt-new")
        self.task("task-1.txt")
        seen = []
        choose = s.turn_model

        def choosing():
            model = choose()                    # the seat has chosen gpt-new and is about to send turn/start
            if model and not seen:
                seen.append(model)
                self.switch("gpt-newer")        # the owner picks again before Codex has answered it
            return model
        s.turn_model = choosing
        mid = []
        settle = s.settle_model

        def settle_seeing(accepted, reason=""):
            mid.append(self.record())
            return settle(accepted, reason)
        s.settle_model = settle_seeing
        self.assertEqual(s.serve(once=True), 0)
        self.assertEqual(seen, ["gpt-new"])
        self.assertEqual([r["params"].get("model") for r in self.sent("turn/start")], ["gpt-new"])
        (during,) = mid
        self.assertEqual((ccm.in_use(during), during["model"], ccm.working(during)), ("gpt-new", "gpt-newer", "gpt-old"))
        self.assertNotIn("running", self.record())
        self.assertEqual(self.record()["confirmed"], "gpt-new")

    def test_a_turn_on_the_pick_that_ends_with_no_verdict_clears_the_mark_and_so_does_a_seats_start(self):
        ccm = seat_mod.codex_core_model
        self.switch("gpt-old", accepted=True)
        s = self.launched(1, model="gpt-old")
        rev = self.switch("gpt-new")["rev"]
        s.trial = ("gpt-new", rev, "gpt-old", True)
        s.mark_running()
        self.assertEqual(ccm.in_use(self.record()), "gpt-new")
        s.abandon_trial()
        self.assertEqual((self.record()["status"], "running" in self.record(), ccm.in_use(self.record())),
                         ("pending", False, "gpt-old"))
        # A seat that died mid-turn left its mark: the next seat's start clears it, whatever its revision.
        self.assertTrue(ccm.started(self.ws / "state", "gpt-new", rev))
        s.unmark_running()
        self.assertNotIn("running", self.record())
        # A pick that was not sent to Codex (already the seat's model) marks nothing; a record that cannot
        # be written is logged and the turn goes on.
        s.trial = ("gpt-new", rev, "gpt-old", False)
        s.mark_running()
        self.assertNotIn("running", self.record())
        s.trial = ("gpt-new", rev, "gpt-old", True)
        with mock.patch.object(ccm, "started", side_effect=OSError("read-only")), \
                mock.patch.object(ccm, "stopped", side_effect=OSError("read-only")):
            s.mark_running()
            s.unmark_running()
        s.trial = None

    def test_a_refusal_that_is_not_about_the_model_leaves_the_pick_pending_and_the_seat_on_its_model(self):
        self.switch("gpt-old", accepted=True)
        self.switch("gpt-new")
        s = self.launched(0, model="gpt-old")
        s.ensure_server()
        (self.fake_dir / "plan.json").write_text(json.dumps(["refuse", "none"]))  # "thread is busy"
        status, detail = s.run_turn("task-1.txt", "hello")
        self.assertEqual(status, "completed", "the turn reran without the pick")
        self.assertEqual([r["params"].get("model") for r in self.sent("turn/start")], ["gpt-new", "gpt-old"])
        self.assertEqual(self.record()["status"], "pending", "a busy thread is no verdict on the model")
        self.assertEqual((s.model, s.thread_params()["model"], s.trial), ("gpt-old", "gpt-old", None),
                         "a restarted child resumes on the working model, not the untried pick")
        self.assertEqual(s.turn_model(), "gpt-new", "the next turn tries it again")
        self.assertEqual(list(self.results.glob("proactive-model-*.txt")), [])

    def catalog(self, *models, default=None):
        rows = [{"id": m, "model": m, "isDefault": m == default} for m in models]
        (self.fake_dir / "models.json").write_text(json.dumps(rows))

    def test_a_pick_not_in_codexs_model_list_is_refused_without_sending_it_and_the_owner_told(self):
        self.catalog("gpt-old", "gpt-new")
        self.switch("gpt-old", accepted=True)
        s = self.launched(1, model="gpt-old")
        self.switch("gpt-bad")
        self.task("task-1.txt")
        self.assertEqual(s.serve(once=True), 0)
        self.assertEqual([("model" in r["params"]) for r in self.sent("turn/start")], [False],
                         "the refused pick is never sent; the task runs on the working model")
        self.assertTrue((self.results / "task-1.txt").exists())
        rec = self.record()
        self.assertEqual((rec["status"], rec["working"]), ("rejected", "gpt-old"))
        self.assertIn("not in the model list", rec["rejected_reason"])
        self.assertEqual(seat_mod.codex_core_model.current(self.ws / "state"), "gpt-old")
        notice, = self.results.glob("proactive-model-refused-*.txt")
        self.assertIn("stays on gpt-old", notice.read_text())

    def test_an_error_answer_to_the_pick_is_no_verdict_the_task_runs_on_the_model_before_it(self):
        # No structured code says "unknown model", so an error answer to turn/start never rejects.
        self.catalog("gpt-old", "gpt-new")
        self.switch("gpt-old", accepted=True)
        s = self.launched(1, model="gpt-old")
        self.switch("gpt-new")
        (self.fake_dir / "refuse-model").write_text("gpt-new")
        self.task("task-1.txt")
        self.assertEqual(s.serve(once=True), 0)
        self.assertEqual([r["params"].get("model") for r in self.sent("turn/start")], ["gpt-new", "gpt-old"])
        self.assertTrue((self.results / "task-1.txt").exists())
        self.assertEqual(self.record()["status"], "pending")
        self.assertEqual(list(self.results.glob("proactive-model-*.txt")), [])

    def test_an_implicit_default_is_named_from_codexs_list_and_restored_explicitly(self):
        self.catalog("gpt-default", "gpt-new", default="gpt-default")
        s = self.launched(0)  # no launch model: Codex's default
        self.switch("gpt-new")
        (self.fake_dir / "refuse-model").write_text("gpt-new")
        self.task("task-1.txt")
        self.assertEqual(s.serve(once=True), 0)
        self.assertEqual([r["params"].get("model") for r in self.sent("turn/start")], ["gpt-new", "gpt-default"])

    def test_a_pick_without_a_verdict_is_tried_at_most_twice_by_one_seat(self):
        self.catalog("gpt-old", "gpt-new")
        s = self.launched(0, model="gpt-old")
        s.ensure_server()
        self.switch("gpt-new")
        for _ in range(seat_mod.TRIAL_ATTEMPTS):
            self.assertEqual(s.turn_model(), "gpt-new")
            s.abandon_trial()
            self.assertEqual(s.turn_model(), "gpt-old", "the thread is moved back off the untried pick")
            s.restore = None  # a turn completed on it
        self.assertEqual(s.turn_model(), "")
        self.assertEqual(self.record()["status"], "unconfirmed")

    def test_a_pick_with_no_verdict_after_the_last_try_is_unconfirmed_not_refused_and_the_owner_told_once(self):
        self.catalog("gpt-old", "gpt-new")
        self.switch("gpt-old", accepted=True)
        s = self.launched(1, model="gpt-old")
        s.ensure_server()
        self.switch("gpt-new")
        for _ in range(seat_mod.TRIAL_ATTEMPTS):
            self.assertEqual(s.turn_model(), "gpt-new")
            s.abandon_trial()
            s.restore = None
        rec = self.record()
        self.assertEqual((rec["status"], rec["accepted"], rec["working"]), ("unconfirmed", False, "gpt-old"))
        self.assertNotIn("rejected_reason", rec, "no verdict is not a refusal")
        self.assertEqual(seat_mod.codex_core_model.pending(self.ws / "state"), ("", 2))
        self.assertEqual(seat_mod.codex_core_model.current(self.ws / "state"), "gpt-old")
        self.assertEqual(seat_mod.codex_core_model.launch(self.ws / "state"), (2, "gpt-old", "gpt-old"))
        self.assertEqual((s.model, s.turn_model()), ("gpt-old", ""))
        s.abandon_trial()  # nothing under trial: no second notice
        notice, = self.results.glob("proactive-model-*.txt")
        self.assertTrue(notice.name.startswith("proactive-model-unconfirmed-"))
        self.assertIn("stays on gpt-old", notice.read_text())

    def test_the_restore_holds_until_codex_completes_a_turn_on_it(self):
        self.catalog("gpt-old", "gpt-new")
        s = self.launched(0, model="gpt-old")
        s.ensure_server()
        self.switch("gpt-new")
        self.assertEqual(s.turn_model(), "gpt-new")
        s.abandon_trial()  # no verdict: the thread may be on gpt-new, so it must go back
        (self.fake_dir / "plan.json").write_text(json.dumps(["fail", "none"]))
        self.assertEqual(s.run_turn("task-1.txt", "hello")[0], "failed")
        self.assertEqual(s.restore, "gpt-old", "a failed turn on the restore keeps the intent")
        self.assertEqual(s.run_turn("task-2.txt", "hello")[0], "completed")
        self.assertIsNone(s.restore)
        self.assertEqual([r["params"].get("model") for r in self.sent("turn/start")], ["gpt-old", "gpt-old"])

    def test_a_pick_codex_ran_while_a_newer_one_was_saved_is_still_the_confirmed_model(self):
        self.catalog("gpt-old", "gpt-new", "gpt-bad-later")
        self.switch("gpt-old", accepted=True)
        s = self.launched(1, model="gpt-old")
        self.switch("gpt-new")                      # rev 2: tried on the next turn
        s.ensure_server()
        self.assertEqual(s.turn_model(), "gpt-new")
        self.switch("gpt-bad-later")                # rev 3 saved while that turn runs
        s.settle_model(True)                         # Codex completed the turn on gpt-new
        rec = self.record()
        self.assertEqual((rec["status"], rec["rev"], rec["confirmed"], rec["confirmed_rev"]),
                         ("pending", 3, "gpt-new", 2), "rev 3 is not settled by rev 2's turn")
        seat_mod.codex_core_model.resolve(self.ws / "state", 3, False, "refused")
        self.assertEqual(seat_mod.codex_core_model.launch(self.ws / "state")[2], "gpt-new",
                         "a restart after the newer pick fails starts on what Codex last ran")

    def test_codex_taking_the_pick_tells_the_owner(self):
        self.catalog("gpt-old", "gpt-new")
        self.switch("gpt-old", accepted=True)
        s = self.launched(1, model="gpt-old")
        self.switch("gpt-new")
        self.task("task-1.txt")
        self.assertEqual(s.serve(once=True), 0)
        notice, = self.results.glob("proactive-model-switched-*.txt")
        self.assertIn("gpt-new", notice.read_text())

    def test_an_incomplete_or_malformed_model_list_is_no_verdict(self):
        for listing in ({"data": [{"id": "gpt-old", "model": "gpt-old"}], "nextCursor": "more"},
                        [{"id": "gpt-old", "model": "gpt-old"}, {"id": "gpt-x"}],
                        [{"id": "gpt-old", "model": "gpt-old"}, "not a row"]):
            with self.subTest(listing=listing):
                (self.fake_dir / "models.json").write_text(json.dumps(listing))
                s = self.launched(0, model="gpt-old")
                s.ensure_server()
                self.switch("gpt-new")
                self.assertEqual(s.turn_model(), "gpt-new", "tried, never refused, on a partial list")
                self.assertEqual(self.record()["status"], "pending")
                s.close_server()

    def test_no_trial_until_the_model_to_go_back_to_is_known(self):
        self.catalog("gpt-new", "gpt-other")  # complete, but names no default
        s = self.launched(0)                 # and the seat has no launch model
        s.ensure_server()
        self.switch("gpt-new")
        self.assertEqual(s.turn_model(), "")
        self.assertIsNone(s.trial)
        self.assertEqual(self.record()["status"], "pending")
        self.assertIn("the model to go back to is unknown", self.out.getvalue())

    def test_an_unreadable_model_list_is_no_verdict(self):
        self.switch("gpt-new")  # no models.json: model/list errors
        s = self.launched(0, model="gpt-old")
        s.ensure_server()
        self.assertEqual(s.turn_model(), "gpt-new")
        self.assertEqual(self.record()["status"], "pending")

    def test_a_claude_cores_record_is_never_a_codex_model(self):
        (self.ws / "state" / "model-switch.json").write_text(json.dumps({"model": "opus", "accepted": True}))
        s = self.launched(0)
        self.assertEqual(s.turn_model(), "")


OWNER = "task: hi\naccess_tier: owner\n"


class ModelNotAvailable(SeatCase):
    """VM 2026-11-24: the core's gpt-6.1-terra is not available under the ChatGPT account it was switched to
    (modelNotFound, raised during compaction); the turn failed twice and the owner saw "thinking" forever."""

    def catalog(self, *models, default=None):
        rows = [{"id": m, "model": m, "isDefault": m == default} for m in models]
        (self.fake_dir / "models.json").write_text(json.dumps(rows))

    def test_what_reads_as_a_missing_model(self):
        yes = ({"codexErrorInfo": "modelNotFound"}, {"codex_error_info": {"modelNotFound": {}}},
               {"message": "The 'gpt-6.1-terra' model is not supported when using Codex with a ChatGPT account."},
               "model not found")
        no = ({"codexErrorInfo": "usageLimitExceeded"}, {"message": "stream disconnected"}, None, "")
        self.assertEqual([seat_mod.model_missing(e) for e in yes], [True] * len(yes))
        self.assertEqual([seat_mod.model_missing(e) for e in no], [False] * len(no))

    def card_calls(self):
        log = self.ws / "model-card-calls.txt"
        return log.read_text().splitlines() if log.exists() else []

    def fake_card(self, rc=0):
        """MODEL_CARD replaced by a script that records its arguments and answers `rc`."""
        script = self.ws / "fake_model_card.py"
        script.write_text("import sys\nopen(%r, 'a').write(' '.join(sys.argv[1:]) + '\\n')\nsys.exit(%d)\n"
                          % (str(self.ws / "model-card-calls.txt"), rc))
        return mock.patch.object(seat_mod, "MODEL_CARD", script)

    def test_it_shows_the_owner_the_model_card_and_leaves_the_task_pending(self):
        """Owner 2026-11-24: a card listing the models this account can use, never a silent switch."""
        self.task("task-1.txt", OWNER)
        self.plan("model-missing")
        s = self.make(retry_s=30, max_attempts=3)
        s.model = "gpt-6.1-terra"
        s.ensure_server()
        with self.fake_card():
            self.assertNotEqual(s.step(), "done")
        self.assertEqual(len(self.sent("turn/start")), 1, "no silent retry on another model")
        self.assertEqual(s.model, "gpt-6.1-terra", "the model is not switched by the seat")
        calls = self.card_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("post --owner-dm --lead gpt-6.1-terra isn't available on this Codex account", calls[0])
        self.assertFalse((self.results / "task-1.txt").exists(), "no result: it stays pending for the pick")
        self.assertEqual(list(self.results.glob("proactive-model-*.txt")), [])

    def test_a_refusal_at_turn_start_with_another_turn_id_gets_the_card_too(self):
        """VM 2026-11-24 05:38: refused straight at turn start (systemError, no compaction), the error naming
        another turn id and the turn's own error empty; no card followed."""
        self.task("task-1.txt", OWNER)
        self.plan("model-missing-at-start")
        s = self.make(retry_s=30, max_attempts=3)
        s.model = "gpt-6-1-terra"
        s.ensure_server()
        with self.fake_card():
            self.assertNotEqual(s.step(), "done")
        calls = self.card_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--lead gpt-6-1-terra isn't available on this Codex account", calls[0])
        self.assertIn("model card posted", self.out.getvalue())

    def test_each_refused_owner_message_gets_the_card_once(self):
        """VM 2026-11-24 06:13-06:16: one card, then every later message refused with no reply."""
        self.task("task-1.txt", OWNER)
        self.plan("model-missing", "model-missing", "model-missing")
        s = self.make(retry_s=0, max_attempts=5)
        s.model = "gpt-6.1-terra"
        s.ensure_server()
        with self.fake_card():
            s.step()
            s.deferred.clear()
            s.step()                               # the same message retried: no second card
            self.assertEqual(len(self.card_calls()), 1)
            (self.results / "task-1.txt").write_text("answered on the pick\n")  # the first one is done
            s.step()
            self.task("task-2.txt", OWNER)         # a new message: its own card
            s.deferred.clear()
            s.step()
        self.assertEqual(len(self.card_calls()), 2)

    def test_a_task_not_the_owners_waits_for_a_model_change_and_then_runs(self):
        """A collaborator's or a cron's task gets no card and is not discarded: it waits, without
        a turn per retry, until the owner picks another model, and then runs on it."""
        self.task("task-cron.txt", "task: run the main loop\naccess_tier: internal\n")
        self.plan("model-missing")
        s = self.make(retry_s=0, max_attempts=5)
        s.model = "gpt-6.1-terra"
        s.ensure_server()
        with self.fake_card():
            self.assertNotEqual(s.step(), "done")
            self.now[0] += 3600
            self.assertIsNone(s.step(), "retried on the same refused model")
        self.assertEqual(self.card_calls(), [], "no card for it")
        self.assertEqual(len(self.sent("turn/start")), 1)
        self.assertFalse((self.results / "task-cron.txt").exists(), "closed with no reply")
        seat_mod.codex_core_model.save(self.ws / "state", "gpt-5.5", applied="next-turn", accepted=False)
        self.assertEqual(s.step(), "done")
        self.assertEqual(self.sent("turn/start")[-1]["params"].get("model"), "gpt-5.5")

    def test_a_card_that_cannot_be_posted_is_said_in_words(self):
        self.task("task-1.txt", OWNER)
        self.plan("model-missing")
        s = self.make(retry_s=30, max_attempts=3)
        s.model = "gpt-6.1-terra"
        s.ensure_server()
        with self.fake_card(rc=1):
            s.step()
        notes = [f.read_text() for f in self.results.glob("proactive-model-refused-*.txt")]
        self.assertEqual(len(notes), 1)
        self.assertIn("isn't available on this Codex account", notes[0])


class QuotaCard(SeatCase):
    """Out of usage, the core seat sends the owner the account card once per limit (core_quota_notice)."""

    def quota(self, posted=True):
        import core_quota_notice
        self.cards = []

        def send(text):
            self.cards.append(text)
            return posted
        return core_quota_notice.QuotaNotice(self.ws, "Codex", send, log=lambda *a: None, now=lambda: self.now[0])

    def test_a_limit_sends_one_card_with_the_reset_and_a_second_limit_sends_none(self):
        self.task("task-1.txt")
        self.plan("limit", "limit")
        s = self.make(quota=self.quota())
        s.ensure_server()
        self.assertEqual(s.step(), "failed")
        self.assertEqual(len(self.cards), 1)
        import core_quota_notice
        self.assertIn("Codex has reached its usage limit (resets %s)" % core_quota_notice.reset_from_epoch(4102444800),
                      self.cards[0])
        self.assertTrue(json.loads((self.ws / "state" / "core-quota-card.json").read_text())["told"])
        self.assertEqual({k: health(self.ws).get(k) for k in ("state", "runtime", "resets_at")},
                         {"state": "quota", "runtime": "codex", "resets_at": 4102444800.0})
        self.now[0] += 3600
        self.assertEqual(s.step(), "failed")
        self.assertEqual(len(self.cards), 1, "a second card for the same limit")
        self.assertIn("quota: quota-card", self.out.getvalue())

    def test_every_owner_message_during_the_limit_gets_the_card_once(self):
        self.task("task-1.txt", "access_tier: owner\ntask: hi\n")
        self.plan("limit", "limit", "limit", "limit")
        s = self.make(quota=self.quota(), retry_s=3600.0, hold_s=3600.0)
        s.ensure_server()
        self.assertEqual(s.step(), "failed")
        self.assertEqual(len(self.cards), 1)
        self.task("task-2.txt", "access_tier: owner\ntask: are you there?\n")
        self.task("task-3.txt", "access_tier: team\ntask: a collaborator\n")
        self.assertEqual([s.step(), s.step()], ["failed", "failed"])
        self.now[0] += 3601
        s.step()                                    # task-1 again
        self.assertEqual(len(self.cards), 2, "task-2 gets its card; task-1 again and a collaborator do not")

    def test_the_errors_own_words_give_the_reset_when_no_window_says(self):
        self.task("task-1.txt")
        self.plan("limit-bare")
        s = self.make(quota=self.quota())
        s.ensure_server()
        s.step()
        self.assertIn("(resets 3:05 PM)", self.cards[0])

    def test_another_error_sends_no_card(self):
        self.task("task-1.txt")
        self.plan("fail-other")
        s = self.make(quota=self.quota())
        s.ensure_server()
        self.assertEqual(s.step(), "failed")
        self.assertEqual(self.cards, [])

    def test_retryable_quota_noise_does_not_override_the_outcome(self):
        self.task("task-1.txt")
        self.plan("limit-retry-other", "limit-retried")
        s = self.make(quota=self.quota(), retry_s=0)
        self.assertEqual(s.step(), "failed")
        self.assertNotEqual(health(self.ws).get("state"), "quota")
        self.assertEqual(s.step(), "done")
        self.assertEqual(health(self.ws)["state"], "ok")
        self.assertEqual(self.cards, [])

    def test_a_completed_turn_ends_the_limit_and_a_later_one_is_a_new_card(self):
        self.task("task-1.txt")
        self.plan("limit", "result", "limit")
        s = self.make(quota=self.quota(), retry_s=0.0)
        s.ensure_server()
        s.step()
        self.assertEqual(health(self.ws)["state"], "quota")
        self.assertEqual(s.step(), "done")
        self.assertFalse(json.loads((self.ws / "state" / "core-quota-card.json").read_text()))
        self.assertEqual(health(self.ws)["state"], "ok")
        self.assertNotIn("resets_at", health(self.ws))
        self.task("task-2.txt")
        s.step()
        self.assertEqual(len(self.cards), 2)

    def test_a_card_that_was_not_posted_is_tried_again_later_not_every_turn(self):
        self.task("task-1.txt")
        self.plan("limit", "limit", "limit")
        s = self.make(quota=self.quota(posted=False), retry_s=0.0, hold_s=0.0)
        s.ensure_server()
        s.step()
        s.step()
        self.assertEqual(len(self.cards), 1, "tried again inside the retry interval")
        self.now[0] += 61
        s.step()
        self.assertEqual(len(self.cards), 2)

    def test_a_seat_without_a_notice_sends_nothing_and_the_core_seat_names_the_account_card(self):
        self.task("task-1.txt")
        self.plan("limit")
        s = self.make()
        s.ensure_server()
        self.assertEqual(s.step(), "failed")
        self.assertFalse((self.ws / "state" / "core-quota-card.json").exists())
        self.assertTrue(seat_mod.ACCOUNT_CARD.is_file())

    def test_a_limit_is_not_a_refused_login_and_a_refused_login_is_not_a_limit(self):
        self.task("task-1.txt")
        self.plan("limit")
        s = self.make(quota=self.quota())
        s.ensure_server()
        self.assertEqual(s.step(), "failed", "out of usage is not `unauthorized`")
        self.assertEqual(len([r for r in self.sent() if "argv" in r]), 1, "no restart for a limit")
        self.assertNotIn("turn refused as unauthorized", self.out.getvalue())
        self.assertEqual(len(self.cards), 1)
        self.task("task-2.txt")
        self.plan("unauth", "unauth")
        s.card_argv = [sys.executable, "-c", "pass"]
        self.assertEqual(s.step(), seat_mod.UNAUTHORIZED)
        self.assertEqual(len(self.cards), 1, "a refused login sends no quota card")

    def test_the_two_kinds_never_collide(self):
        limit = ({"codexErrorInfo": "usageLimitExceeded", "message": "401 Unauthorized"},
                 {"message": "You've hit your usage limit. Log out and sign in again."}, "usage limit reached")
        unauth = ({"codexErrorInfo": "unauthorized", "message": "You've hit your usage limit."},
                  {"message": "refresh token was revoked"})
        for e in limit:
            self.assertEqual((seat_mod.error_kind(e), seat_mod.unauthorized(e)), (seat_mod.USAGE_LIMIT_INFO, False), e)
        for e in unauth:
            self.assertEqual((seat_mod.error_kind(e), seat_mod.usage_limit(e)), (seat_mod.UNAUTHORIZED, False), e)

    def test_usage_limit_and_limit_reset(self):
        self.assertTrue(seat_mod.usage_limit({"codexErrorInfo": "usageLimitExceeded"}))
        self.assertTrue(seat_mod.usage_limit({"codexErrorInfo": {"usage_limit_exceeded": {}}}))
        self.assertTrue(seat_mod.usage_limit({"message": "You've hit your usage limit."}))
        self.assertFalse(seat_mod.usage_limit({"message": "stream disconnected", "codexErrorInfo": "other"}))
        self.assertFalse(seat_mod.usage_limit(None))
        self.assertEqual(seat_mod.limit_reset({"primary": {"usedPercent": 100, "resetsAt": 10},
                                               "secondary": {"usedPercent": 100, "resetsAt": 20}}), 20.0)
        self.assertIsNone(seat_mod.limit_reset({"primary": {"usedPercent": 99, "resetsAt": 10}}))


class CoreQueue(SeatCase):
    def test_the_core_leaves_worker_held_and_claimed_tasks_alone(self):
        self.task("task-mine.txt")
        self.task("task-held.txt")
        self.task("task-claimed.txt")
        held = self.ws / "deliveries" / "w1"
        held.mkdir(parents=True)
        (held / "task-held.txt").touch()
        claims = self.ws / "state" / "task-event-handler-claims"
        claims.mkdir(parents=True)
        (claims / "task-claimed.txt").touch()
        s = self.make()
        self.assertEqual([name for name, _ in s.pending()], ["task-mine.txt"])


class Worker(SeatCase):
    worker = "w1"

    def setUp(self):
        super().setUp()
        self.inbox = self.ws / "deliveries" / "w1"
        self.inbox.mkdir(parents=True)

    def pool_stage(self):
        return [sys.executable, str(POOL / "pool_delivery.py"), "--workspace", str(self.ws),
                "--recipient", "w1", "mark-done"]

    def deliver(self, task_id: str, body: str = "task: do it\n", payload: bool = True):
        if payload:
            self.task(f"{task_id}.txt", body)
        (self.inbox / f"{task_id}.txt").touch()

    def archived(self, task_id: str):
        (self.tasks / "archive").mkdir(exist_ok=True)
        (self.tasks / "archive" / f"{task_id}.txt").write_text("task: do it\n")
        (self.inbox / f"{task_id}.txt").touch()

    def worker_seat(self, stage=None, **kw):
        decl = self.declaration(inbox=self.inbox, payloads=self.tasks, stage=stage or self.pool_stage())
        return self.make(decl, **kw)

    def test_a_core_model_switch_leaves_a_workers_model_alone(self):
        # Core and worker models are separate: the core's switch record is never a worker's model.
        self.deliver("task-1")
        s = self.worker_seat(stage=[sys.executable, str(self.hook)], model="gpt-worker")
        seat_mod.codex_core_model.save(self.ws / "state", "gpt-core", applied="next-turn", accepted=False)
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        self.assertEqual([("model" in r["params"]) for r in self.sent("turn/start")], [False])
        self.assertEqual(s.model, "gpt-worker")
        self.assertEqual([r["params"].get("model") for r in self.sent("thread/start")], ["gpt-worker"])
        launcher = (POOL / "launch-codex-worker-session.sh").read_text(encoding="utf-8")
        for core_only in ("codex_core_model", "model-switch.json", "SUTANDO_CORE_MODEL"):
            self.assertNotIn(core_only, launcher, "a worker launch never reads the core's model")

    def test_a_delivery_is_staged_pending_before_its_turn_and_done_only_after_the_result(self):
        self.deliver("task-1")
        pend = pool_delivery.pending_flag(self.ws, "w1", "task-1")
        done = pool_delivery.done_flag(self.ws, "w1", "task-1")
        os.environ["FAKE_PROBE"] = f"{pend}:{done}:{self.results / 'task-1.txt'}"
        os.environ["SUTANDO_POOL_DELIVERY_SCRIPT"] = str(POOL / "pool_delivery.py")
        s = self.worker_seat()
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        probe = [r["probe"] for r in self.sent() if "probe" in r]
        self.assertEqual(probe, [[True, False, False]], "pending recorded, not done, no result when the turn starts")
        self.assertTrue(pool_delivery.is_done_flag(done))
        self.assertFalse(pend.exists())
        self.assertEqual(self.prompts(), [
            f"Sutando task ready: task-1.txt. Worker w1: read {self.copy_of(s, 'task-1.txt')}, complete only this "
            "delivered task, use AGENTS.md for code conventions but skip core operations, do not create task "
            f"files, and write the result to {self.results}/task-1.txt."])
        self.assertFalse((self.inbox / "task-1.txt").exists(), "the spent sentinel was pruned, as the notifier does")
        self.assertTrue((self.inbox / "archive" / "task-1.txt").exists())
        self.assertIsNone(s.step())

    def test_deliveries_run_in_priority_order_and_only_this_workers(self):
        self.deliver("task-b")
        os.utime(self.tasks / "task-b.txt", (100, 100))
        self.deliver("task-a")
        os.utime(self.tasks / "task-a.txt", (200, 200))
        self.deliver("task-u", "priority: urgent\ntask: now\n")
        self.task("task-core.txt")
        other = self.ws / "deliveries" / "w2"
        other.mkdir()
        self.task("task-other.txt")
        (other / "task-other.txt").touch()
        (self.inbox / "task-acc.accepted").touch()
        self.task("task-acc.txt")
        os.utime(self.tasks / "task-acc.txt", (300, 300))
        s = self.worker_seat(stage=[sys.executable, str(self.hook)])
        self.assertEqual([name for name, _ in s.pending()],
                         ["task-u.txt", "task-b.txt", "task-a.txt", "task-acc.txt"])

    def test_an_answered_delivery_is_marked_done_without_a_turn_and_an_archived_payload_is_withdrawn(self):
        self.deliver("task-answered")
        (self.results / "task-answered.txt").write_text("answered by the core\n")
        self.archived("task-gone")
        pool_delivery.mark_done(self.ws, "w1", "task-gone", published=False)
        s = self.worker_seat()
        s.ensure_server()
        self.assertIsNone(s.step())
        self.assertEqual(self.sent("turn/start"), [])
        self.assertTrue(pool_delivery.is_done_flag(pool_delivery.done_flag(self.ws, "w1", "task-answered")))
        self.assertFalse(pool_delivery.pending_flag(self.ws, "w1", "task-gone").exists())
        self.assertFalse((self.inbox / "task-gone.txt").exists(), "the withdrawn hold retires its sentinel")
        s.pending()
        self.assertEqual(self.stage_log.exists(), False)

    def test_a_crash_mid_turn_neither_loses_nor_repeats_the_delivery(self):
        self.deliver("task-1")
        self.plan("die", "result")
        s = self.worker_seat(stage=[sys.executable, str(self.hook)])
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        self.assertEqual(self.stage_log.read_text().splitlines(),
                         ["--task-id task-1 --stage pending", "--task-id task-1 --stage done"])
        self.assertEqual(len(self.sent("turn/start")), 2)
        self.assertIsNone(s.step())
        s.pending()
        self.assertEqual(len(self.stage_log.read_text().splitlines()), 2, "done is recorded once")

    def test_a_crash_that_stops_the_seat_leaves_the_delivery_pending_for_the_pool(self):
        self.deliver("task-1")
        self.plan("die", "die")
        s = self.worker_seat()
        self.assertEqual(s.serve(once=True), seat_mod.EXIT_FATAL)
        self.assertTrue(pool_delivery.pending_flag(self.ws, "w1", "task-1").exists())
        self.assertFalse(pool_delivery.is_done_flag(pool_delivery.done_flag(self.ws, "w1", "task-1")))
        self.assertTrue((self.inbox / "task-1.txt").exists(), "the sentinel still names the owed task")

    def test_a_turn_without_a_result_stays_pending_never_done(self):
        self.deliver("task-1")
        self.plan("none")
        s = self.worker_seat(stage=[sys.executable, str(self.hook)])
        s.ensure_server()
        self.assertEqual(s.step(), "no-result")
        self.assertEqual(self.stage_log.read_text().splitlines(), ["--task-id task-1 --stage pending"])

    def test_a_refused_pending_stage_defers_the_task_and_runs_nothing(self):
        self.deliver("task-1")
        Path(str(self.stage_log) + ".fail").touch()
        s = self.worker_seat(stage=[sys.executable, str(self.hook)], retry_s=30)
        s.ensure_server()
        self.assertEqual(s.step(), "deferred")
        self.assertEqual(self.sent("turn/start"), [])
        self.assertIsNone(s.step(), "not offered again before the delay")
        os.unlink(str(self.stage_log) + ".fail")
        self.now[0] += 31
        self.assertEqual(s.step(), "done")

    def hooked_pool_stage(self):
        """The recording hook in front of the real pool writer, so a `done` can fail and then land."""
        os.environ["STAGE_REAL"] = str(POOL / "pool_delivery.py")
        os.environ["SUTANDO_POOL_DELIVERY_SCRIPT"] = str(POOL / "pool_delivery.py")
        return [sys.executable, str(self.hook), "--workspace", str(self.ws), "--recipient", "w1", "mark-done"]

    def done_calls(self):
        return [ln for ln in self.stage_log.read_text().splitlines() if ln.endswith("--stage done")]

    def test_a_done_stage_that_failed_once_is_recorded_again_without_running_the_task_again(self):
        self.deliver("task-1")
        Path(str(self.stage_log) + ".fail-done-once").touch()
        s = self.worker_seat(stage=self.hooked_pool_stage(), retry_s=30)
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        self.assertEqual(len(self.done_calls()), 1)
        self.assertTrue((self.inbox / "task-1.txt").exists(), "not recorded yet: the sentinel is still owed")
        self.assertTrue(pool_delivery.pending_flag(self.ws, "w1", "task-1").exists())
        self.assertIsNone(s.step())
        self.assertEqual(len(self.done_calls()), 1, "not hammered: the retry waits its delay")
        self.now[0] += 31
        self.assertIsNone(s.step())
        self.assertEqual(len(self.done_calls()), 2, "the failed stage call is made again")
        self.assertTrue(pool_delivery.is_done_flag(pool_delivery.done_flag(self.ws, "w1", "task-1")))
        self.assertFalse((self.inbox / "task-1.txt").exists(), "the sentinel is retired once done is recorded")
        self.assertEqual(len(self.sent("turn/start")), 1, "the task itself ran once")
        self.now[0] += 1000
        self.assertIsNone(s.step())
        self.assertEqual(len(self.done_calls()), 2, "a recorded settlement is not repeated")
        self.assertIn("will record it again in 30s", self.out.getvalue())

    def test_a_done_stage_that_keeps_failing_is_retried_a_bounded_number_of_times_and_said_plainly(self):
        self.deliver("task-1")
        Path(str(self.stage_log) + ".fail-done").touch()
        s = self.worker_seat(stage=self.hooked_pool_stage(), retry_s=30)
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        for _ in range(12):
            self.now[0] += 31
            self.assertIsNone(s.step())
        self.assertEqual(len(self.done_calls()), seat_mod.SETTLE_ATTEMPTS)
        self.assertEqual(len(self.sent("turn/start")), 1, "the task is never run again")
        log = self.out.getvalue()
        self.assertEqual(log.count("could not record done for it"), 1)
        self.assertIn(f"after {seat_mod.SETTLE_ATTEMPTS} tries", log)
        self.assertTrue((self.inbox / "task-1.txt").exists())

    def test_a_plain_inbox_worker_also_records_a_failed_done_again(self):
        own = self.ws / "own-inbox"
        own.mkdir()
        self.task("task-1.txt", folder=own)
        Path(str(self.stage_log) + ".fail-done-once").touch()
        s = self.make(self.declaration(inbox=own, stage=[sys.executable, str(self.hook)]), retry_s=30)
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        self.now[0] += 31
        self.assertIsNone(s.step())
        self.assertEqual(len(self.done_calls()), 2)
        self.assertEqual(len(self.sent("turn/start")), 1)

    def test_a_delivery_whose_payload_is_only_missing_is_kept_and_run_when_the_payload_lands(self):
        # A sentinel can precede its payload: withdrawing on "missing" orphaned a payload that landed
        # after the scan (with the real pool hook, no turn started).
        os.environ["SUTANDO_POOL_DELIVERY_SCRIPT"] = str(POOL / "pool_delivery.py")
        self.deliver("task-late", payload=False)
        pool_delivery.mark_done(self.ws, "w1", "task-late", published=False)
        s = self.worker_seat(retry_s=30)
        s.ensure_server()
        with mock.patch.object(s, "stage", wraps=s.stage) as stage:
            self.assertIsNone(s.step(), "no payload yet: nothing to run")
            self.assertEqual([c.args for c in stage.call_args_list if c.args[1] == "abandon"], [])
            self.assertTrue((self.inbox / "task-late.txt").exists(), "the sentinel is kept")
            self.assertTrue(pool_delivery.pending_flag(self.ws, "w1", "task-late").exists(), "and its hold")
            self.task("task-late.txt", "task: do it\n")
            self.assertEqual(s.step(), "done")
            self.assertEqual([c.args for c in stage.call_args_list if c.args[1] == "abandon"], [])
        self.assertEqual(len(self.sent("turn/start")), 1, "the delivery was run")
        self.assertTrue(pool_delivery.is_done_flag(pool_delivery.done_flag(self.ws, "w1", "task-late")))

    def test_a_failed_abandon_is_not_made_again_once_the_payload_is_back_from_the_archive(self):
        self.archived("task-back")
        Path(str(self.stage_log) + ".fail").touch()
        s = self.worker_seat(stage=[sys.executable, str(self.hook)], retry_s=30)
        s.ensure_server()
        self.assertIsNone(s.step())
        self.assertIn(("task-back.txt", "abandon"), s.unsettled)
        os.unlink(str(self.stage_log) + ".fail")
        os.replace(self.tasks / "archive" / "task-back.txt", self.tasks / "task-back.txt")
        self.now[0] += 31
        self.assertEqual(s.step(), "done")
        self.assertEqual(self.stage_log.read_text().count("--stage abandon"), 1, "the abandon was not asked again")
        self.assertNotIn(("task-back.txt", "abandon"), s.unsettled)
        self.assertEqual(len(self.sent("turn/start")), 1)

    def test_an_abandon_that_failed_is_recorded_again_while_the_payload_stays_archived(self):
        self.archived("task-gone")
        Path(str(self.stage_log) + ".fail").touch()
        s = self.worker_seat(stage=[sys.executable, str(self.hook)], retry_s=30)
        self.assertIsNone(s.step())
        os.unlink(str(self.stage_log) + ".fail")
        self.assertIsNone(s.step(), "not before the retry is due")
        self.assertEqual(self.stage_log.read_text().count("--stage abandon"), 1)
        self.now[0] += 31
        self.assertIsNone(s.step())
        self.assertEqual(self.stage_log.read_text().count("--stage abandon"), 2)
        self.assertNotIn(("task-gone.txt", "abandon"), s.unsettled)
        self.assertIn(("task-gone.txt", "abandon"), s.staged)

    def test_a_failing_done_or_abandon_hook_is_logged_not_fatal(self):
        self.deliver("task-1")
        self.archived("task-gone")
        s = self.worker_seat(stage=[sys.executable, str(self.hook)])
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        self.assertIn("--task-id task-gone --stage abandon", self.stage_log.read_text())
        Path(str(self.stage_log) + ".fail").touch()
        s.staged.clear()
        s.settle("task-1.txt", "done")
        s.settle("task-1.txt", "done")
        s.settle("task-gone.txt", "abandon")
        self.assertIn("stage done exited 1", self.out.getvalue())
        self.assertIn("stage abandon exited 1", self.out.getvalue())
        with mock.patch.object(seat_mod.subprocess, "run", side_effect=OSError("gone")):
            self.assertIn("did not run", s.stage("task-gone.txt", "abandon"))
            os.unlink(str(self.stage_log) + ".fail")
            os.environ["SUTANDO_POOL_DELIVERY_SCRIPT"] = "/nonexistent/pool_delivery.py"
            s.staged.clear()
            s.unsettled.clear()
            with mock.patch.object(s.host, "stage", return_value=""):
                s.settle("task-1.txt", "done")
        self.assertIn("could not prune spent delivery sentinels", self.out.getvalue())

    def test_a_worker_on_a_plain_inbox_reads_task_files_directly(self):
        own = self.ws / "own-inbox"
        own.mkdir()
        self.task("task-1.txt", folder=own)
        s = self.make(self.declaration(inbox=own))
        self.assertEqual(s.pending(), [("task-1.txt", own / "task-1.txt")])
        self.assertEqual(s.stage("task-1.txt", "abandon"), "", "no stage hook declared")
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        self.assertIn(f"Worker w1: read {self.copy_of(s, 'task-1.txt')}", self.prompts()[0])


class PanelLines(SeatCase):
    def test_each_item_kind_is_one_line(self):
        s = self.make()
        show = s.show
        show("item/started", {"item": {"type": "commandExecution", "command": "ls\n-la " + "x" * 400}})
        show("item/started", {"item": {"type": "mcpToolCall", "server": "s-space", "tool": "room_list"}})
        show("item/started", {"item": {"type": "webSearch", "query": "codex app-server"}})
        show("item/started", {"item": {"type": "fileChange", "changes": [{"path": "a.py"}, {"path": "b.py"}, 3]}})
        show("item/started", {"item": {"type": "dynamicToolCall", "tool": "probe"}})
        show("item/started", {"item": {"type": "contextCompaction"}})
        show("item/started", {"item": {"type": "reasoning"}})
        show("item/completed", {"item": {"type": "commandExecution", "exitCode": 2}})
        show("item/completed", {"item": {"type": "commandExecution", "exitCode": 0}})
        show("item/completed", {"item": {"type": "agentMessage", "text": ""}})
        show("thread/status/changed", {"status": {"type": "systemError"}})
        show("thread/status/changed", {"status": {"type": "idle"}})
        show("account/rateLimits/updated", {"item": "not an object"})
        lines = [ln.split(" ", 1)[1] for ln in self.out.getvalue().splitlines()]
        self.assertEqual(len(lines[0]), 160 + 4)
        self.assertTrue(lines[0].startswith("  $ ls -la x") and lines[0].endswith("…"))
        self.assertEqual(lines[1:], ["  tool s-space.room_list", "  search codex app-server", "  edit a.py, b.py",
                                     "  dynamicToolCall probe", "  contextCompaction", "    exit 2",
                                     "  thread status: systemError"])


class WatcherChild(SeatCase):
    def script(self, body: str) -> Path:
        path = Path(self.tmp.name) / "watch-stub.sh"
        path.write_text("#!/bin/bash\n" + body)
        return path

    def test_an_announcement_wakes_the_seat_and_the_watcher_is_stopped_with_it(self):
        mark = Path(self.tmp.name) / "watcher-args"
        wake = threading.Event()
        w = seat_mod.Watcher(str(self.tasks), wake, script=self.script(
            f'printf "%s\\n" "$*" > "{mark}"\necho "noise"\necho "TASK_FILE: task-1.txt"\nexec sleep 300\n'))
        w.ensure()
        self.assertTrue(wake.wait(10))
        self.assertEqual(mark.read_text().strip(), f"{self.tasks} --role standby --inbox {self.tasks}")
        pid = w.p.pid
        w.ensure()
        self.assertEqual(w.p.pid, pid, "a live watcher is left alone")
        w.stop()
        self.assertIsNotNone(w.p.poll())
        w.stop()

    def test_a_dead_watcher_is_restarted_only_after_the_restart_delay(self):
        w = seat_mod.Watcher(str(self.tasks), threading.Event(), script=self.script("exit 4\n"), restart_s=3600)
        w.stop()
        w.ensure()
        w.p.wait(10)
        first = w.p
        w.ensure()
        self.assertIs(w.p, first, "not restarted in a tight loop")
        w.restart_s = 0
        w.ensure()
        self.assertIsNot(w.p, first)
        w.p.wait(10)
        self.assertIn("watcher exited (4)", self.out.getvalue())

    def test_a_watcher_that_cannot_start_leaves_polling(self):
        w = seat_mod.Watcher(str(self.tasks), threading.Event())
        with mock.patch.object(seat_mod.subprocess, "Popen", side_effect=OSError("no bash")):
            w.ensure()
        self.assertIsNone(w.p)
        self.assertIn("still picked up by polling", self.out.getvalue())

    def test_a_watcher_that_ignores_term_is_killed(self):
        w = seat_mod.Watcher(str(self.tasks), threading.Event(), script=self.script("exec sleep 300\n"))
        w.ensure()
        with mock.patch.object(seat_mod.os, "killpg", side_effect=OSError("gone")), \
                mock.patch.object(w.p, "wait", side_effect=subprocess.TimeoutExpired("w", 5)), \
                mock.patch.object(w.p, "terminate") as term, mock.patch.object(w.p, "kill") as kill:
            w.stop()
        term.assert_called_once()
        kill.assert_called_once()
        w.p.kill()
        w.p.wait()

    def gen_sh(self) -> str:
        """Shell: `gen <file>` prints the generation a watcher would announce for it, or `-`."""
        return (f"gen() {{ '{sys.executable}' -c 'import os, sys; s = os.stat(sys.argv[1]); "
                'print(s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)\' "$1" 2>/dev/null || echo -; }\n')

    def announcing(self, wake):
        """A watcher that announces a task once `<tmp>/announce-<task file>` exists: with the
        generation written in that file, else the one the task file has at that moment."""
        box = Path(self.tmp.name)
        return seat_mod.Watcher(str(self.tasks), wake, script=self.script(
            self.gen_sh() + f'while :; do for f in "{box}"/announce-*; do [ -e "$f" ] || continue; '
            f'n="${{f##*/announce-}}"; g="$(cat "$f")"; [ -n "$g" ] || g="$(gen "{self.tasks}/$n")"; '
            'echo "TASK_GENERATION: $n $g"; echo "TASK_FILE: $n"; rm -f "$f"; done; sleep 0.05; done\n'))

    def announce(self, s, name: str, generation: str = ""):
        mark = Path(self.tmp.name) / f"announce-{name}"
        mark.with_suffix(".tmp").write_text(generation, encoding="utf-8")
        os.replace(mark.with_suffix(".tmp"), mark)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and name not in s.watcher.announced:
            time.sleep(0.02)
        self.assertIn(name, s.watcher.announced)

    def announce_again(self, s, name: str):
        s.watcher.announced.pop(name, None)
        self.announce(s, name)

    def test_the_core_runs_a_task_only_once_the_watcher_announces_it(self):
        # The task file lands a moment before the pool's router gives it to a worker. Read straight
        # from the inbox, the core ran it and so did the worker: two answers to one message.
        s = self.make()
        s.ensure_server()
        s.watcher = self.announcing(s.wake)
        s.watcher.ensure()
        self.addCleanup(s.watcher.stop)
        self.task("task-bound.txt")
        self.task("task-core.txt")
        self.assertIsNone(s.step(), "neither is the core's yet")
        self.assertEqual(self.sent("turn/start"), [])
        # The router gives the first to its worker; the watcher announces only the second.
        held = self.ws / "deliveries" / "w1"
        held.mkdir(parents=True)
        (held / "task-bound.txt").touch()
        self.announce(s, "task-core.txt")
        self.assertEqual(s.step(), "done")
        self.assertIsNone(s.step())
        self.assertEqual(len(self.sent("turn/start")), 1)
        self.assertTrue(task_dispatch.has_ready_result(self.results, "task-core.txt"))
        self.assertFalse(task_dispatch.has_ready_result(self.results, "task-bound.txt"))

    def test_an_announcement_is_for_the_file_that_was_there_not_for_its_name(self):
        s = self.make()
        s.ensure_server()
        s.watcher = self.announcing(s.wake)
        s.watcher.ensure()
        self.addCleanup(s.watcher.stop)
        self.task("task-1.txt")
        self.announce(s, "task-1.txt")
        # Another file takes the name before the core gets to it: nothing has routed that one.
        other = self.task("task-1.txt.new", "task: something else entirely\n", folder=self.ws)
        os.replace(other, self.tasks / "task-1.txt")
        self.assertIsNone(s.step())
        self.assertIsNone(s.step())
        self.assertEqual(self.out.getvalue().count("not the one the watcher read when it routed the task"), 1)
        self.assertEqual(self.sent("turn/start"), [])
        self.announce_again(s, "task-1.txt")
        self.assertEqual(s.step(), "done")

    def test_a_file_replaced_before_the_seat_reads_the_announcement_is_not_taken_for_the_one_announced(self):
        # The handoff itself: the watcher read one file, and another is there when its line arrives.
        s = self.make()
        s.ensure_server()
        s.watcher = self.announcing(s.wake)
        s.watcher.ensure()
        self.addCleanup(s.watcher.stop)
        first = self.task("task-1.txt")
        read_by_the_watcher = " ".join(str(n) for n in seat_mod.generation(first))
        other = self.task("task-1.txt.new", "task: something else entirely\n", folder=self.ws)
        os.replace(other, first)
        self.announce(s, "task-1.txt", read_by_the_watcher)
        self.assertIsNone(s.step())
        self.assertEqual(self.sent("turn/start"), [])
        self.assertIn("not the one the watcher read when it routed the task", self.out.getvalue())
        self.announce_again(s, "task-1.txt")
        self.assertEqual(s.step(), "done")

    def test_an_announcement_without_a_generation_is_held_and_never_filled_in_from_the_inbox(self):
        s = self.make()
        s.ensure_server()
        s.watcher = self.announcing(s.wake)
        s.watcher.ensure()
        self.addCleanup(s.watcher.stop)
        self.task("task-1.txt")
        # The watcher could not read the file's metadata: it says so, and the seat does not guess.
        self.announce(s, "task-1.txt", "-")
        self.assertIsNone(s.step())
        self.assertIsNone(s.step())
        self.assertEqual(self.out.getvalue().count("announced it without the generation it read"), 1)
        self.assertIsNone(s.watcher.announced["task-1.txt"])
        # A line that is not four numbers, and a TASK_FILE line with nothing before it, are the same.
        for line in ("TASK_GENERATION: task-1.txt 1 2 three 4", ""):
            w = seat_mod.Watcher(str(self.tasks), threading.Event(), script=self.script(
                f'echo "{line}"\necho "TASK_FILE: task-1.txt"\nexec sleep 300\n'))
            w.ensure()
            self.addCleanup(w.stop)
            self.assertTrue(w.wake.wait(10))
            self.assertEqual(w.announced, {"task-1.txt": None}, line)
        self.assertEqual(self.sent("turn/start"), [])
        self.announce_again(s, "task-1.txt")
        self.assertEqual(s.step(), "done")

    def test_the_real_watcher_hands_the_seat_the_generation_it_read_and_the_seat_runs_that_file(self):
        # Producer and consumer together: src/watch-tasks-stream.sh itself, started as the seat starts it.
        s = self.make()
        s.ensure_server()
        self.task("task-1.txt")
        for folder in ("results", "state"):
            (self.ws / folder).mkdir(exist_ok=True)
        env = {"SUTANDO_WORKSPACE_DIR": str(self.ws), "SUTANDO_RESULTS_DIR": str(self.results)}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("SUTANDO_ANNOUNCE_GENERATION", None)
            s.watcher = seat_mod.Watcher(str(self.tasks), s.wake)
            s.watcher.ensure()
        self.addCleanup(s.watcher.stop)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and "task-1.txt" not in s.watcher.announced:
            time.sleep(0.05)
        self.assertEqual(s.watcher.announced.get("task-1.txt"), seat_mod.generation(self.tasks / "task-1.txt"))
        self.assertEqual(s.step(), "done")
        self.assertEqual(len(self.sent("turn/start")), 1)

    def test_a_file_replaced_after_the_queue_chose_it_is_not_dispatched_even_with_the_server_running(self):
        # Selection to dispatch, warm: the admitted generation is carried to the dispatch and read last.
        s = self.make()
        s.ensure_server()
        s.watcher = self.announcing(s.wake)
        s.watcher.ensure()
        self.addCleanup(s.watcher.stop)
        first = self.task("task-1.txt")
        self.announce(s, "task-1.txt")
        chosen = s.pending()
        self.assertEqual([name for name, _ in chosen], ["task-1.txt"])
        other = self.task("task-1.txt.new", "task: something else entirely\n", folder=self.ws)
        os.replace(other, first)
        self.assertEqual(s.run_task(*chosen[0]), seat_mod.WITHDRAWN)
        self.assertEqual(self.sent("turn/start"), [])
        self.assertIn("its file is not the one that was admitted; not started", self.out.getvalue())

    def test_an_announcement_naming_a_path_is_kept_by_its_file_name(self):
        s = self.make()
        s.watcher = self.announcing(s.wake)
        s.watcher.ensure()
        self.addCleanup(s.watcher.stop)
        self.task("task-1.txt")
        (Path(self.tmp.name) / "announce-task-1.txt").touch()
        self.assertTrue(s.wake.wait(10))
        self.assertEqual([name for name, _ in s.pending()], ["task-1.txt"])
        # A task that has left the inbox is forgotten with it.
        (self.tasks / "task-1.txt").unlink()
        self.assertEqual(s.pending(), [])
        self.assertEqual(s.watcher.announced, {})
        w = seat_mod.Watcher(str(self.tasks), threading.Event(), script=self.script(
            'echo "TASK_FILE: /some/where/task-9.txt"\nexec sleep 300\n'))
        w.ensure()
        self.addCleanup(w.stop)
        self.assertTrue(w.wake.wait(10))
        self.assertEqual(set(w.announced), {"task-9.txt"})

    def test_with_no_watcher_a_declared_handler_holds_the_cores_tasks_and_none_declared_does_not(self):
        s = self.make()
        s.ensure_server()
        self.task("task-1.txt")
        declared = self.ws / "state" / "task-event-handler.json"
        declared.parent.mkdir(parents=True, exist_ok=True)
        declared.write_text('{"handler": "/abs/pool_route_handler.py"}')
        self.assertIsNone(s.step())
        self.assertIsNone(s.step())
        self.assertEqual(self.out.getvalue().count("pending task(s) held"), 1, "said once")
        self.assertEqual(self.sent("turn/start"), [])
        # A watcher that has exited is no watcher.
        s.watcher = seat_mod.Watcher(str(self.tasks), s.wake, script=self.script("exit 0\n"))
        s.watcher.ensure()
        s.watcher.p.wait(10)
        self.assertIsNone(s.step())
        declared.unlink()
        self.assertEqual(s.step(), "done", "nothing to route: every pending task is the core's")

    def test_with_no_watcher_a_shutdown_in_progress_holds_the_cores_tasks(self):
        s = self.make()
        s.ensure_server()
        self.task("task-1.txt")
        mark = self.ws / "state" / "shutdown.sentinel"
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text("{}")
        self.assertIsNone(s.step())
        self.assertIn("pending task(s) held: a shutdown is in progress", self.out.getvalue())
        self.assertEqual(self.sent("turn/start"), [])
        mark.unlink()
        self.assertEqual(s.step(), "done")

    def test_with_no_watcher_a_declaration_that_cannot_be_read_or_a_pinned_handler_holds_them(self):
        s = self.make()
        s.ensure_server()
        self.task("task-1.txt")
        declared = self.ws / "state" / "task-event-handler.json"
        declared.parent.mkdir(parents=True, exist_ok=True)
        declared.write_text("{ not json")
        self.assertIsNone(s.step(), "unparseable is still a declaration")
        declared.unlink()
        with mock.patch.object(seat_mod.os, "lstat", side_effect=PermissionError("denied")):
            self.assertIsNone(s.step(), "a declaration that cannot be examined is not ruled out")
        with mock.patch.dict(os.environ, {"SUTANDO_TASK_EVENT_HANDLER": "/abs/pinned-handler"}):
            self.assertIsNone(s.step(), "a pinned handler is a handler")
        self.assertEqual(self.sent("turn/start"), [])
        self.assertEqual(s.step(), "done")

    def test_serve_keeps_the_watcher_running_and_picks_up_a_task_it_announces(self):
        s = self.make(poll_s=30)
        # The task arrives once the seat is idle and is announced once it is there: fixed delays
        # raced a slow app-server start.
        there = self.ws / "task-late.there"
        stub = self.script(self.gen_sh() + f'while [ ! -e "{there}" ]; do sleep 0.05; done\n'
                           f'echo "TASK_GENERATION: task-late.txt $(gen "{self.tasks}/task-late.txt")"\n'
                           'echo "TASK_FILE: task-late.txt"\nexec sleep 300\n')
        w = seat_mod.Watcher(str(self.tasks), s.wake, script=stub)

        def arrive():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and "idle: waiting for tasks" not in self.out.getvalue():
                time.sleep(0.02)
            self.task("task-late.txt")
            there.touch()

        threading.Thread(target=arrive).start()
        stopper = threading.Thread(target=lambda: (self.wait_for_result("task-late.txt"), s.request_stop()))
        stopper.start()
        self.assertEqual(s.serve(watcher=w), 0)
        stopper.join(20)
        self.assertTrue(task_dispatch.has_ready_result(self.results, "task-late.txt"))
        self.assertIn("idle: waiting for tasks", self.out.getvalue())
        self.assertIsNotNone(w.p.poll(), "the watcher ends with the seat")

    def wait_for_result(self, name):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not task_dispatch.has_ready_result(self.results, name):
            time.sleep(0.05)


class CommandLine(SeatCase):
    def test_run_once_from_the_command_line(self):
        self.task("task-1.txt")
        decl = self.declaration()
        log_file = self.ws / "logs" / "codex-seat.log"
        with mock.patch.dict(os.environ, {"SUTANDO_SEAT_BACKOFF_S": "0", "SUTANDO_SEAT_RETRY_S": "nonsense",
                                          "SUTANDO_SEAT_HOLD_S": "-5"}), \
                mock.patch.object(seat_mod, "workstream_context", lambda name: ""), \
                mock.patch.object(seat_mod.signal, "signal") as handlers:
            rc = seat_mod.main(["run", "--host", str(decl), "--workspace", str(self.ws), "--seat", "sutando-core",
                                "--codex", str(self.fake), "--model", "gpt-test", "-c", 'web_search="live"',
                                "--once", "--log", str(log_file)])
        self.addCleanup(setattr, seat_mod, "LOG_FILE", "")
        self.assertEqual(rc, 0)
        self.assertEqual(handlers.call_count, 3)
        self.assertTrue(task_dispatch.has_ready_result(self.results, "task-1.txt"))
        self.assertEqual(self.sent()[0]["argv"], ["-c", 'web_search="live"', "app-server", "--listen", "stdio://"])
        self.assertEqual(self.sent("thread/start")[0]["params"]["model"], "gpt-test")
        self.assertIn("seat sutando-core (core) on codex app-server", log_file.read_text())
        seat_mod.LOG_FILE = str(self.ws / "state")
        seat_mod.log("a log file that cannot be written does not stop the seat")

    def test_run_with_the_watcher_flag_starts_one(self):
        decl = self.declaration()
        made = []

        class StubWatcher:
            def __init__(self, inbox, wake):
                made.append(inbox)

            def ensure(self):
                made.append("ensure")

            def alive(self):
                return False

            def stop(self):
                made.append("stop")

        with mock.patch.object(seat_mod, "Watcher", StubWatcher), mock.patch.object(seat_mod.signal, "signal"), \
                mock.patch.object(seat_mod, "resolve_workspace", return_value=self.ws) as resolved:
            rc = seat_mod.main(["run", "--host", str(decl), "--seat", "s",
                                "--codex", str(self.fake), "--watcher", "--once"])
        self.assertEqual(rc, 0)
        resolved.assert_called_once_with()
        self.assertEqual(made, [str(self.tasks), "ensure", "stop"])

    def test_a_bad_declaration_is_refused_before_any_app_server(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text(json.dumps({"schema": "something-else/1"}))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = seat_mod.main(["run", "--host", str(bad), "--workspace", str(self.ws), "--seat", "s"])
            same_dirs = seat_mod.main(["declare", "--out", str(bad), "--inbox", str(self.tasks),
                                       "--results", str(self.tasks), "--state", str(self.ws / "state" / "x")])
        self.assertEqual((rc, same_dirs), (seat_mod.EXIT_USAGE, seat_mod.EXIT_USAGE))
        self.assertIn("codex-seat: ", err.getvalue())
        self.assertEqual(self.sent(), [])

    def test_the_declaration_is_the_supervised_host_schema(self):
        decl = json.loads(self.declaration(payloads=self.tasks, stage=["/abs/py", "hook.py", "--workspace", "w",
                                                                    "mark-done"]).read_text())
        self.assertEqual(decl["schema"], "ag2-sparrow.supervised-host/1")
        self.assertEqual(decl["publish"], "result_publish")
        self.assertEqual(decl["stage_command"], ["/abs/py", "hook.py", "--workspace", "w", "mark-done"])
        self.assertEqual(set(decl), {"schema", "inbox", "results", "state", "publish", "payloads", "cwd",
                                     "stage_command"})


class HostDeclaration(unittest.TestCase):
    """`supervised_host.py` beside the seat: the declaration it refuses and the stage hook it runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        for d in ("inbox", "results", "payloads"):
            (self.root / d).mkdir()
        self.sh = seat_mod.supervised_host

    def decl(self, **changes) -> dict:
        d = {"schema": self.sh.SCHEMA, "inbox": str(self.root / "inbox"), "results": str(self.root / "results"),
             "state": str(self.root / "state"), "publish": "result_publish"}
        d.update(changes)
        return {k: v for k, v in d.items() if v is not ...}

    def load(self, body):
        path = self.root / "host.json"
        path.write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode())
        return self.sh.load(str(path))

    def test_the_seat_reads_the_copy_beside_it_and_not_the_ag2_sparrow_package(self):
        self.assertEqual(Path(self.sh.__file__).resolve(), SEAT_PY.with_name("supervised_host.py"))
        self.assertNotIn("ag2_sparrow", SEAT_PY.read_text())

    def test_a_valid_declaration_loads_with_its_optional_keys_and_makes_the_state_dir(self):
        host = self.load(self.decl(payloads=str(self.root / "payloads"), cwd=str(self.root),
                                   stage_command=["/abs/py", "hook"]))
        self.assertEqual((host.inbox, host.results, host.state, host.publish_contract),
                         (str(self.root / "inbox"), str(self.root / "results"), str(self.root / "state"),
                          "result_publish"))
        self.assertEqual((host.payloads, host.cwd, host.stage_command),
                         (str(self.root / "payloads"), str(self.root), ["/abs/py", "hook"]))
        self.assertTrue((self.root / "state").is_dir())
        self.assertEqual(host.task_path("task-1.txt"), str(self.root / "payloads" / "task-1.txt"))
        plain = self.load(self.decl())
        self.assertEqual((plain.payloads, plain.cwd, plain.stage_command), (None, None, None))
        self.assertEqual(plain.task_path("task-1.txt"), str(self.root / "inbox" / "task-1.txt"))

    def test_each_unusable_declaration_is_refused_with_one_line_naming_what_is_wrong(self):
        cases = [
            (b"{not json", "is not JSON"),
            (b"\xff\xfe", "is not JSON"),
            (b" " * (self.sh.DECLARATION_MAX + 1), "is larger than"),
            ([1], "must be a JSON object"),
            (self.decl(extra=1), "unknown key in the host declaration: extra"),
            (self.decl(results=...), "the host declaration has no results"),
            (self.decl(schema="something-else/1"), "is not ag2-sparrow.supervised-host/1"),
            (self.decl(publish="copy"), "publish 'copy' is not one of result_publish"),
            (self.decl(inbox="relative/inbox"), "inbox must be an absolute path"),
            (self.decl(results=str(self.root / "absent")), "is not a directory"),
            (self.decl(state=7), "state must be an absolute path"),
            (self.decl(payloads="relative"), "payloads must be an absolute path"),
            (self.decl(cwd=str(self.root / "absent")), "is not a directory"),
            (self.decl(results=str(self.root / "inbox")), "must be three different directories"),
            (self.decl(stage_command=["relative-py", "hook"]), "stage_command must be a list"),
            (self.decl(stage_command=[]), "stage_command must be a list"),
            (self.decl(stage_command="/abs/py hook"), "stage_command must be a list"),
        ]
        for body, why in cases:
            with self.subTest(why=why, body=str(body)[:60]):
                with self.assertRaises(self.sh.DeclarationError) as raised:
                    self.load(body)
                self.assertIn(why, str(raised.exception))
        with self.assertRaises(self.sh.DeclarationError) as raised:
            self.sh.load(str(self.root / "no-such-file.json"))
        self.assertIn("cannot read the host declaration", str(raised.exception))

    def test_the_stage_hook_is_run_with_the_task_and_stage_and_its_failures_are_said(self):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw["timeout"]))
            return subprocess.CompletedProcess(argv, self.rc, "", self.err)

        host = self.load(self.decl(stage_command=["/abs/py", "hook"]))
        self.rc, self.err = 0, ""
        self.assertEqual(host.stage("task-1", "pending", run=run), "")
        self.assertEqual(calls, [(["/abs/py", "hook", "--task-id", "task-1", "--stage", "pending"],
                                  self.sh.STAGE_TIMEOUT_S)])
        self.rc, self.err = 3, "first line\nlast line\n"
        self.assertEqual(host.stage("task-1", "done", run=run), "stage done exited 3: last line")
        self.rc, self.err = 4, ""
        self.assertEqual(host.stage("task-1", "done", run=run), "stage done exited 4")

        def absent(argv, **kw):
            raise FileNotFoundError(argv[0])

        self.assertEqual(host.stage("task-1", "done", run=absent), "stage done did not run (FileNotFoundError)")
        with self.assertRaises(ValueError):
            host.stage("task-1", "abandon", run=run)
        self.assertEqual(len(calls), 3)

    def test_a_host_with_no_stage_hook_records_nothing_and_reports_no_problem(self):
        def never(argv, **kw):
            raise AssertionError("no hook is declared")

        self.assertEqual(self.load(self.decl()).stage("task-1", "done", run=never), "")



class AdvisoryFailures(SeatCase):
    """The account-health, limit-hold, card and quota side paths never stop the seat when they fail."""

    def test_limit_reset_of_no_snapshot_is_none(self):
        self.assertIsNone(seat_mod.limit_reset(None))

    def test_health_account_read_and_hold_failures_are_logged(self):
        import codex_account
        import core_limit_hold
        s = self.make()
        with mock.patch.object(seat_mod.core_account_health, "mark", side_effect=RuntimeError):
            s.health("quota")
        self.assertIsNone(s.health_bad, "a record not kept is no bad state")
        with mock.patch.object(codex_account, "read", side_effect=RuntimeError):
            self.assertFalse(s.signed_in_confirmed())
        with mock.patch.object(core_limit_hold, "read_hold", side_effect=RuntimeError):
            s.release_limit_hold()
        out = self.out.getvalue()
        for line in ("account health not kept (RuntimeError)", "account read failed (RuntimeError)",
                     "limit hold not lifted (RuntimeError)"):
            self.assertIn(line, out)

    def test_a_card_command_that_cannot_run_is_not_sent(self):
        (self.codex_home / "auth.json").write_text("tok-a")
        s = self.make()
        s.card_argv = [str(self.fake_dir / "no-such-card")]
        s.signed_out("task-1.txt")
        self.assertIsNone(s.told_signed_out)
        self.assertIn("the account card was not sent", self.out.getvalue())

    def test_a_refused_model_on_a_worker_seat_is_not_its_concern(self):
        s = self.make()
        s.worker = "w1"
        s.model_unavailable("task-1.txt")
        self.assertEqual(s.model_wait, {})

    def test_a_model_card_that_cannot_run_is_said_in_words(self):
        s = self.make()
        owner = self.task("task-3.txt", "access_tier: owner\ntask: do it\n")
        with mock.patch.object(seat_mod.subprocess, "run", side_effect=OSError("no python")), \
                mock.patch.object(s, "notify") as notify:
            s.model_unavailable("task-3.txt", owner)
        self.assertIn("model card failed", self.out.getvalue())
        notify.assert_called_once()

    def test_a_quota_notice_that_raises_is_logged(self):
        s = self.make()
        s.quota = mock.Mock(hit=mock.Mock(side_effect=RuntimeError))
        s.turn_limit = {"message": "You've hit your usage limit."}
        s.quota_step("failed", "task-4.txt")
        self.assertIn("quota: quota-failed (RuntimeError)", self.out.getvalue())

if __name__ == "__main__":
    unittest.main()
