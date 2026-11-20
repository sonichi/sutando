"""The cloud-session adapter's side of the task-lifecycle contract, end to end through the adapter.

Gated like test_task_lifecycle.py (on by default; TASK_LIFECYCLE_CONTRACT=0 turns them off). Run:
    python3 packages/ag2-sparrow/tests/test_cloud_session_contract.py
"""
import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_claude_cloud_session_adapter import (FakeClock, Fake, ok, result, run_adapter,  # noqa: E402
                                               write_task)

from ag2_sparrow import holder_lock, result_disposal, result_ready, task_lifecycle as lc  # noqa: E402

# On now that the owner module has landed; TASK_LIFECYCLE_CONTRACT=0 turns the contract off.
CONTRACT = os.environ.get("TASK_LIFECYCLE_CONTRACT", "1") == "1"


# holder_lock keeps its lock dirs under the uid's home; these tests and their children use a temp home.
LOCK_HOME = tempfile.mkdtemp(prefix="sparrow-lock-home-")
holder_lock._home = lambda: LOCK_HOME
atexit.register(shutil.rmtree, LOCK_HOME, True)
SET_HOME = "from ag2_sparrow import holder_lock\nholder_lock._home = lambda: %r\n" % LOCK_HOME
PKG_ROOT = Path(__file__).resolve().parents[1]


def charges(work):
    return lc.RateJournal(Path(work) / "state" / "rate.jsonl").charges()


def sa_main(argv, env):
    from ag2_sparrow.adapters.claude_cloud_session import session_adapter as sa
    return sa.main(argv, env)


def claim(work, tid):
    return lc.ClaimStore(Path(work) / "state" / "claims").load(tid)


@unittest.skipUnless(CONTRACT, "the task-lifecycle contract is off (TASK_LIFECYCLE_CONTRACT=0)")
class AdapterLifecycleTest(unittest.TestCase):
    def crash_during_the_call(self, d, fake, clock):
        class Crash(BaseException):
            pass

        def send_then_crash(argv, **kw):
            clock.t += 10
            subprocess.run(argv, **kw)
            raise Crash()
        with self.assertRaises(Crash):
            run_adapter(d, fake, clock=clock, run=send_then_crash, once=1)
        self.assertEqual(claim(d, "task-c")["phase"], "CALLING")
        self.assertFalse((Path(d) / "results" / "task-c.txt").exists())

    def test_a_crash_during_the_call_is_never_resent_and_counts_at_the_restart(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Fake(d, [ok()])
            write_task(d, "task-c", "x", mtime=1000)
            first = FakeClock()
            self.crash_during_the_call(d, fake, first)
            write_task(d, "task-next", "x", mtime=2000)
            clock = FakeClock(first.t + 5)
            restart = clock.t
            starts = []

            def record(argv, **k):
                starts.append(clock.t)
                return subprocess.CompletedProcess(argv, 0, ok()["stdout"], "")
            run_adapter(d, fake, clock=clock, run=record, once=1, max_per_hour=1)
            self.assertEqual(len(fake.calls()), 1, "never resent")
            self.assertEqual(result(d, "task-c"), "This message may or may not have reached the cloud agent "
                                                  "(interrupted); it was not retried.\n")
            self.assertEqual(charges(d)[("task-c", 1)], restart, "counted at the restart, the latest it can be")
            self.assertEqual(len(starts), 1)
            self.assertGreaterEqual(starts[0], restart + 3600, "the next turn waits the hour")

    def test_a_crash_during_the_call_keeps_the_gap_for_the_next_turn(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Fake(d, [ok()])
            write_task(d, "task-c", "x", mtime=1000)
            first = FakeClock()
            self.crash_during_the_call(d, fake, first)
            write_task(d, "task-next", "x", mtime=2000)
            clock = FakeClock(first.t + 5)
            restart = clock.t
            starts = []

            def record(argv, **k):
                starts.append(clock.t)
                return subprocess.CompletedProcess(argv, 0, ok()["stdout"], "")
            run_adapter(d, fake, clock=clock, run=record, once=1, min_gap_seconds=2)
            self.assertGreaterEqual(starts[0], restart + 2)

    def test_a_result_claimed_at_once_by_the_production_drain_is_never_answered_twice(self):
        class Crash(BaseException):
            pass
        with tempfile.TemporaryDirectory() as d:
            fake = Fake(d, [ok(1), ok(2)])
            write_task(d, "task-q", "x")
            results = Path(d) / "results"
            live, archive = results / "task-q.txt", results / "archive"
            archive.mkdir(parents=True)
            stop, retired = threading.Event(), []

            def drain():
                while not stop.is_set():
                    if live.exists():
                        try:
                            _, generation = result_ready.identity_of(live)
                        except FileNotFoundError:
                            continue
                        retired.append(result_disposal.retire_generation(
                            results, live, generation, lambda m: None, archive, ["task-q-%d.txt" % len(retired)]))
                    time.sleep(0.001)
            t = threading.Thread(target=drain, daemon=True)
            t.start()
            real_save = lc.ClaimStore.save

            def crash_at_published(store, rec):
                if rec["phase"] == "PUBLISHED":
                    deadline = time.time() + 5
                    while not retired and time.time() < deadline:
                        time.sleep(0.001)
                    raise Crash()
                return real_save(store, rec)
            try:
                with mock.patch.object(lc.ClaimStore, "save", crash_at_published), self.assertRaises(Crash):
                    run_adapter(d, fake, once=1)
                run_adapter(d, fake, once=0)
                time.sleep(0.05)
            finally:
                stop.set()
                t.join()
            self.assertEqual(len(fake.calls()), 1)
            self.assertEqual(sorted(p.name for p in archive.iterdir()), ["task-q-0.txt"], "one result, ever")
            self.assertFalse(live.exists())
            self.assertEqual(claim(d, "task-q")["phase"], "SETTLED")
            self.assertEqual((Path(d) / "state" / "settled-ids").read_text(), "task-q\n")

    def test_an_existing_result_means_no_call_and_no_second_result(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Fake(d, [ok()])
            (Path(d) / "results" / "archive").mkdir(parents=True)
            (Path(d) / "results" / "archive" / "task-a.txt").write_text("[no-send]\n")
            write_task(d, "task-a", "x")
            run_adapter(d, fake, once=1)
            self.assertEqual(fake.calls(), [])
            self.assertFalse((Path(d) / "results" / "task-a.txt").exists())
            self.assertEqual(claim(d, "task-a")["phase"], "SETTLED")

    def test_a_settled_id_is_never_called_again(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Fake(d, [ok(1), ok(2)])
            write_task(d, "task-s", "x")
            run_adapter(d, fake, once=1)
            (Path(d) / "results" / "task-s.txt").unlink()
            write_task(d, "task-s", "again")
            run_adapter(d, fake)
            self.assertEqual(len(fake.calls()), 1)
            self.assertEqual((Path(d) / "state" / "settled-ids").read_text(), "task-s\n", "fenced by the ledger")

    def phases_of(self, fake_script, run=None, **kw):
        """The claim phases one task passes through, and its journal events, for a given CLI behaviour."""
        phases = []
        real = lc.ClaimStore.save
        with tempfile.TemporaryDirectory() as d:
            fake = Fake(d, fake_script)
            write_task(d, "task-p", "x")
            with mock.patch.object(lc.ClaimStore, "save", lambda st, rec: (phases.append(rec["phase"]),
                                                                            real(st, rec))):
                run_adapter(d, fake, once=1, backoff=1, **({"run": run} if run else {}), **kw)
            journal = [(e["attempt"], e["kind"]) for e in lc.RateJournal(Path(d) / "state" / "rate.jsonl").events]
            return phases, journal, result(d, "task-p"), len(fake.calls())

    def test_not_delivered_needs_no_receipt_and_a_proof_nothing_was_sent(self):
        phases, journal, body, calls = self.phases_of([{"rc": 1, "stderr": "getaddrinfo ENOTFOUND"}, ok(2)])
        self.assertEqual(phases, ["PREPARED", "CALLING", "NOT_DELIVERED", "PREPARED", "CALLING", "CONFIRMED",
                                  "RESULT_STAGED", "PUBLISHED", "SETTLED"])
        self.assertEqual(journal, [(1, "reserved"), (1, "unsent"), (2, "reserved"), (2, "queued")])
        self.assertEqual(calls, 2)
        phases, journal, body, calls = self.phases_of([dict(ok(3), rc=1, stderr="getaddrinfo ENOTFOUND")])
        self.assertNotIn("NOT_DELIVERED", phases, "a receipt means queued, whatever else the call says")
        self.assertIn("CONFIRMED", phases)
        self.assertEqual(calls, 1)
        phases, journal, body, calls = self.phases_of([{"rc": 1, "stderr": "Error: session not found"}])
        self.assertNotIn("NOT_DELIVERED", phases, "an error that proves nothing is not proof of non-delivery")
        self.assertIn("OUTCOME_UNKNOWN", phases)
        self.assertEqual(calls, 1)

    def test_an_ambiguous_outcome_is_outcome_unknown_charged_and_never_resent(self):
        def timeout(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        phases, journal, body, calls = self.phases_of([ok()], run=timeout)
        self.assertEqual(phases, ["PREPARED", "CALLING", "OUTCOME_UNKNOWN", "RESULT_STAGED", "PUBLISHED", "SETTLED"])
        self.assertEqual(journal, [(1, "reserved"), (1, "unknown")])
        self.assertIn("may or may not have reached the cloud agent (timeout)", body)

    def test_a_provider_child_outliving_the_adapter_produces_at_most_one_turn(self):
        with tempfile.TemporaryDirectory() as d:
            started, calls = Path(d) / "started", Path(d) / "calls"
            claude = Path(d) / "claude"
            claude.write_text("#!%s\nimport sys, time\nsys.stdin.read()\nopen(%r, 'a').write('started\\n')\n"
                              "time.sleep(1.5)\nopen(%r, 'a').write('call\\n')\n"
                              "print('{\"ok\": true, \"url\": \"u\"}')\n" % (sys.executable, str(started), str(calls)))
            claude.chmod(0o755)
            work = Path(d) / "work"
            write_task(work, "task-c", "x")
            argv = ["--work", str(work), "--session-id", "session_X", "--claude", str(claude), "--poll", "0.02"]
            env = {"PATH": "/usr/bin:/bin", "HOME": d}
            code = ("import sys\nsys.path.insert(0, %r)\n%sfrom ag2_sparrow.adapters.claude_cloud_session "
                    "import session_adapter as sa\nsys.exit(sa.main(%r, %r))\n"
                    % (str(PKG_ROOT), SET_HOME, argv + ["--idle-exit", "30"], env))
            adapter = subprocess.Popen([sys.executable, "-c", code], stderr=subprocess.PIPE, text=True)
            self.addCleanup(lambda: (adapter.kill(), adapter.wait(10)))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not started.exists():
                time.sleep(0.02)
            self.assertTrue(started.exists(), "the provider child started")
            os.kill(holder_lock.read_holder(str(work)).record["pid"], signal.SIGKILL)
            adapter.wait(10)
            while time.monotonic() < deadline + 5 and (not calls.exists() or not calls.read_text()):
                time.sleep(0.02)
            self.assertEqual(calls.read_text(), "call\n", "the orphaned provider child finished its one turn")
            self.assertEqual(claim(work, "task-c")["phase"], "CALLING")
            self.assertEqual(sa_main(argv + ["--idle-exit", "0.3"], env), 0, "a successor starts at once")
            self.assertEqual(calls.read_text(), "call\n", "and never resends")
            self.assertEqual(claim(work, "task-c")["phase"], "SETTLED")
            self.assertIn("may or may not have reached", result(work, "task-c"))

    def test_min_gap_counts_from_the_outcome_not_the_start(self):
        clock = FakeClock()
        starts = []

        def slow(argv, **kw):
            starts.append(clock.t)
            clock.t += 10
            return subprocess.CompletedProcess(argv, 0, ok(len(starts))["stdout"], "")
        with tempfile.TemporaryDirectory() as d:
            fake = Fake(d, [ok()])
            write_task(d, "task-1", "x", mtime=1000)
            write_task(d, "task-2", "x", mtime=2000)
            run_adapter(d, fake, clock=clock, run=slow, once=2, min_gap_seconds=2)
            self.assertEqual(len(starts), 2)
            self.assertGreaterEqual(starts[1], starts[0] + 10 + 2)
            self.assertEqual(sorted(charges(d).values()), [starts[0] + 10, starts[1] + 10])



REPO_SRC = Path(__file__).resolve().parents[3] / "src"
BRIDGE_FIXTURE = REPO_SRC / "remote-gateway-bridge.test.py"
FAKE_CLAUDE = "#!%s\nimport sys\nsys.stdin.read()\nprint('{\"ok\": true, \"url\": \"u\"}')\n"


def bounded_wait(predicate, what, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("timed out after %.0fs waiting for %s" % (timeout, what))


@unittest.skipUnless(CONTRACT, "the task-lifecycle contract is off (TASK_LIFECYCLE_CONTRACT=0 turns it off)")
@unittest.skipUnless(BRIDGE_FIXTURE.exists(), "the bridge's own broker fixture lives in the repository's src/")
class ProductionBridgeWitnessTest(unittest.TestCase):
    """The packaged bridge, as a subprocess, against the bridge's own in-process broker fixture."""

    def test_one_task_round_trips_through_the_packaged_bridge(self):
        import contextlib
        import importlib.util
        import io
        import signal
        from http.server import ThreadingHTTPServer
        from ag2_sparrow.adapters.claude_cloud_session import session_adapter as sa
        spec = importlib.util.spec_from_file_location("bridge_fixture", BRIDGE_FIXTURE)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        server = ThreadingHTTPServer(("127.0.0.1", 0), fixture.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        cloud, gateway = base / "cloud-session", base / "gateway-state"
        claude = base / "claude"
        claude.write_text(FAKE_CLAUDE % sys.executable)
        claude.chmod(0o755)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(base)}
        argv = ["--work", str(cloud), "--session-id", "session_X", "--claude", str(claude), "--poll", "0.05",
                "--gateway-state", str(gateway), "--result-mode", "echo"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sa.main(argv + ["--idle-exit", "0.1"], env), 0, "the adapter initialises its dir")
        bridge_env = dict(env, PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                          REMOTE_TASK_URL="http://127.0.0.1:%d" % server.server_address[1],
                          REMOTE_TASK_TOKEN="testtoken", REMOTE_TASK_POLL_WAIT="1",
                          AGENT_CONNECT_TASK_DIR=str(cloud / "tasks"), AGENT_CONNECT_RESULT_DIR=str(cloud / "results"),
                          AGENT_CONNECT_STATE_DIR=str(gateway))
        bridge = subprocess.Popen([sys.executable, "-m", "ag2_sparrow"], env=bridge_env, cwd=str(base),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop():
            if bridge.poll() is None:
                bridge.send_signal(signal.SIGTERM)
            try:
                bridge.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bridge.kill()
                bridge.wait(timeout=10)
        self.addCleanup(stop)
        task = cloud / "tasks" / (fixture.TASK["id"] + ".txt")
        bounded_wait(task.exists, "the packaged bridge to write the task")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sa.main(argv + ["--once", "1", "--idle-exit", "5"], env), 0)
        bounded_wait(lambda: any(r.get("id") == fixture.TASK["id"] for r in fixture.STATE["results"]),
                     "the packaged bridge to post the result to the broker")
        posted = [r for r in fixture.STATE["results"] if r.get("id") == fixture.TASK["id"]][0]
        self.assertIn("Sent to the cloud agent: u", json.dumps(posted))
        self.assertEqual(claim(cloud, fixture.TASK["id"])["phase"], "SETTLED")
        self.assertTrue(gateway.is_dir() and os.listdir(gateway), "the bridge's state lives under --gateway-state")
        self.assertEqual(sorted(n for n in os.listdir(cloud / "state") if not n.startswith(".")),
                         ["claims", "format", "rate.jsonl", "settled-ids"])

if __name__ == "__main__":
    unittest.main()
