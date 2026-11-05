#!/usr/bin/env python3
"""Real private tmux lifecycle; the supplied endpoint proves argv, not model continuity.

Run with the disposable runtime's environment.json loaded into the environment.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills/worker-pool/scripts"
sys.path.insert(0, str(SCRIPTS))
import create_worker as creator  # noqa: E402

import pool_remedy as rem  # noqa: E402
import pool_router as router  # noqa: E402

wi, sw, sup, pd, ps = rem.wi, rem.sw, rem.sup, rem.pd, rem.ps


def wait_until(predicate, description, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {description}")


def record(path, **event):
    with path.open("a") as stream:
        stream.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **event}) + "\n")


def product_child(mode, journal, barrier, argv):
    """Only timer installation and the post-launch synchronization are test seams."""
    original = sw.spawn
    launches = 0

    def logged_run(command, **kwargs):
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, **kwargs)
        record(journal, command=command, returncode=result.returncode,
               stdout=result.stdout[-12000:], stderr=result.stderr[-12000:])
        return result

    def spawn(*args, **kwargs):
        nonlocal launches
        result = original(*args, runner=logged_run, **kwargs)
        launches += 1
        record(journal, launched=result["worker_id"], session=result["runtime_session_id"])
        if barrier != "-" and launches == 1:
            Path(barrier + ".ready").write_text(json.dumps(result))
            wait_until(lambda: Path(barrier + ".release").exists(), "resume release", timeout=30)
        return result

    with mock.patch.object(sw, "ensure_remedy_timer", return_value={
            "ensured": False, "why": "timer installation excluded from lifecycle test"}), \
            mock.patch.object(sw, "spawn", side_effect=spawn):
        return (creator.main if mode == "create" else rem.main)(argv)


class ResumeSuspensionLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        socket = os.environ.get("SUTANDO_RUNTIME_SOCKET")
        if not socket or not (Path(socket).parent / "environment.json").is_file():
            raise unittest.SkipTest("requires the supplied disposable runtime environment")
        cls.runtime = Path(socket).parent
        cls.supplied = json.loads((cls.runtime / "environment.json").read_text())
        cls.env_base = {**os.environ, **cls.supplied, "PYTHONDONTWRITEBYTECODE": "1",
                        "SUTANDO_TELEMETRY": "0"}
        cls.scratch = tempfile.TemporaryDirectory(prefix="resume-", dir=cls.runtime)
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.root = Path(cls.scratch.name)
        cls.endpoint = cls.root / "endpoint"
        cls.endpoint.mkdir()
        subprocess.run(["cc", str(cls.runtime / "runtime-endpoint.c"),
                        "-o", str(cls.endpoint / "claude")],
                       env=cls.env_base, check=True, capture_output=True, timeout=30)

    def setUp(self):
        self.case = Path(tempfile.mkdtemp(prefix="case-", dir=self.root))
        self.ws = self.case / "workspace"
        self.ws.mkdir()
        self.socket = self.case / "tmux.sock"
        self.diagnostics = self.runtime / "diagnostics" / ("pool-resume-" + self.case.name)
        self.diagnostics.mkdir(parents=True)
        self.events = self.diagnostics / "events.jsonl"
        self.journal = self.diagnostics / "launches.jsonl"
        self.env = {**self.env_base, "PATH": str(self.endpoint) + os.pathsep + self.env_base["PATH"],
                    "SUTANDO_WORKSPACE": str(self.ws), "SUTANDO_WORKSPACE_DIR": str(self.ws),
                    "SUTANDO_DEFAULT_WORKSPACE": str(self.ws),
                    "SUTANDO_RESULTS_DIR": str(self.ws / "results"),
                    "SUTANDO_RUNTIME_STATE": str(self.ws / "state"),
                    "SUTANDO_RUNTIME_DB": str(self.ws / "state/runtime.sqlite"),
                    "SUTANDO_HA_DIR": str(self.ws / "state/ha"),
                    "SUTANDO_TMUX_SOCKET": str(self.socket),
                    "CLAUDE_CONFIG_DIR": str(self.case / "claude"),
                    "ANTHROPIC_BASE_URL": "http://factory-runtime.invalid",
                    "SUTANDO_NOTIFIER_GRACE_PERIOD": "300", "SUTANDO_NOTIFIER_ROLE_POLL": "1"}
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.children = []
        self.server = None
        self.addCleanup(self.stop_processes)
        log = (self.diagnostics / "tmux.log").open("w")
        self.addCleanup(log.close)
        command = ["tmux", "-D", "-f", "/dev/null", "-S", str(self.socket)]
        self.server = subprocess.Popen(command, env=self.env, cwd=REPO, stdout=log, stderr=log)
        record(self.events, test=self.id(), command=command, server_pid=self.server.pid,
               claim="synthetic endpoint: lifecycle and argument forwarding only")
        wait_until(lambda: self.socket.exists() or self.server.poll() is not None, "private tmux")
        self.assertIsNone(self.server.poll())
        self.tmux("set-option", "-g", "default-shell", "/bin/bash")
        # An empty foreground server reports "no current target", not session absence.
        self.tmux("new-session", "-d", "-s", "fixture-anchor", "sleep", "600")
        self.workers = [self.create(label) for label in ("First worker", "Second worker")]
        self.ids = [w["worker_id"] for w in self.workers]

    def run_command(self, command, *, check=True, timeout=30):
        result = subprocess.run([str(v) for v in command], env=self.env, cwd=REPO,
                                capture_output=True, text=True, timeout=timeout)
        record(self.events, command=[str(v) for v in command], returncode=result.returncode,
               stdout=result.stdout[-12000:], stderr=result.stderr[-12000:])
        if check:
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result

    def tmux(self, *args, check=True):
        return self.run_command(["tmux", "-S", self.socket, *args], check=check)

    def child_command(self, mode, *args, barrier="-"):
        return [sys.executable, str(Path(__file__).resolve()), "--product-child", mode,
                str(self.journal), str(barrier), "--workspace", str(self.ws),
                "--repo", str(REPO), *args]

    def create(self, label):
        cwd = self.case / label.replace(" ", "-")
        cwd.mkdir()
        out = self.run_command(self.child_command(
            "create", "--folder", str(cwd), "--label", label, "--runtime", "claude",
            "--socket", str(self.socket), "--json"))
        worker = json.loads(out.stdout)
        self.endpoint_process(worker["worker_id"], "--session-id", worker["runtime_session_id"])
        return worker

    def remedy(self, *args, workspace=None):
        return json.loads(self.run_command([
            sys.executable, SCRIPTS / "pool_remedy.py", "--workspace", workspace or self.ws,
            "--repo", REPO, *args]).stdout)

    def resume(self):
        return json.loads(self.run_command(self.child_command("remedy", "--resume")).stdout)

    def absent(self, wid):
        return sw.session_state(wi.tmux_session_name(wid), str(self.socket)) == "absent"

    def endpoint_process(self, wid, flag, session):
        name = wi.tmux_session_name(wid)
        pid = int(self.tmux("list-panes", "-t", "=" + name + ":0", "-F", "#{pane_pid}").stdout)
        args = self.run_command(["ps", "-p", pid, "-o", "args="]).stdout.strip()
        argv = shlex.split(args)
        self.assertEqual(Path(argv[0]).name, "claude")
        executable = self.run_command(["ps", "-p", pid, "-o", "comm="]).stdout.strip()
        self.assertEqual(Path(executable).name, "claude")
        self.assertEqual(argv[argv.index("--name") + 1], name)
        self.assertEqual(argv[argv.index(flag) + 1], session)
        pane = self.tmux("capture-pane", "-p", "-t", "=" + name + ":0.0").stdout
        self.assertIn("factory synthetic endpoint ready", pane)
        record(self.events, worker=wid, endpoint_pid=pid, argv=argv, ready=True)
        return pid

    def launches(self):
        return [row["launched"] for row in map(json.loads, self.journal.read_text().splitlines())
                if "launched" in row]

    def stop_workers(self):
        for wid in self.ids:
            if not self.absent(wid):
                pid = int(self.tmux("list-panes", "-t", "=" + wi.tmux_session_name(wid) + ":0",
                                    "-F", "#{pane_pid}").stdout)
                os.kill(pid, signal.SIGTERM)
                wait_until(lambda: self.absent(wid), "stopped worker")
        wait_until(lambda: self.tmux("list-sessions", "-F", "#{session_name}").stdout.strip() == "fixture-anchor",
                   "watcher supervisors to exit", timeout=35)

    def owned_state(self, wid):
        paths = [wi.sessions_path(self.ws, wid), sw.pr.roster_path(self.ws)]
        for directory in (pd.deliveries_dir(self.ws, wid), self.ws / "tasks",
                          self.ws / "state/attribution", wi.worker_dir(self.ws, wid) / "done"):
            paths.extend(p for p in directory.rglob("*") if p.is_file())
        return {str(p.relative_to(self.ws)): p.read_bytes() for p in paths}

    def delivery_evidence(self, wid):
        for task_id in ("task-pending", "task-accepted"):
            payload = pd.payload_path(self.ws, task_id)
            payload.parent.mkdir(exist_ok=True)
            payload.write_text(f"source: test\npriority: normal\ntask: synthetic {task_id}\n")
            self.assertEqual(router.deliver_one(self.ws, wid, task_id), "delivered")
        pd.accept(pd.find(self.ws, wid, "task-accepted"))
        pd.mark_done(self.ws, wid, "task-accepted", published=False)
        self.assertEqual(len(pd.pending(self.ws, wid)), 1)
        self.assertEqual(len(pd.accepted(self.ws, wid)), 1)

    def test_renewed_suspension_skips_next_worker_and_later_resume_recovers_once(self):
        evidence = ps.WorkerEvidence(watcher_consecutive=3, watcher_first_detected_at=10.0,
                                     rearm_issued_at=20.0, wedge_consecutive=2,
                                     wedge_first_detected_at=30.0, last_pane_id="old-pane")
        sup.save_state(self.ws, ps.SupervisionState(last_sample_at=time.time(),
                                                  workers={wid: evidence for wid in self.ids}))
        self.remedy("--suspend", "initial-stop")
        self.stop_workers()
        order = list(sup.observe(self.ws, time.time()))
        first, skipped = order
        self.delivery_evidence(skipped)
        owned = self.owned_state(skipped)
        runs = wi.incarnations_path(self.ws, skipped).read_bytes()
        current = wi.current_path(self.ws, skipped).read_bytes()
        launched = self.launches()
        barrier = self.case / "resume"
        stdout = (self.diagnostics / "resume.stdout").open("w+")
        stderr = (self.diagnostics / "resume.stderr").open("w+")
        self.addCleanup(stdout.close)
        self.addCleanup(stderr.close)
        command = self.child_command("remedy", "--resume", barrier=barrier)
        child = subprocess.Popen(command, env=self.env, cwd=REPO, stdout=stdout, stderr=stderr)
        self.children.append((child, barrier))
        record(self.events, command=command, pid=child.pid)
        try:
            wait_until(lambda: Path(str(barrier) + ".ready").exists() or child.poll() is not None,
                       "first successful launch")
            self.assertIsNone(child.poll(), "resume exited before its post-launch barrier")
            ready = json.loads(Path(str(barrier) + ".ready").read_text())
            self.assertEqual(ready["worker_id"], first)
            first_pid = self.endpoint_process(first, "--resume", ready["runtime_session_id"])
            self.assertTrue(self.absent(skipped))
            self.remedy("--suspend", "renewed-stop")
            marker = rem.suspended_path(self.ws).read_bytes()
            self.assertIn(skipped, json.loads(marker)["stopped"])
        finally:
            Path(str(barrier) + ".release").touch()
        self.assertEqual(child.wait(timeout=30), 0)
        stdout.seek(0)
        result = json.load(stdout)
        record(self.events, resume=result, returncode=child.returncode)
        self.assertEqual(result["resume"]["restarted"][skipped],
                         {"worker_id": skipped, "outcome": rem.SUSPENDED})
        self.assertEqual(result["resume"]["restarted"][first]["outcome"], rem.RECOVERED)
        self.assertEqual(self.launches(), launched + [first])
        for _ in range(2):
            self.assertTrue(self.absent(skipped))
            self.assertEqual(rem.suspended_path(self.ws).read_bytes(), marker)
            self.assertEqual(self.owned_state(skipped), owned)
            self.assertEqual(wi.incarnations_path(self.ws, skipped).read_bytes(), runs)
            self.assertEqual(wi.current_path(self.ws, skipped).read_bytes(), current)
            self.assertEqual(sup.load_state(self.ws).workers[skipped], evidence)
            self.assertEqual(self.endpoint_process(first, "--resume", ready["runtime_session_id"]), first_pid)
            self.assertTrue(self.remedy("--sweep")["suspended"].startswith("renewed-stop"))
        self.assertEqual(self.launches(), launched + [first])
        self.assertEqual(sup.load_state(self.ws).workers[skipped], evidence)
        session = wi.sessions(self.ws, skipped)[-1]["session_id"]
        later = self.resume()
        self.assertEqual(list(later["resume"]["restarted"]), [skipped])
        self.assertEqual(later["resume"]["restarted"][skipped]["outcome"], rem.RECOVERED)
        self.assertIsNone(rem.suspension(self.ws))
        self.assertEqual(self.launches(), launched + [first, skipped])
        self.assertEqual(len(wi.incarnations(self.ws, skipped)), len(json.loads(runs)["incarnations"]) + 1)
        self.assertEqual(self.owned_state(skipped), owned)
        self.endpoint_process(skipped, "--resume", session)
        self.assertEqual(self.endpoint_process(first, "--resume", ready["runtime_session_id"]), first_pid)
        self.assertEqual(self.resume()["resume"]["restarted"], {})
        self.assertEqual(self.launches(), launched + [first, skipped])

    def test_both_recover_without_renewal_even_with_foreign_workspace_suspended(self):
        self.remedy("--suspend", "local-stop")
        self.stop_workers()
        foreign = self.case / "other-workspace"
        self.remedy("--suspend", "foreign-stop", workspace=foreign)
        marker = rem.suspended_path(foreign).read_bytes()
        before = self.launches()
        result = self.resume()["resume"]["restarted"]
        self.assertEqual(set(result), set(self.ids))
        self.assertEqual({row["outcome"] for row in result.values()}, {rem.RECOVERED})
        self.assertCountEqual(self.launches()[len(before):], self.ids)
        self.assertEqual(rem.suspended_path(foreign).read_bytes(), marker)
        for worker in self.workers:
            self.endpoint_process(worker["worker_id"], "--resume", worker["runtime_session_id"])

    def test_owner_paused_worker_stays_paused(self):
        paused = self.ids[1]
        marker = wi.worker_dir(self.ws, paused) / sup.PAUSED_MARKER
        marker.write_text("owner pause\n")
        before = wi.incarnations_path(self.ws, paused).read_bytes()
        self.remedy("--suspend", "local-stop")
        self.stop_workers()
        result = self.resume()["resume"]["restarted"]
        self.assertEqual(list(result), [self.ids[0]])
        self.assertEqual(result[self.ids[0]]["outcome"], rem.RECOVERED)
        self.assertTrue(self.absent(paused))
        self.assertEqual(marker.read_text(), "owner pause\n")
        self.assertEqual(wi.incarnations_path(self.ws, paused).read_bytes(), before)
        self.assertEqual(self.launches().count(paused), 1)

    def stop_processes(self):
        for child, barrier in self.children:
            Path(str(barrier) + ".release").touch()
            try:
                child.wait(timeout=35)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)
        if self.server is None:
            return

        def descendants(pid):
            result = self.run_command(["pgrep", "-P", pid], check=False)
            self.assertIn(result.returncode, (0, 1), result.stderr)
            children = [int(line) for line in result.stdout.splitlines()]
            return [nested for child in children for nested in [*descendants(child), child]]

        def gone(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False

        try:
            for pid in descendants(self.server.pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                wait_until(lambda: gone(pid), f"reaped descendant {pid}", timeout=5)
            wait_until(lambda: not descendants(self.server.pid), "empty tmux process tree")
        finally:
            if self.server.poll() is None:
                self.tmux("kill-server", check=False)
            self.server.wait(timeout=10)
            record(self.events, cleanup="foreground tmux reaped", returncode=self.server.returncode)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--product-child":
        raise SystemExit(product_child(sys.argv[2], Path(sys.argv[3]), sys.argv[4], sys.argv[5:]))
    unittest.main(verbosity=2)
