#!/usr/bin/env python3
"""Evaluate role-lock handoff with real POSIX processes and temporary workspaces.

Run: python3 tests/workspace-lock-process.test.py -v
Lease clocks and birth tokens are fixtures; PIDs, process death, kernel locks
and atomic writes are real. Replacement/guard hooks control the schedule.
This excludes identity probing, PID reuse, power-loss durability and delivery.
"""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WAIT = 10


def emit(event, **data):
    print(json.dumps(dict(event=event, **data)), flush=True)


def actor_main(workspace):
    sys.path.insert(0, str(REPO / "src"))
    import file_lock
    import workspace_lock as locks

    locks.proc_start = lambda pid, proc=None: "fixture-birth-%d" % pid
    real_replace, real_lock = os.replace, locks.lock_fd

    def pause_replace(src, dst):
        emit("before_replace")
        if json.loads(sys.stdin.readline()) != {"continue": True}:
            raise RuntimeError("replacement was not resumed")
        real_replace(src, dst)

    def observe_guard(fd):
        free = file_lock.try_lock_fd(fd)
        if free:
            file_lock.unlock_fd(fd)
        emit("guard_attempt", contended=not free)
        real_lock(fd)
        if command.get("pause_after_lock"):
            emit("guard_acquired")
            if json.loads(sys.stdin.readline()) != {"continue": True}:
                raise RuntimeError("guard read was not resumed")

    for line in sys.stdin:
        command = json.loads(line)
        locks._now = lambda: command["now"]
        os.replace = pause_replace if command.get("pause_replace") else real_replace
        locks.lock_fd = observe_guard if command.get("observe_guard") else real_lock
        try:
            op, role = command["op"], command.get("role", "evaluation")
            value = getattr(locks, op)(role, Path(workspace))
            if op == "acquire":
                value = {"status": value.status, "holder": value.holder}
            emit("result", value=value)
        finally:
            os.replace, locks.lock_fd = real_replace, real_lock


class Actor:
    def __init__(self, case, workspace, home):
        self.case = case
        home.mkdir()
        self.errors = tempfile.TemporaryFile()
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
               "TMPDIR": str(home), "SUTANDO_HOST_LABEL": "role-lock-test",
               "PYTHONNOUSERSITE": "1"}
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).resolve()), "--actor", str(workspace)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors,
            env=env, cwd=home,
        )
        self.buffer = b""
        case.addCleanup(self.close)

    def close(self):
        process = self.process
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(WAIT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(WAIT)
        finally:
            process.stdin.close()
            process.stdout.close()
            self.errors.close()

    def send(self, command):
        self.process.stdin.write(json.dumps(command).encode() + b"\n")
        self.process.stdin.flush()

    def receive(self, event="result"):
        deadline = time.monotonic() + WAIT
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while b"\n" not in self.buffer:
                remaining = deadline - time.monotonic()
                self.case.assertGreater(remaining, 0, "child response deadline expired")
                self.case.assertTrue(selector.select(remaining), "child response deadline expired")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    self.errors.seek(0)
                    self.case.fail("child exited before %s: %s" % (
                        event, self.errors.read(8192).decode(errors="replace")))
                self.buffer += chunk
                self.case.assertLessEqual(len(self.buffer), 65536, "unbounded child response")
        line, self.buffer = self.buffer.split(b"\n", 1)
        message = json.loads(line)
        self.case.assertEqual(message["event"], event, message)
        return message

    def call(self, op, now, **options):
        self.send(dict(op=op, now=now, **options))
        return self.receive()["value"]


@unittest.skipUnless(os.name == "posix", "real POSIX guard-lock schedules")
class RoleLockProcessTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sutando-role-lock-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.actors = []

    def actor(self, workspace=None):
        child = Actor(self, workspace or self.workspace, self.root / ("home-%d" % len(self.actors)))
        self.actors.append(child)
        return child

    def lease_path(self, workspace=None, role="evaluation"):
        return (workspace or self.workspace) / "state" / "locks" / (role + ".lock")

    def assert_owner(self, actor, heartbeat, workspace=None, role="evaluation"):
        record = json.loads(self.lease_path(workspace, role).read_bytes())
        self.assertEqual(record["pid"], actor.process.pid)
        self.assertEqual(record["heartbeat_at"], heartbeat)
        self.assertEqual(record["role"], role)
        self.assertEqual(record["workspace"], str(workspace or self.workspace))
        self.assertEqual(record.get("proc_start"), "fixture-birth-%d" % actor.process.pid)
        return record

    def test_a_reaped_live_holder_cannot_mutate_its_successor(self):
        old, successor = self.actor(), self.actor()
        self.assertEqual(old.call("acquire", 1000)["status"], "acquired")
        self.assert_owner(old, 1000)
        self.assertEqual(successor.call("acquire", 1090)["status"], "reaped")
        self.assertIsNone(old.process.poll(), "takeover is from a still-running process")
        self.assert_owner(successor, 1090)
        snapshot = self.lease_path().read_bytes()
        for op, expected in (("heartbeat", False), ("retain", False), ("release", None)):
            with self.subTest(op=op):
                self.assertEqual(old.call(op, 1091), expected)
                self.assertEqual(self.lease_path().read_bytes(), snapshot)
        deferred = old.call("acquire", 1091)
        self.assertEqual(deferred["status"], "deferred")
        self.assertEqual(deferred["holder"]["pid"], successor.process.pid)
        self.assertEqual(self.lease_path().read_bytes(), snapshot)

        other_workspace = self.root / "other-workspace"
        other = self.actor(other_workspace)
        self.assertEqual(other.call("acquire", 1091)["status"], "acquired")
        self.assertEqual(old.call("acquire", 1091, role="other-role")["status"], "acquired")
        self.assert_owner(other, 1091, workspace=other_workspace)
        self.assert_owner(old, 1091, role="other-role")
        self.assertEqual(self.lease_path().read_bytes(), snapshot)
        self.assertTrue(successor.call("heartbeat", 1092))
        self.assert_owner(successor, 1092)
        successor.call("release", 1092)
        self.assertFalse(self.lease_path().exists())
        self.assertEqual(old.call("acquire", 1093)["status"], "acquired")
        self.assert_owner(old, 1093)

    def test_a_contender_reads_the_heartbeat_committed_under_the_guard(self):
        holder, contender = self.actor(), self.actor()
        self.assertEqual(holder.call("acquire", 1000)["status"], "acquired")
        holder.send(dict(op="heartbeat", now=1001, pause_replace=True))
        holder.receive("before_replace")
        self.assert_owner(holder, 1000)
        contender.send(dict(op="acquire", now=1090, observe_guard=True))
        self.assertTrue(contender.receive("guard_attempt")["contended"])
        holder.send({"continue": True})
        self.assertTrue(holder.receive()["value"])
        result = contender.receive()["value"]
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["holder"], self.assert_owner(holder, 1001))

    def test_death_before_replace_releases_the_guard_and_reaps_a_fresh_lease(self):
        holder, successor = self.actor(), self.actor()
        self.assertEqual(holder.call("acquire", 1000)["status"], "acquired")
        snapshot = self.lease_path().read_bytes()
        holder.send(dict(op="heartbeat", now=1001, pause_replace=True))
        holder.receive("before_replace")
        self.assertEqual(self.lease_path().read_bytes(), snapshot)
        pending = list(self.lease_path().parent.glob("*.tmp.%d" % holder.process.pid))
        self.assertEqual(len(pending), 1, "pause follows the real temporary write")
        self.assertEqual(json.loads(pending[0].read_bytes())["heartbeat_at"], 1001)
        successor.send(dict(op="acquire", now=1002, observe_guard=True, pause_after_lock=True))
        self.assertTrue(successor.receive("guard_attempt")["contended"])
        holder.process.kill()
        holder.process.wait(WAIT)
        successor.receive("guard_acquired")
        successor.send({"continue": True})
        self.assertEqual(successor.receive()["value"]["status"], "reaped")
        self.assert_owner(successor, 1002)
        self.assertTrue(successor.call("heartbeat", 1003))
        self.assert_owner(successor, 1003)
        successor.call("release", 1003)
        self.assertFalse(self.lease_path().exists())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--actor":
        actor_main(sys.argv[2])
    else:
        unittest.main()
