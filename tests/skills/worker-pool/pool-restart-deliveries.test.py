#!/usr/bin/env python3
"""Interrupted deliveries traverse the real launcher, recovery CLI and session watcher."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path


def load_fixture():
    spec = importlib.util.spec_from_file_location(
        "restart_fixture", Path(__file__).with_name("pool-restart.test.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixture = load_fixture()
rem, REPO = fixture.rem, fixture.REPO
sys.path.insert(0, str(REPO / "tests"))
from _helpers.process_group import popen_bound

pd, wi, sw = rem.pd, rem.wi, rem.sw
import pool_router as router
from result_publish import publish_text
from util_paths import watcher_sentinel_path


class RestartDeliveries(unittest.TestCase):
    def setUp(self):
        self.h = fixture.RestartSessionContinuity(methodName="runTest")
        self.addCleanup(self.h.doCleanups)
        self.h.setUp()
        self.ws = self.h.ws
        self.logs = self.h.diagnostics / "restart-deliveries"
        self.logs.mkdir(exist_ok=True)
        self.folder = self.h.root / "project"
        self.folder.mkdir()
        first = wi.create_worker(self.ws, runtime="claude", cwd=str(self.folder),
                                 tmux_socket=str(self.h.socket))
        self.wid, self.sid = first["worker_id"], first["runtime_session_id"]
        wi.end_incarnation(self.ws, self.wid, first["incarnation_id"], "exited")
        sw.pr.register_worker(self.ws, self.wid, "alpha", room="test-room", runtime="claude")
        spawned = self.h.call_main(sw, ["--resume", self.sid, "--socket", str(self.h.socket)],
                                   "restart-deliveries/spawn.json")
        self.env = {**os.environ, **spawned["env"]}
        self.inbox = pd.deliveries_dir(self.ws, self.wid)
        self.name = wi.tmux_session_name(self.wid)
        self.watchers = []
        self.addCleanup(self.stop_watchers)
        self.evidence = {"synthetic_endpoint": True, "model_execution": False,
                         "started_at": time.time(), "watcher_commands": [], "recoveries": []}
        self.addCleanup(self.save_evidence)

    def save_evidence(self):
        self.evidence["finished_at"] = time.time()
        self.evidence["watchers_reaped"] = all(p.poll() is not None for p in self.watchers)
        (self.logs / "journey.json").write_text(json.dumps(self.evidence, indent=2))

    def stop_watchers(self):
        for proc in self.watchers:
            self.stop_watcher(proc)
        self.save_evidence()

    def stop_watcher(self, proc):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)

    def watch(self, label):
        log = self.logs / f"{label}.log"
        command = ["bash", str(REPO / "src/watch-tasks-stream.sh"),
                   "--role", "session", "--inbox", str(self.inbox)]
        with log.open("w") as stream:
            proc = popen_bound(command, cwd=REPO, env=self.env, stdin=subprocess.DEVNULL,
                               stdout=stream, stderr=subprocess.STDOUT)
        self.watchers.append(proc)
        sentinel = watcher_sentinel_path(self.ws / "state", instance=self.wid)

        def ready():
            self.assertIsNone(proc.poll(), log.read_text())
            return sentinel.is_file() and sentinel.read_text().strip() == str(proc.pid)

        self.h.wait_for(ready, seconds=15)
        time.sleep(float(self.env.get("SUTANDO_STANDBY_STOP_TIMEOUT", "2")) + 0.5)
        self.evidence["watcher_commands"].append({"argv": command, "pid": proc.pid,
                                                   "ready_at": time.time(), "log": log.name})
        return proc, log

    def announced(self, log):
        return [line.removeprefix("TASK_FILE: ").strip() for line in log.read_text().splitlines()
                if line.startswith("TASK_FILE: ")]

    def wait_announced(self, log, ids):
        paths = [str(pd.payload_path(self.ws, tid)) for tid in ids]
        self.h.wait_for(lambda: all(p in self.announced(log) for p in paths), seconds=15)
        for path in paths:
            self.assertEqual(self.announced(log).count(path), 1, log.read_text())

    def deliver(self, tid, *, accepted=False, legacy=False):
        payload = pd.payload_path(self.ws, tid)
        payload.parent.mkdir(exist_ok=True)
        payload.write_text(f"source: test\nchannel_id: test-room\ntask: original payload {tid}\n")
        self.assertEqual(router.route(self.ws, {"id": tid, "channel_id": "test-room"})
                         ["delivered"], [self.wid])
        entry = self.inbox / f"{tid}.txt"
        if accepted:
            entry = pd.accept(entry)
            if legacy:
                entry.rename(entry.with_suffix(".claimed"))
        return payload

    def interrupt(self):
        before = self.h.observe(self.name, self.sid, self.folder)
        os.kill(before["pid"], signal.SIGTERM)
        self.h.wait_for(lambda: not self.h.alive(before["pid"]), seconds=15)
        self.h.wait_for(lambda: sw.session_state(self.name, str(self.h.socket)) == "absent", seconds=15)
        return before

    def recover(self, mode):
        args = ["--restart", self.wid] if mode == "restart" else ["--" + mode]
        if mode == "sweep":
            now = time.time()
            rem.sup.save_state(self.ws, rem.ps.SupervisionState(last_sample_at=now - 1, workers={
                self.wid: rem.ps.WorkerEvidence(first_detected_at=now - 100, consecutive=1)}))
        out = self.h.call_main(rem, args, f"restart-deliveries/{mode}.json")
        if mode == "restart":
            self.assertEqual(out["result"], "restarted")
        else:
            recoveries = out["resume"]["restarted"] if mode == "resume" else out["recoveries"]
            self.assertEqual(recoveries[self.wid]["outcome"], rem.RECOVERED, out)
        after = self.h.observe(self.name, self.sid, self.folder)
        self.evidence["recoveries"].append({"mode": mode, "at": time.time(),
                                            "outcome": out, "process": after})
        return after

    def test_restart_resume_and_sweep_reoffer_only_unanswered_work(self):
        lineage, roster = wi.sessions(self.ws, self.wid), sw.pr.load_roster(self.ws)
        completed, protected = [], []
        for mode in ("restart", "resume", "sweep"):
            with self.subTest(mode=mode):
                watcher, before_log = self.watch(mode + "-before")
                tid = "task-" + mode
                payload = self.deliver(tid)
                self.wait_announced(before_log, [tid])
                accepted = pd.accept(self.inbox / f"{tid}.txt")
                if mode == "resume":
                    accepted.rename(accepted.with_suffix(".claimed"))
                    pd.result_path(self.ws, tid).write_text(" \n\t")
                if mode == "sweep":
                    pd.result_path(self.ws, tid).write_text("")
                body = payload.read_bytes()
                self.stop_watcher(watcher)
                pending = "task-pending-" + mode
                self.deliver(pending)
                if mode == "restart":
                    for kind in ("live", "archive", "terminal"):
                        done = "task-protected-" + kind
                        self.deliver(done, accepted=True, legacy=kind == "archive")
                        protected.append(done)
                        if kind == "terminal":
                            pd.mark_done(self.ws, self.wid, done, published=True)
                        else:
                            dst = pd.result_path(self.ws, done)
                            if kind == "archive":
                                dst.write_text(" \n")
                                dst = dst.parent / "archive" / dst.name
                            publish_text(dst, "synthetic previous answer")
                    held = pd.find(self.ws, self.wid, tid)
                    live = self.h.call_main(rem, ["--restart", self.wid], "restart-deliveries/live.json")
                    self.assertEqual(live["result"], "already-running")
                    self.assertEqual(pd.find(self.ws, self.wid, tid), held)
                if mode == "resume":
                    self.h.call_main(rem, ["--suspend", "test-stop"], "restart-deliveries/suspend.json")
                before = self.interrupt()
                if mode == "restart":
                    lock = self.inbox / pd.LOCK_NAME
                    lock.unlink()
                    lock.mkdir()
                    runs = wi.incarnations(self.ws, self.wid)
                    output = io.StringIO()
                    with redirect_stdout(output):
                        rc = rem.main(["--workspace", str(self.ws), "--repo", str(REPO),
                                       "--restart", self.wid])
                    failed = json.loads(output.getvalue())
                    self.assertEqual(rc, 1, failed)
                    self.assertEqual(failed["result"], "failed")
                    self.assertIn(".lock", failed["detail"]["why"])
                    self.assertEqual(sw.session_state(self.name, str(self.h.socket)), "absent")
                    self.assertEqual(len(wi.incarnations(self.ws, self.wid)), len(runs))
                    self.assertTrue((self.inbox / f"{tid}.accepted").exists())
                    self.evidence["reconciliation_failure"] = failed
                    lock.rmdir()
                after = self.recover(mode)
                self.assertNotEqual(before["pid"], after["pid"])
                self.assertEqual(payload.read_bytes(), body)
                self.assertEqual(router.committed_recipient(self.ws, tid), self.wid)
                self.assertEqual(wi.sessions(self.ws, self.wid), lineage)
                self.assertEqual(sw.pr.load_roster(self.ws), roster)
                self.assertEqual(wi.incarnations(self.ws, self.wid)[-1]["tmux"]["socket"], str(self.h.socket))
                watcher, after_log = self.watch(mode + "-after")
                self.wait_announced(after_log, [tid, pending])
                for done in protected + completed:
                    self.assertNotIn(str(pd.payload_path(self.ws, done)), self.announced(after_log))
                for done in (tid, pending):
                    pd.accept(self.inbox / f"{done}.txt")
                    pd.mark_done(self.ws, self.wid, done, published=False)
                    publish_text(pd.result_path(self.ws, done), "synthetic completed answer")
                    pd.mark_done(self.ws, self.wid, done, published=True)
                    completed.append(done)
                self.stop_watcher(watcher)
        self.interrupt()
        self.recover("restart")
        positive = "task-final-positive"
        self.deliver(positive)
        watcher, log = self.watch("completed-check")
        self.wait_announced(log, [positive])
        self.assertEqual(self.announced(log), [str(pd.payload_path(self.ws, positive))])
        self.assertEqual(pd.reconcile_interrupted(self.ws, self.wid), [])
        self.evidence["completed_tasks"] = completed
        self.evidence["protected_tasks"] = protected
        self.stop_watcher(watcher)


if __name__ == "__main__":
    unittest.main(verbosity=2)
