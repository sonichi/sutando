#!/usr/bin/env python3
"""Request expiry and recovery through the real CLI, daemon and Unix socket."""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env

ROOT = Path(__file__).resolve().parents[1]
socket_setting = os.environ.get("SUTANDO_RUNTIME_SOCKET")
ENVIRONMENT = Path(socket_setting).parent / "environment.json" if socket_setting else None
if ENVIRONMENT is not None and not ENVIRONMENT.is_file():
    ENVIRONMENT = None
isolate_os_environ()
if ENVIRONMENT is not None:
    os.environ.update(json.loads(ENVIRONMENT.read_text()))

sys.path.insert(0, str(ROOT / "src" / "runtime-api"))
from ha_adapter import HumanActionAdapter, ha_action_id  # noqa: E402
from request_store import RequestStore  # noqa: E402

from hitl.projector import project  # noqa: E402
from hitl.schema import (  # noqa: E402
    ActionReply, HumanRequirement, StaleRequirementError, WIRE_FIELD,
)


class RequestExpiryJourneyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="expiry-", dir=ENVIRONMENT.parent if ENVIRONMENT else None)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.ws = self.base / "workspace"
        self.env = isolated_child_env()
        if ENVIRONMENT is not None:
            self.env.update(json.loads(ENVIRONMENT.read_text()))
        self.env.update({
            "PYTHONDONTWRITEBYTECODE": "1", "SUTANDO_TEST_MODE": "1",
            "SUTANDO_TELEMETRY": "0", "SUTANDO_PACKAGED": "", "SUTANDO_APP_SUPPORT": "",
            "SUTANDO_WORKSPACE": str(self.ws), "SUTANDO_WORKSPACE_DIR": str(self.ws),
            "SUTANDO_DEFAULT_WORKSPACE": str(self.ws),
            "SUTANDO_TASKS_DIR": str(self.ws / "tasks"),
            "SUTANDO_RESULTS_DIR": str(self.ws / "results"),
            "SUTANDO_RUNTIME_STATE": str(self.ws / "state"),
            "SUTANDO_RUNTIME_DB": str(self.ws / "state/runtime.sqlite"),
            "SUTANDO_HA_DIR": str(self.ws / "state/ha"),
            "SUTANDO_RUNTIME_SOCKET": str(self.base / "api.sock"),
            "SUTANDO_RUN_DIR": str(self.base / "run"),
            "SUTANDO_INSTANCE_REGISTRY": str(self.base / "registry"),
            "SUTANDO_RUNTIME_RESOLVE_POLL": "0.05",
            "SUTANDO_AGENT_ID": "expiry-test", "SUTANDO_HOST_LABEL": "expiry-test",
            "SUTANDO_INSTANCE_ID": "",
        })
        diagnostic_root = ENVIRONMENT.parent if ENVIRONMENT else self.base
        self.diagnostics = diagnostic_root / "diagnostics" / self.base.name
        self.diagnostics.mkdir(parents=True)
        self.ha = HumanActionAdapter(self.env["SUTANDO_HA_DIR"])
        self.manager = self.ha.manager
        self.sent = []
        self.daemon = None
        self.starts = 0
        self.addCleanup(self.stop_daemon)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, env=self.env,
                                capture_output=True, text=True, check=True, timeout=5)
        self.record(event="environment", supplied=str(ENVIRONMENT),
                    base_commit=commit.stdout.strip(), checkout=str(ROOT),
                    socket=self.env["SUTANDO_RUNTIME_SOCKET"], workspace=str(self.ws))
        self.start_daemon()

    def record(self, **fields):
        with (self.diagnostics / "observations.jsonl").open("a") as output:
            output.write(json.dumps({"ts": time.time(), **fields}) + "\n")

    def run_cli(self, *args, timeout=5):
        argv = [str(ROOT / "bin/sutando"), *args]
        result = subprocess.run(argv, cwd=ROOT, env=self.env, capture_output=True,
                                text=True, timeout=timeout)
        self.record(event="cli", argv=argv, returncode=result.returncode,
                    stdout=result.stdout[-8192:], stderr=result.stderr[-8192:])
        return result

    def cli(self, *args):
        result = self.run_cli(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def start_daemon(self):
        self.starts += 1
        self.log_path = self.diagnostics / f"daemon-{self.starts}.log"
        argv = [sys.executable, str(ROOT / "src/runtime-api/server.py")]
        with self.log_path.open("w") as log:
            self.daemon = subprocess.Popen(argv, cwd=ROOT, env=self.env,
                                           stdout=log, stderr=subprocess.STDOUT)
        self.record(event="start", argv=argv, pid=self.daemon.pid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.daemon.poll() is not None:
                self.fail(f"daemon exited: {self.log_path.read_text()}")
            if Path(self.env["SUTANDO_RUNTIME_SOCKET"]).exists():
                result = self.run_cli("runtime", "health", timeout=2)
                if result.returncode == 0:
                    self.record(event="ready", pid=self.daemon.pid,
                                response=json.loads(result.stdout))
                    return
            time.sleep(0.05)
        self.fail(f"readiness timed out: {self.log_path.read_text()}")

    def stop_daemon(self):
        if self.daemon is None:
            return
        daemon, self.daemon = self.daemon, None
        if daemon.poll() is None:
            daemon.terminate()
        forced = False
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            forced = True
            daemon.kill()
            daemon.wait(timeout=5)
        self.record(event="reaped", pid=daemon.pid, returncode=daemon.returncode,
                    forced_kill=forced)
        self.log_path.write_bytes(self.log_path.read_bytes()[-16384:])
        self.assertEqual(daemon.returncode, -signal.SIGKILL if forced else 0,
                         self.log_path.read_text())

    def row(self, rid):
        # Read-only SQL cannot trigger RequestStore.get's lazy expiry.
        with closing(sqlite3.connect(
                f"file:{self.env['SUTANDO_RUNTIME_DB']}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM runtime_requests WHERE request_id = ?",
                                   (rid,)).fetchone())

    def mirror(self, rid):
        return asdict(self.manager.get(ha_action_id(rid)))

    def snapshot(self, rids):
        return {rid: {"row": self.row(rid), "mirror": self.mirror(rid),
                      "projection": self.manager.store.projection(ha_action_id(rid))}
                for rid in rids}

    def eventually(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("persisted lifecycle did not converge before the deadline")

    def issue(self, kind, *, ttl=4, task_id=None):
        args = ({"approval": ["approval", "request", "--action", "synthetic approval"],
                 "elicitation": ["elicitation", "request", "--question", "Choose A or B",
                                 "--options", '["A", "B"]'],
                 "human_action": ["human-action", "request", "--action", "synthetic action"]}
                [kind])
        args += ["--expires-in", str(ttl)]
        if task_id:
            args += ["--task-id", task_id]
        result = self.cli(*args)
        self.assertEqual(result["status"], "pending")
        return result["requestId"]

    def send(self, payload):
        self.sent.append(payload)
        self.record(event="projection", payload=payload)
        return {"ok": True, "event_id": f"$synthetic-{len(self.sent)}"}

    def project(self):
        return project(self.manager, self.send, "!synthetic:test")

    def expire_journey(self, mode):
        task_id = self.cli("task", "submit", "Synthetic expiry task")["taskId"]
        rids = [self.issue(kind, task_id=task_id if kind == "elicitation" else None)
                for kind in ("approval", "elicitation", "human_action")]
        choice = rids[1]
        unrelated = self.manager.create(HumanRequirement(
            kind="choice", runtime="unrelated", message="Unrelated", expires_at=1))
        other = HumanActionAdapter(str(self.base / "other-workspace/state/ha"))
        other.open_elicitation({"requestId": choice, "expiresAt": 1,
                               "params": {"question": "Other workspace"}})
        other_before = {p.name: p.read_bytes() for p in other.manager.store.root.iterdir()}
        unrelated_before = asdict(unrelated)
        self.project()
        before = self.snapshot(rids)
        self.assertEqual(self.cli("task", "status", task_id)["state"], "waiting_for_input")
        for rid in rids:
            self.assertEqual(before[rid]["row"]["status"], "pending")
            self.assertEqual(before[rid]["mirror"]["status"], "pending")
            self.assertEqual(before[rid]["mirror"]["expires_at"], before[rid]["row"]["expires_at"])
            self.assertEqual(before[rid]["mirror"]["id"], ha_action_id(rid))
            self.assertEqual(before[rid]["mirror"]["guard"], f"runtime:{rid}")
            self.assertTrue(before[rid]["projection"]["event_id"])
        expired_rows = None
        if mode != "running":
            self.stop_daemon()
            self.eventually(lambda: time.time() > max(self.row(r)["expires_at"] for r in rids))
            for rid in rids:
                self.assertEqual(self.row(rid)["status"], "pending")
            if mode == "already-expired":
                store = RequestStore(self.env["SUTANDO_RUNTIME_DB"])
                try:
                    for rid in rids:
                        self.assertEqual(store.get(rid)["status"], "expired")
                finally:
                    store.close()
                expired_rows = {rid: self.row(rid) for rid in rids}
            for rid in rids:
                self.assertEqual(self.mirror(rid), before[rid]["mirror"])
            self.record(event="stopped-state", mode=mode, records=self.snapshot(rids))
            self.start_daemon()
        self.eventually(lambda: all(self.row(r)["status"] == "expired"
                                   and self.mirror(r)["status"] == "expired" for r in rids))
        if expired_rows is not None:
            self.assertEqual({rid: self.row(rid) for rid in rids}, expired_rows)
        for rid in rids:
            old, current = before[rid], self.snapshot([rid])[rid]
            self.assertEqual(current["mirror"]["revision"], old["mirror"]["revision"] + 1)
            for key in old["mirror"].keys() - {"status", "revision", "updated_at"}:
                self.assertEqual(current["mirror"][key], old["mirror"][key], key)
            for key in old["row"].keys() - {"status", "resolved_at"}:
                self.assertEqual(current["row"][key], old["row"][key], key)
            self.assertEqual(current["projection"], old["projection"])
        active = {r.id for r in self.manager.active()}
        self.assertTrue(active.isdisjoint(map(ha_action_id, rids)))
        self.assertEqual(self.cli("task", "status", task_id)["state"], "pending")
        self.project()
        for rid in rids:
            cards = [p for p in self.sent if p["extra_content"].get(WIRE_FIELD, {}).get("id")
                     == ha_action_id(rid)]
            self.assertEqual(len(cards), 2, cards)
            self.assertEqual(cards[0]["op"], "message")
            self.assertEqual(cards[1]["op"], "edit")
            self.assertEqual(cards[1]["event_id"], before[rid]["projection"]["event_id"])
            self.assertEqual(cards[1]["extra_content"][WIRE_FIELD]["status"], "expired")
            self.assertIn("Expired", cards[1]["body"])
        settled = self.snapshot(rids)
        for revision in (before[choice]["mirror"]["revision"], self.mirror(choice)["revision"]):
            with self.assertRaises(StaleRequirementError):
                self.manager.apply_action(ActionReply(ha_action_id(choice), revision,
                                                       "opt1", f"runtime:{choice}"))
        for _ in range(2):
            time.sleep(0.15)
            self.stop_daemon()
            self.start_daemon()
            for rid in rids:
                expected = {"requestId": rid, "status": "expired"}
                self.assertEqual(self.cli("request", "get", rid), expected)
                self.assertEqual(self.cli("request", "wait", rid, "--timeout", "0.1"), expected)
            self.assertEqual(self.project(), [])
            self.assertEqual(self.snapshot(rids), settled)
        self.assertEqual(asdict(self.manager.get(unrelated.id)), unrelated_before)
        self.assertEqual({p.name: p.read_bytes() for p in other.manager.store.root.iterdir()}, other_before)
        self.record(event="converged", mode=mode, records=settled,
                    unrelated_preserved=True, other_workspace_preserved=True)

    def test_expiry_during_normal_resolution(self):
        self.expire_journey("running")

    def test_expiry_while_stopped(self):
        self.expire_journey("stopped")

    def test_restart_with_already_expired_rows_and_pending_mirrors(self):
        self.expire_journey("already-expired")

    def test_terminal_answers_and_post_restart_choice_completion(self):
        outcomes = {}
        for kind, answer, expected in (("elicitation", 1, "resolved"),
                                      ("approval", 1, "approved"),
                                      ("approval", 2, "denied"),
                                      ("human_action", 1, "completed")):
            rid = self.issue(kind, ttl=6)
            self.ha.resolve(ha_action_id(rid), {"1": [answer]}, "synthetic-owner")
            self.eventually(lambda: self.row(rid)["status"] == expected)
            result = {"requestId": rid, "status": expected, "resolvedBy": "synthetic-owner"}
            if kind == "elicitation":
                result["result"] = {"answer": "A"}
            self.assertEqual(self.cli("request", "get", rid), result)
            outcomes[rid] = result
        cancelled = self.issue("elicitation", ttl=6)
        outcomes[cancelled] = self.cli("request", "cancel", cancelled)
        self.assertEqual(outcomes[cancelled]["status"], "cancelled")
        self.project()
        before = self.snapshot(outcomes)
        task_id = self.cli("task", "submit", "Synthetic restart choice")["taskId"]
        live = self.issue("elicitation", ttl=120, task_id=task_id)
        pending = {"requestId": live, "status": "pending"}
        self.assertEqual(self.cli("request", "get", live), pending)
        self.assertEqual(self.cli("request", "wait", live, "--timeout", "0.05"),
                         {**pending, "timedOut": True})
        self.assertEqual(self.cli("task", "status", task_id)["state"], "waiting_for_input")
        self.eventually(lambda: time.time() > max(self.row(r)["expires_at"] for r in outcomes))
        self.stop_daemon()
        self.start_daemon()
        self.assertEqual(self.snapshot(outcomes), before)
        self.assertEqual(self.mirror(cancelled)["status"], "pending")
        for rid, expected in outcomes.items():
            self.assertEqual(self.cli("request", "get", rid), expected)
            self.assertEqual(self.cli("request", "wait", rid, "--timeout", "0.1"), expected)
        self.assertEqual(self.cli("request", "get", live), pending)
        self.assertEqual(self.mirror(live)["status"], "pending")
        self.assertEqual(self.cli("task", "status", task_id)["state"], "waiting_for_input")
        self.ha.resolve(ha_action_id(live), {"1": [2]}, "synthetic-owner")
        self.eventually(lambda: self.row(live)["status"] == "resolved")
        answer = {"requestId": live, "status": "resolved", "result": {"answer": "B"},
                  "resolvedBy": "synthetic-owner"}
        self.assertEqual(self.cli("request", "wait", live, "--timeout", "1"), answer)
        self.assertEqual(self.cli("task", "status", task_id)["state"], "pending")
        self.project()
        settled = self.snapshot([*outcomes, live])
        self.stop_daemon()
        self.start_daemon()
        self.assertEqual(self.cli("request", "get", live), answer)
        self.assertEqual(self.project(), [])
        self.assertEqual(self.snapshot([*outcomes, live]), settled)
        self.record(event="negative-controls", records=settled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
