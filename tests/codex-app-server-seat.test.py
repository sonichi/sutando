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


rec({"argv": sys.argv[1:], "pid": os.getpid()})
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
        step = plan()
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
    elif method == "turn/interrupt":
        out({"id": rid, "result": {}})
        if state["turn"] and not os.path.exists(os.path.join(D, "deaf")):
            finish("interrupted")
    else:
        out({"id": rid, "result": {}})
'''

STAGE_HOOK = r'''
import os, sys
log = os.environ["STAGE_LOG"]
with open(log, "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
sys.exit(1 if os.path.exists(log + ".fail") else 0)
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
        env = mock.patch.dict(os.environ, {"FAKE_DIR": str(self.fake_dir), "STAGE_LOG": str(self.stage_log)})
        env.start()
        self.addCleanup(env.stop)
        for name in ("SUTANDO_POOL_DELIVERY_SCRIPT", "FAKE_PROBE"):
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
    def test_handshake_then_one_durable_thread_started_with_the_tui_launch_settings(self):
        s = self.make(model="gpt-test", instructions="You are a worker.")
        s.ensure_server()
        argv = self.sent()[0]["argv"]
        self.assertEqual(argv, ["app-server", "-c", 'web_search="live"', "-c",
                                "check_for_update_on_startup=false", "--listen", "stdio://"])
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
        self.assertEqual(self.prompts(), [self.notifier_prompt("task-1.txt", self.tasks / "task-1.txt", self.results)])
        turn = self.sent("turn/start")[0]["params"]
        self.assertEqual(turn["cwd"], str(self.ws))

    def test_prior_workstream_context_is_handed_over_as_the_notifier_does(self):
        self.task("task-1.txt")
        s = self.make(context=lambda name: f"earlier work for {name}\n")
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        ctx = Path(s.host.state) / "context" / "task-1.txt"
        self.assertEqual(self.prompts(), [self.notifier_prompt("task-1.txt", self.tasks / "task-1.txt",
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
        self.assertIn("interrupted by Stop", self.out.getvalue())
        self.assertEqual(s.step(), "done", "the next task runs")
        self.assertIn("task-2.txt", self.prompts()[1])
        self.now[0] += 10 ** 6
        self.assertIsNone(s.step(), "a stopped task is never started again by the seat")
        self.assertEqual(len(self.sent("turn/start")), 2)

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

    def worker_seat(self, stage=None, **kw):
        decl = self.declaration(inbox=self.inbox, payloads=self.tasks, stage=stage or self.pool_stage())
        return self.make(decl, **kw)

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
            f"Sutando task ready: task-1.txt. Worker w1: read {self.tasks / 'task-1.txt'}, complete only this "
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

    def test_an_answered_delivery_is_marked_done_without_a_turn_and_a_gone_payload_is_withdrawn(self):
        self.deliver("task-answered")
        (self.results / "task-answered.txt").write_text("answered by the core\n")
        self.deliver("task-gone", payload=False)
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

    def test_a_failing_done_or_abandon_hook_is_logged_not_fatal(self):
        self.deliver("task-1")
        self.deliver("task-gone", payload=False)
        s = self.worker_seat(stage=[sys.executable, str(self.hook)])
        s.ensure_server()
        self.assertEqual(s.step(), "done")
        self.assertIn("--task-id task-gone --stage abandon", self.stage_log.read_text())
        Path(str(self.stage_log) + ".fail").touch()
        s.staged.clear()
        s.settle("task-1.txt", "done")
        s.settle("task-gone.txt", "abandon")
        self.assertIn("stage done exited 1", self.out.getvalue())
        self.assertIn("stage abandon exited 1", self.out.getvalue())
        with mock.patch.object(seat_mod.subprocess, "run", side_effect=OSError("gone")):
            self.assertIn("did not run", s.stage("task-gone.txt", "abandon"))
            os.unlink(str(self.stage_log) + ".fail")
            os.environ["SUTANDO_POOL_DELIVERY_SCRIPT"] = "/nonexistent/pool_delivery.py"
            s.staged.clear()
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
        self.assertIn(f"Worker w1: read {own / 'task-1.txt'}", self.prompts()[0])


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

    def test_serve_keeps_the_watcher_running_and_picks_up_a_task_it_announces(self):
        s = self.make(poll_s=30)
        stub = self.script('sleep 0.5\necho "TASK_FILE: task-late.txt"\nexec sleep 300\n')
        w = seat_mod.Watcher(str(self.tasks), s.wake, script=stub)

        def arrive():
            self.task("task-late.txt")

        threading.Timer(0.2, arrive).start()
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
        self.assertEqual(self.sent()[0]["argv"], ["app-server", "-c", 'web_search="live"', "--listen", "stdio://"])
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


if __name__ == "__main__":
    unittest.main()
