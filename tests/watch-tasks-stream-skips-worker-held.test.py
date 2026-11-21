#!/usr/bin/env python3
"""The core's watcher never routes or announces a task a worker already holds.

A worker's own lane routes each task it queues before publishing it to `tasks/`, so the task is
held (a sentinel in `deliveries/<worker>/`) when the core's watcher first sees it. The watcher
leaves it alone: no handler probe or run, no TASK_FILE. A task nobody holds is routed as before, and
a deliveries folder that cannot be read still goes to the handler, which fails closed. "Holds" means
the delivery committed: a sentinel with the attribution naming that worker. A sentinel alone (a router
that died between the two writes) goes to the handler, whose replay attributes it. The skip is the
core's only: the worker's own watcher announces the task it holds.

Run: python3 tests/watch-tasks-stream-skips-worker-held.test.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from clean_watcher_env import clean_env  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.process_group import kill_group, popen_bound  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
W = "d" * 32
DEADLINE_S = 40.0


class CoreWatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skip-held-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = self.tmp / "ws"
        for d in ("tasks", "results/archive", "state"):
            (self.ws / d).mkdir(parents=True)
        feed = self.tmp / "feed"
        feed.write_text("")
        b = self.tmp / "bin"
        b.mkdir()
        (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
        (b / "fswatch").chmod(0o755)
        self.calls = self.tmp / "handler-calls.log"
        self.handler = self.tmp / "handler.sh"
        # Declines every task, so a routed one is announced to the core.
        self.handler.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {self.calls}\nexit 3\n')
        self.handler.chmod(0o755)

    def task(self, tid):
        (self.ws / "tasks" / f"{tid}.txt").write_text(f"id: {tid}\naccess_tier: owner\ntask: hi\n")

    def hold(self, tid, attributed=True):
        folder = self.ws / "deliveries" / W
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{tid}.txt").touch()
        if attributed:
            (self.ws / "state" / "attribution").mkdir(parents=True, exist_ok=True)
            (self.ws / "state" / "attribution" / tid).write_text(W + "\n")

    def run_watcher(self, until, worker=False):
        env = clean_env()
        env.update(PATH=f"{self.tmp / 'bin'}:{env['PATH']}", TMPDIR=str(self.tmp),
                   SUTANDO_RESULTS_DIR=str(self.ws / "results"), SUTANDO_TASK_EVENT_HANDLER=str(self.handler))
        inbox = self.ws / "tasks"
        if worker:
            inbox = self.ws / "deliveries" / W
            env.update(SUTANDO_WORKSPACE_DIR=str(self.ws), SUTANDO_INSTANCE_ID=W, SUTANDO_INBOX_RESOLVER=str(
                REPO / "skills" / "worker-pool" / "scripts" / "resolve_inbox_entry.py"))
        errf = open(self.tmp / "watcher.err", "w+")
        p = popen_bound(["bash", "src/watch-tasks-stream.sh", str(inbox),
                         "--role", "standby", "--inbox", str(inbox)],
                        cwd=str(REPO), env=env, stdout=subprocess.PIPE, stderr=errf)
        os.set_blocking(p.stdout.fileno(), False)
        out, end = b"", time.time() + DEADLINE_S
        try:
            while time.time() < end and p.poll() is None:
                try:
                    out += os.read(p.stdout.fileno(), 65536) or b""
                except BlockingIOError:
                    pass
                errf.seek(0)
                if until(out.decode(), errf.read()):
                    break
                time.sleep(0.1)
        finally:
            # Bash defers its TERM trap until a foreground child exits; a loaded runner can outlast a fixed wait.
            kill_group(p.pid, wait_s=10.0)
            p.wait(timeout=10)
            p.stdout.close()
            errf.seek(0)
            err = errf.read()
            errf.close()
        calls = self.calls.read_text() if self.calls.exists() else ""
        return out.decode(), err, calls

    def test_a_held_task_is_not_routed_and_a_free_one_is(self):
        self.task("task-held")
        self.hold("task-held")
        self.task("task-free")
        out, err, calls = self.run_watcher(
            lambda o, e: "TASK_FILE: task-free.txt" in o and "task-held.txt" in e)
        self.assertIn("held by a worker, not routing it: task-held.txt", err)
        self.assertNotIn("task-held", calls, "the router never ran for the held task")
        self.assertNotIn("task-held", out)
        self.assertIn("task-free.txt", calls)
        self.assertIn("TASK_FILE: task-free.txt", out)

    def test_a_sentinel_with_no_attribution_goes_to_the_router_to_be_finished(self):
        self.task("task-half")
        self.hold("task-half", attributed=False)
        out, err, calls = self.run_watcher(lambda o, e: self.calls.exists() and "task-half" in self.calls.read_text())
        self.assertIn("task-half.txt", calls, "a lane's router that died before attributing it is repaired by the core")
        self.assertNotIn("held by a worker, not routing it: task-half.txt", err)

    def test_a_held_task_under_the_cores_own_dead_claim_is_still_replayed(self):
        self.task("task-mine")
        self.hold("task-mine")
        claims = self.ws / "state" / "task-event-handler-claims"
        claims.mkdir(parents=True)
        dead = subprocess.Popen(["true"])
        dead.wait()
        (claims / "task-mine.txt").write_text(
            f"{dead.pid}\nold-watcher\n{self.ws / 'tasks' / 'task-mine.txt'}\nfallback\n\n")
        out, err, calls = self.run_watcher(lambda o, e: self.calls.exists() and "task-mine" in self.calls.read_text())
        self.assertIn("task-mine.txt", calls, "the core finishes its own interrupted routing")
        self.assertNotIn("held by a worker, not routing it: task-mine.txt", err)

    def test_the_workers_own_watcher_still_announces_its_committed_task(self):
        # The skip is the core's: to the worker holding it, a committed task is exactly what to run.
        self.task("task-mine-w")
        self.hold("task-mine-w")
        out, err, _ = self.run_watcher(lambda o, e: "task-mine-w.txt" in o or "task-mine-w.txt" in e, worker=True)
        self.assertIn("TASK_FILE:", out)
        self.assertIn("/tasks/task-mine-w.txt", out)
        self.assertNotIn("held by a worker", err)

    @unittest.skipIf(os.geteuid() == 0, "root reads an unreadable folder")
    def test_deliveries_that_cannot_be_read_still_go_to_the_handler(self):
        self.task("task-unsure")
        self.hold("task-unsure")
        (self.ws / "deliveries").chmod(0)
        self.addCleanup((self.ws / "deliveries").chmod, 0o755)
        out, err, calls = self.run_watcher(lambda o, e: "TASK_FILE: task-unsure.txt" in o)
        self.assertIn("task-unsure.txt", calls)
        self.assertNotIn("held by a worker", err)


if __name__ == "__main__":
    unittest.main()
