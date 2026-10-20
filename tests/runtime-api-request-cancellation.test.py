#!/usr/bin/env python3
"""Request cancellation through the real daemon and bin/sutando on a private socket."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

from ha_adapter import HumanActionAdapter, ha_action_id  # noqa: E402
from hitl.projector import project  # noqa: E402

from hitl.schema import ActionReply, StaleRequirementError, WIRE_FIELD  # noqa: E402
from request_store import RequestStore  # noqa: E402

ISSUE_ARGS = {
    "approval": ["approval", "request", "--action", "synthetic.approval"],
    "elicitation": ["elicitation", "request", "--question", "Choose a fixture?",
                    "--options", '["A", "B"]'],
    "human_action": ["human-action", "request", "--action", "Synthetic local act"],
}
WAITING = {"approval": "waiting_for_approval", "elicitation": "waiting_for_input",
           "human_action": "waiting_for_human_action"}


def record(event, **fields):
    print(json.dumps({"event": event, "at": datetime.now(timezone.utc).isoformat(),
                      **fields}), flush=True)


class RequestCancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rc-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "workspace"
        self.state = self.workspace / "state"
        self.socket = self.base / "api.sock"
        self.env = dict(os.environ)
        self.env.update({
            "SUTANDO_TEST_MODE": "1", "SUTANDO_PACKAGED": "", "SUTANDO_APP_SUPPORT": "",
            "SUTANDO_WORKSPACE": str(self.workspace),
            "SUTANDO_WORKSPACE_DIR": str(self.workspace),
            "SUTANDO_DEFAULT_WORKSPACE": str(self.workspace),
            "SUTANDO_RESULTS_DIR": str(self.workspace / "results"),
            "SUTANDO_RUNTIME_STATE": str(self.state),
            "SUTANDO_RUNTIME_SOCKET": str(self.socket),
            "SUTANDO_RUNTIME_DB": str(self.state / "runtime.sqlite"),
            "SUTANDO_HA_DIR": str(self.state / "ha"),
            "SUTANDO_RUN_DIR": str(self.base / "run"),
            "SUTANDO_INSTANCE_REGISTRY": str(self.base / "registry"),
            "SUTANDO_AGENT_ID": "synthetic-cancellation-test",
            "SUTANDO_HOST_LABEL": "synthetic-test", "SUTANDO_INSTANCE_ID": "",
            "CLAUDE_CONFIG_DIR": str(self.base / "claude"),
            "SUTANDO_TMUX_SOCKET": str(self.base / "unused-tmux.sock"),
            "SUTANDO_SCP_WSS_URL": "", "SUTANDO_SCP_WSS_TOKEN": "",
            "SUTANDO_TELEMETRY": "0", "SUTANDO_PY": sys.executable,
        })
        self.store = RequestStore(self.env["SUTANDO_RUNTIME_DB"])
        self.addCleanup(self.store.close)
        self.ha = HumanActionAdapter(self.env["SUTANDO_HA_DIR"])
        self.manager = self.ha.manager
        self.proc = None
        self.boot = 0
        self.addCleanup(self.stop)
        self.start()

    def start(self):
        self.boot += 1
        self.log_path = self.base / f"daemon-{self.boot}.log"
        argv = [sys.executable, str(ROOT / "src" / "runtime-api" / "server.py")]
        with self.log_path.open("w") as log:
            self.proc = subprocess.Popen(argv, cwd=ROOT, env=self.env,
                                         stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=subprocess.STDOUT)
        record("daemon-start", argv=argv, pid=self.proc.pid, socket=str(self.socket))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.fail(f"daemon exited before readiness: {self.log_path.read_text()[-4000:]}")
            if "listening on" in self.log_path.read_text():
                readiness = self.cli("request", "list")
                self.assertIn("requests", readiness)
                record("ready", pid=self.proc.pid, response=readiness)
                return
            time.sleep(0.05)
        self.fail(f"daemon readiness timed out: {self.log_path.read_text()[-4000:]}")

    def stop(self):
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        record("daemon-stop", pid=proc.pid, exit_code=proc.returncode, reaped=True,
               log=self.log_path.read_text()[-4000:])
        self.assertEqual(proc.returncode, 0)

    def cli(self, *args, error=None):
        argv = [str(ROOT / "bin" / "sutando"), *args]
        proc = subprocess.run(argv, cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=10)
        record("cli", argv=argv, exit_code=proc.returncode,
               stdout=proc.stdout, stderr=proc.stderr)
        if error is not None:
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(proc.stdout, "")
            failure = json.loads(proc.stderr)
            self.assertIn(str(error), failure["error"])
            return failure
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def issue(self, kind, task_id=None):
        args = ISSUE_ARGS[kind] + (["--task-id", task_id] if task_id else [])
        result = self.cli(*args)
        self.assertEqual(result["status"], "pending")
        rid = result["requestId"]
        req = self.manager.get(ha_action_id(rid))
        self.assertIsNotNone(req)
        self.assertEqual(req.status, "pending")
        return rid

    def snapshot(self, rid):
        return self.store.get(rid), self.manager.get(ha_action_id(rid))

    def inventory(self):
        return (self.store._db.execute(
                    "SELECT request_id FROM runtime_requests ORDER BY request_id").fetchall(),
                sorted(p.name for p in self.manager.store.root.glob("hitl_*.json")))

    def project_locally(self):
        sent = []

        def send(payload):
            sent.append(payload)
            return {"event_id": "$synthetic-" + payload["extra_content"][WIRE_FIELD]["id"]}

        project(self.manager, send, "!synthetic:test")
        record("local-projection", payloads=sent)
        return {p["extra_content"][WIRE_FIELD]["id"]: p for p in sent}

    def assert_cancelled(self, rid):
        row, req = self.snapshot(rid)
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(req.status, "cancelled")
        self.assertNotIn(req.id, [r.id for r in self.manager.active()])
        record("cancelled-pair", request_id=rid, requirement_id=req.id,
               runtime_status=row["status"], mirror_status=req.status, revision=req.revision)

    @contextmanager
    def mirror_write_failure(self):
        root = self.manager.store.root
        original_mode = root.stat().st_mode & 0o777
        root.chmod(0o500)
        try:
            yield
        finally:
            root.chmod(original_mode)

    def test_all_types_cancel_stale_actions_project_and_survive_restart(self):
        snapshots, task_files = {}, {}
        for kind in ISSUE_ARGS:
            with self.subTest(kind=kind):
                task = self.cli("task", "submit", f"Synthetic {kind} cancellation")
                tid = task["taskId"]
                path = self.workspace / "tasks" / f"{tid}.txt"
                task_files[path] = path.read_bytes()
                rid = self.issue(kind, tid)
                self.assertEqual(self.cli("task", "status", tid)["state"], WAITING[kind])
                before = self.manager.get(ha_action_id(rid))
                reply = ActionReply(hitl_id=before.id, expected_revision=before.revision,
                                    guard=before.guard, action_id=before.actions[0].id)
                initial = self.project_locally()[before.id]
                self.assertEqual(initial["extra_content"][WIRE_FIELD]["status"], "pending")
                self.assertEqual(self.cli("request", "cancel", rid)["status"], "cancelled")
                self.assert_cancelled(rid)
                with self.assertRaises(StaleRequirementError):
                    self.manager.apply_action(reply)
                cancelled = self.project_locally()[before.id]
                self.assertEqual(cancelled["op"], "edit")
                self.assertEqual(cancelled["event_id"], "$synthetic-" + before.id)
                self.assertEqual(cancelled["extra_content"][WIRE_FIELD]["status"], "cancelled")
                self.assertIn("Cancelled", cancelled["body"])
                self.assertEqual(self.cli("task", "status", tid), {"taskId": tid, "state": "pending"})
                snapshots[rid] = self.snapshot(rid)
                self.cli("request", "cancel", rid)
                self.assertEqual(self.snapshot(rid), snapshots[rid])
        inventory = self.inventory()
        self.stop()
        self.start()
        for rid, before in snapshots.items():
            self.assertEqual(self.cli("request", "get", rid)["status"], "cancelled")
            self.cli("request", "cancel", rid)
            self.assertEqual(self.snapshot(rid), before)
        self.assertEqual(self.inventory(), inventory)
        self.assertEqual(self.project_locally(), {})
        self.assertEqual({p: p.read_bytes() for p in (self.workspace / "tasks").glob("*.txt")},
                         task_files)

    def test_unrelated_terminal_unknown_and_unmirrored_controls(self):
        unrelated = self.issue("elicitation")
        before = self.snapshot(unrelated)
        cancelled = self.issue("approval")
        self.cli("request", "cancel", cancelled)
        self.assertEqual(self.snapshot(unrelated), before)
        self.assertIn(ha_action_id(unrelated), [r.id for r in self.manager.active()])
        self.ha.resolve(ha_action_id(unrelated), {"1": [2]}, "synthetic-owner")
        resolved = self.cli("request", "wait", unrelated, "--timeout", "2")
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["result"], {"answer": "B"})
        for kind, answer, outcome in (("approval", "Approve", "approved"),
                                      ("approval", "Deny", "denied"),
                                      ("elicitation", "B", "resolved"),
                                      ("human_action", "Done", "completed"),
                                      ("human_action", "Decline", "declined")):
            with self.subTest(outcome=outcome):
                rid = self.issue(kind)
                self.ha.resolve(ha_action_id(rid), {"1": answer}, "synthetic-owner")
                self.assertEqual(self.cli("request", "wait", rid, "--timeout", "2")["status"],
                                 outcome)
                before = self.snapshot(rid)
                self.assertEqual(before[1].answer, answer)
                self.assertEqual(self.cli("request", "cancel", rid)["status"], outcome)
                self.assertEqual(self.snapshot(rid), before)
        inventory = self.inventory()
        self.cli("request", "cancel", "unknown-fixture", error=-32602)
        self.assertEqual(self.inventory(), inventory)
        for kind in ("approval", "capability"):
            row = self.store.create(kind, f"{kind}.request", "synthetic", {})
            files = self.inventory()[1]
            self.assertEqual(self.cli("request", "cancel", row["requestId"])["status"], "cancelled")
            self.assertIsNone(self.manager.get(ha_action_id(row["requestId"])))
            self.assertEqual(self.inventory()[1], files)

    def test_failed_mirror_write_is_reported_and_retry_repairs_after_restart(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                rid = self.issue("human_action")
                before = self.snapshot(rid)[1]
                inventory = self.inventory()
                with self.mirror_write_failure():
                    failure = self.cli("request", "cancel", rid, error=-32000)
                    self.assertIn("Permission denied", failure["error"])
                row, mirror = self.snapshot(rid)
                self.assertEqual(row["status"], "cancelled")
                self.assertEqual(mirror, before)
                self.assertIn(mirror.id, [r.id for r in self.manager.active()])
                if restart:
                    self.stop()
                    self.start()
                    self.assertEqual(self.snapshot(rid), (row, before))
                self.assertEqual(self.cli("request", "cancel", rid)["status"], "cancelled")
                self.assert_cancelled(rid)
                self.assertEqual(self.store.get(rid), row)
                self.assertEqual(self.manager.get(mirror.id).revision, before.revision + 1)
                reconciled = self.snapshot(rid)
                self.cli("request", "cancel", rid)
                self.assertEqual(self.snapshot(rid), reconciled)
                self.assertEqual(self.inventory(), inventory)

    def test_retry_preserves_a_mirror_answer_that_won_before_reconciliation(self):
        rid = self.issue("elicitation")
        with self.mirror_write_failure():
            self.cli("request", "cancel", rid, error=-32000)
        self.ha.resolve(ha_action_id(rid), {"1": "B"}, "synthetic-owner")
        before = self.snapshot(rid)
        self.assertEqual(before[0]["status"], "cancelled")
        self.assertEqual(before[1].status, "resolved")
        self.assertEqual(before[1].answer, "B")
        self.stop()
        self.start()
        self.cli("request", "cancel", rid)
        self.assertEqual(self.snapshot(rid), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
