#!/usr/bin/env python3
"""Expiry journeys through the shipped daemon/CLI and private Unix sockets."""
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))
from ha_adapter import DEFAULT_TTL_S, HumanActionAdapter, ha_action_id
from hitl.projector import project
from hitl.replies import EVENT_TYPE, REPLY_FIELD, HitlReplyHandler
from hitl.schema import WIRE_FIELD
from request_store import RequestStore

KINDS = ("approval", "elicitation", "human_action")
WAITING = {"approval": "waiting_for_approval", "elicitation": "waiting_for_input",
           "human_action": "waiting_for_human_action"}
OWNER = "@owner:synthetic"


class ExpiryJourneyTests(unittest.TestCase):
    def setUp(self):
        self.env = os.environ.copy()
        supplied_socket = self.env.get("SUTANDO_RUNTIME_SOCKET")
        environment_file = (Path(supplied_socket).parent / "environment.json"
                            if supplied_socket else None)
        if environment_file and environment_file.is_file():
            self.env.update(json.loads(environment_file.read_text()))
            private_root = environment_file.parent
        else:
            private_root = None
        self.tmp = tempfile.TemporaryDirectory(prefix="expiry-", dir=private_root)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.ws = self.base / "workspace"
        self.state = self.ws / "state"
        self.state.mkdir(parents=True)
        for name in ("tasks", "results"):
            (self.ws / name).mkdir()
        self.socket = self.base / "api.sock"
        self.db = self.state / "runtime.sqlite"
        self.ha_dir = self.state / "ha"
        self.env.update({
            "SUTANDO_TEST_MODE": "1", "SUTANDO_TELEMETRY": "0",
            "SUTANDO_PACKAGED": "", "SUTANDO_APP_SUPPORT": "",
            "SUTANDO_WORKSPACE": str(self.ws), "SUTANDO_WORKSPACE_DIR": str(self.ws),
            "SUTANDO_DEFAULT_WORKSPACE": str(self.ws),
            "SUTANDO_RESULTS_DIR": str(self.ws / "results"),
            "SUTANDO_RUNTIME_STATE": str(self.state),
            "SUTANDO_RUNTIME_SOCKET": str(self.socket),
            "SUTANDO_RUNTIME_DB": str(self.db), "SUTANDO_HA_DIR": str(self.ha_dir),
            "SUTANDO_RUN_DIR": str(self.base / "run"),
            "SUTANDO_INSTANCE_REGISTRY": str(self.base / "registry"),
            "SUTANDO_AGENT_ID": "expiry-synthetic", "SUTANDO_HOST_LABEL": "expiry-test",
            "SUTANDO_INSTANCE_ID": "", "CLAUDE_CONFIG_DIR": str(self.base / "claude"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        for key in ("SUTANDO_RUNTIME_RESOLVE_POLL", "SUTANDO_SCP_WSS_URL",
                    "SUTANDO_SCP_WSS_TOKEN"):
            self.env.pop(key, None)
        self.diagnostics = ((private_root / "diagnostics") if private_root else self.base)
        self.diagnostics.mkdir(exist_ok=True)
        self.label = self._testMethodName + "-" + self.base.name
        self.observations = []
        self.addCleanup(self.save_observations)
        self.proc = None
        self.boot = 0
        self.addCleanup(self.stop)
        self.ha = HumanActionAdapter(str(self.ha_dir))
        self.manager = self.ha.manager
        self.deliveries = []
        self.handler = HitlReplyHandler(self.manager, OWNER, workspace=self.ws,
                                        log=lambda message: None)
        self.start()

    def note(self, kind, **fields):
        self.observations.append({"time": time.time(), "kind": kind, **fields})

    def save_observations(self):
        path = self.diagnostics / (self.label + ".json")
        path.write_text(json.dumps(self.observations, indent=2))
        print("observations:", path, flush=True)

    def cli(self, *args):
        argv = ["bash", str(ROOT / "bin" / "sutando"), *map(str, args)]
        result = subprocess.run(argv, cwd=ROOT, env=self.env, text=True,
                                capture_output=True, timeout=10)
        self.note("cli", argv=argv, exit=result.returncode,
                  stdout=result.stdout, stderr=result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def start(self):
        self.boot += 1
        log = self.diagnostics / f"{self.label}-server-{self.boot}.log"
        argv = [sys.executable, str(ROOT / "src/runtime-api/server.py")]
        started = time.monotonic()
        with log.open("w") as output:
            self.proc = subprocess.Popen(argv, cwd=ROOT, env=self.env,
                                         stdout=output, stderr=subprocess.STDOUT)
        self.note("start", argv=argv, pid=self.proc.pid, log=str(log))
        while time.monotonic() - started < 5:
            self.assertIsNone(self.proc.poll(), log.read_text())
            if self.socket.exists() and "listening on" in log.read_text():
                response = self.cli("request", "list")
                self.note("ready", pid=self.proc.pid, response=response,
                          elapsed=time.monotonic() - started)
                return
            time.sleep(0.05)
        self.fail("daemon readiness timed out: " + log.read_text())

    def stop(self):
        if self.proc is None:
            return
        process, self.proc = self.proc, None
        # SIGINT lets asyncio cancel tasks without interrupting a dequeued callback.
        process.send_signal(signal.SIGINT)
        try:
            code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            self.fail("daemon needed forced termination")
        self.note("reaped", pid=process.pid, exit=code)
        self.assertEqual(code, 0)

    def raw(self, rid):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute("SELECT * FROM runtime_requests WHERE request_id = ?",
                                     (rid,)).fetchone())

    def issue(self, kind, expires=None):
        tid = self.cli("task", "submit", "Synthetic local " + kind)["taskId"]
        args = [kind.replace("_", "-"), "request", "--task-id", tid]
        if kind == "elicitation":
            args += ["--question", "Choose a local option", "--type", "single_select",
                     "--options", '["Alpha", "Beta"]']
        else:
            args += ["--action", "Synthetic local action"]
        if expires is not None:
            args += ["--expires-in", str(expires)]
        row = self.cli(*args)
        rid = row["requestId"]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(self.cli("task", "status", tid)["state"], WAITING[kind])
        req = self.manager.get(ha_action_id(rid))
        self.assertEqual(req.subject["runtime_request_id"], rid)
        return {"rid": rid, "tid": tid, "req": req, "row": self.raw(rid)}

    def send(self, payload):
        self.deliveries.append(payload)
        self.note("synthetic-delivery", payload=payload)
        return {"ok": True, "event_id": "$synthetic-" + str(len(self.deliveries))}

    def project(self):
        return project(self.manager, self.send, "!local:synthetic")

    def reply(self, req, actor=OWNER, revision=None, action=None):
        event = {"type": EVENT_TYPE, "event_id": "$synthetic-click",
                 "actor_id": actor, "content": {REPLY_FIELD: {
                     "hitl_id": req.id,
                     "expected_revision": req.revision if revision is None else revision,
                     "guard": req.guard, "action_id": action or req.actions[0].id}}}
        self.handler.offer(event)
        self.note("synthetic-reply", event=event, outcome=self.handler.last_outcome)
        return self.handler.last_outcome

    def wait_expired(self, records, limit=None):
        remaining = list(records)
        while remaining:
            for record in remaining[:]:
                rid = record["rid"]
                row, req = self.raw(rid), self.manager.get(record["req"].id)
                bound = limit or record["row"]["expires_at"] + 6
                if row["status"] == req.status == "expired":
                    self.assertLessEqual(time.time(), bound, rid)
                    self.note("expired", request=row, requirement=req.to_wire(),
                              seconds_after_deadline=time.time() - row["expires_at"])
                    remaining.remove(record)
                elif time.time() > bound:
                    self.fail(f"expiry did not reconcile: {rid}: {row['status']}/{req.status}")
            if remaining:
                time.sleep(0.05)

    def assert_expired_projection(self, records, ledgers):
        for record in records:
            rid, before = record["rid"], record["req"]
            row, req = self.raw(rid), self.manager.get(before.id)
            self.assertEqual(req.revision, before.revision + 1)
            for field in ("id", "subject", "guard", "expires_at", "answer", "chosen_action"):
                self.assertEqual(getattr(req, field), getattr(before, field))
            for field, value in record["row"].items():
                if field not in ("status", "resolved_at"):
                    self.assertEqual(row[field], value, field)
            self.assertEqual(self.manager.store.projection(req.id), ledgers[req.id])
            self.assertNotIn(req.id, {r.id for r in self.manager.active()})
            status = self.cli("task", "status", record["tid"])
            self.assertEqual(status["state"], "pending")
            self.assertNotIn("waitingOn", status)
            self.assertEqual(self.cli("request", "get", rid)["status"], "expired")
            self.assertEqual(self.reply(before), "rejected")
            self.assertEqual(self.raw(rid), row)
            self.assertEqual(self.manager.get(req.id), req)
        start = len(self.deliveries)
        self.project()
        edits = self.deliveries[start:]
        self.assertEqual(len(edits), len(records))
        for edit in edits:
            wire = edit["extra_content"][WIRE_FIELD]
            self.assertEqual(edit["op"], "edit")
            self.assertEqual(wire["status"], "expired")
            self.assertEqual(edit["event_id"], ledgers[wire["id"]]["event_id"])
        self.assertEqual(self.project(), [])
        self.assertEqual(self.project(), [])
        self.assertEqual(len(self.deliveries), start + len(records))

    def test_periodic_expiry_projection_and_negative_controls(self):
        indefinite = [self.issue(kind) for kind in KINDS]
        unrelated = [self.issue(kind, 3600) for kind in KINDS]
        resolved = [self.issue(kind, 30) for kind in KINDS]
        for record in resolved:
            req = record["req"]
            self.assertEqual(self.reply(req, actor="@other:synthetic"), "ignored")
            self.assertEqual(self.reply(req, revision=req.revision - 1), "rejected")
            self.assertEqual(self.manager.get(req.id), req)
            self.assertEqual(self.raw(record["rid"]), record["row"])
            self.assertEqual(self.reply(req, action=req.actions[-1].id), "applied")
            answer = self.cli("request", "wait", record["rid"], "--timeout", "4")
            kind = record["row"]["request_type"]
            self.assertEqual(answer["status"], {"approval": "denied", "elicitation": "resolved",
                                                "human_action": "declined"}[kind])
            if kind == "elicitation":
                self.assertEqual(answer["result"], {"answer": "Beta"})
            record["terminal_row"] = self.raw(record["rid"])
            record["terminal_req"] = self.manager.get(req.id)
        expiring = [self.issue(kind, 15) for kind in KINDS]
        self.project()
        ledgers = {r["req"].id: self.manager.store.projection(r["req"].id)
                   for r in expiring}
        for record in expiring:
            creates = [p for p in self.deliveries
                       if p.get("extra_content", {}).get(WIRE_FIELD, {}).get("id") == record["req"].id]
            self.assertEqual(len(creates), 1)
            self.assertEqual(creates[0]["op"], "message")
            self.assertEqual(creates[0]["extra_content"][WIRE_FIELD]["status"], "pending")
        result_file = self.ws / "results" / "task-synthetic-existing.txt"
        result_file.write_text("Synthetic result payload\n")
        payloads = {p: p.read_bytes() for folder in ("tasks", "results")
                    for p in (self.ws / folder).glob("*.txt")}
        other = self.base / "other-workspace"
        other_store = RequestStore(str(other / "state" / "runtime.sqlite"))
        other_ha = HumanActionAdapter(str(other / "state" / "ha"))
        other_row = other_store.create("approval", "approval.request", "other", {}, expires_in_s=3600)
        other_aid = other_ha.open_approval(other_row)
        other_ha.manager.record_projection(other_aid, 1, "$other-original")
        other_store.close()
        other_bytes = {p: p.read_bytes() for p in other.rglob("*") if p.is_file()}
        self.wait_expired(expiring)
        self.assert_expired_projection(expiring, ledgers)
        deadline = max(r["row"]["expires_at"] for r in resolved)
        while time.time() <= deadline:
            time.sleep(0.05)
        self.stop()
        self.start()
        for record in indefinite + unrelated:
            self.assertEqual(self.raw(record["rid"]), record["row"])
            self.assertEqual(self.manager.get(record["req"].id), record["req"])
        for record in indefinite:
            self.assertIsNone(record["row"]["expires_at"])
            self.assertAlmostEqual(record["req"].expires_at - record["row"]["created_at"],
                                   DEFAULT_TTL_S, delta=1)
        for record in resolved:
            self.assertEqual(self.raw(record["rid"]), record["terminal_row"])
            self.assertEqual(self.manager.get(record["req"].id), record["terminal_req"])
            result = self.cli("request", "get", record["rid"])
            if record["row"]["request_type"] == "elicitation":
                self.assertEqual(result["result"], {"answer": "Beta"})
        self.assertEqual(payloads, {p: p.read_bytes() for p in payloads})
        self.assertEqual(other_bytes, {p: p.read_bytes() for p in other.rglob("*") if p.is_file()})

    def test_restart_recovers_downtime_and_already_expired_rows(self):
        downtime = [self.issue(kind, 30) for kind in KINDS]
        already_expired = [self.issue(kind, 30) for kind in KINDS]
        records = downtime + already_expired
        self.project()
        ledgers = {r["req"].id: self.manager.store.projection(r["req"].id) for r in records}
        self.stop()
        margin = min(r["row"]["expires_at"] for r in records) - time.time()
        self.note("offline-before-expiry", margin_seconds=margin)
        self.assertGreater(margin, 2, "fixture setup must finish before the first deadline")
        for record in records:
            self.assertEqual(self.raw(record["rid"])["status"], "pending")
            self.assertEqual(self.manager.get(record["req"].id), record["req"])
        deadline = max(r["row"]["expires_at"] for r in records)
        while time.time() <= deadline:
            time.sleep(0.05)
        store = RequestStore(str(self.db))
        try:
            for record in already_expired:
                self.assertEqual(store.get(record["rid"])["status"], "expired")
                record["expired_row"] = self.raw(record["rid"])
        finally:
            store.close()
        for record in downtime:
            self.assertEqual(self.raw(record["rid"])["status"], "pending")
        for record in records:
            self.assertEqual(self.manager.get(record["req"].id).status, "pending")
        bound = time.time() + 6
        self.start()
        self.wait_expired(records, limit=bound)
        for record in already_expired:
            self.assertEqual(self.raw(record["rid"]), record["expired_row"])
        self.assert_expired_projection(records, ledgers)
        snapshots = [(self.raw(r["rid"]), self.manager.get(r["req"].id),
                      self.manager.store.projection(r["req"].id)) for r in records]
        self.stop()
        self.start()
        self.assertEqual(snapshots, [(self.raw(r["rid"]), self.manager.get(r["req"].id),
                                     self.manager.store.projection(r["req"].id)) for r in records])
        self.assertEqual(self.project(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
