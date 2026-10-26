#!/usr/bin/env python3
"""Tests for src/workspace_lock.py (MC1 atomic per-workspace role lock).

Covers acquire on absent / idempotent re-acquire / defer-on-fresh-holder /
reap-on-stale-holder / corrupt-lock-reaped, heartbeat (holder vs not), release
(only-if-ours), and a real-subprocess concurrency test asserting O_EXCL yields
exactly one winner. Liveness is heartbeat-freshness, narrowed by one pid rule:
a same-host holder whose pid is gone is reaped at once. Tests that need a LIVE
holder therefore plant a pid that is really running (_live_pid); the host label
is pinned for determinism.
"""
from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "src" / "workspace_lock.py"
HOST = "testhost"
os.environ["SUTANDO_HOST_LABEL"] = HOST


def _live_pid() -> int:
    """A running process that is not us: the test runner's parent."""
    return os.getppid()


# Never a real process: every test planting it stubs the platform probe for it.
FAKE_PID = 424_242


def _load() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("workspace_lock", SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


class LockTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _probing(self, table):
        """The platform probe answering `table[pid]` for the pids it names, the real one otherwise."""
        real = self.mod._probe
        return unittest.mock.patch.object(self.mod, "_probe", lambda pid: table[pid] if pid in table else real(pid))

    def _lock_path(self, role="gw"):
        return self.ws / "state" / "locks" / f"{role}.lock"

    def _plant(self, role, pid, hb_age_s, host=HOST):
        p = self._lock_path(role)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "role": role, "pid": pid, "host": host, "workspace": str(self.ws),
            "acquired_at": int(time.time()) - hb_age_s,
            "heartbeat_at": int(time.time()) - hb_age_s, "schema_version": 1,
        }))

    def test_acquire_absent(self):
        r = self.mod.acquire("gw", self.ws)
        self.assertEqual(r.status, "acquired")
        data = json.loads(self._lock_path().read_text())
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["role"], "gw")
        self.assertEqual(data["host"], HOST)

    def test_the_record_names_the_holders_start_time_where_proc_exists(self):
        proc = self.ws / "proc"
        (proc / "sys" / "kernel" / "random").mkdir(parents=True)
        (proc / "sys" / "kernel" / "random" / "boot_id").write_text("b-1\n")
        (proc / "77").mkdir()
        rest = " ".join(["S"] + ["0"] * 18 + ["12345", "0"])
        (proc / "77" / "stat").write_bytes(f"77 (a) b) {rest}".encode())
        self.assertEqual(self.mod.proc_start(77, proc), "b-1:12345")
        self.assertIsNone(self.mod.proc_start(78, proc), "a gone process has none")
        with unittest.mock.patch.object(self.mod, "_darwin_sysctl", lambda mib: None), \
                unittest.mock.patch.object(self.mod, "_IS_WINDOWS", False):
            self.assertIsNone(self.mod.proc_start(77, self.ws / "absent"), "no proc root and no platform fallback")
        start = self.mod.proc_start(os.getpid())
        if sys.platform in ("linux", "darwin"):
            self.assertIsNotNone(start, "macOS reads it through sysctl")
        self.mod.acquire("gw", self.ws)
        data = json.loads(self._lock_path().read_text())
        self.assertEqual(data.get("proc_start"), start)
        if start is None:
            self.assertNotIn("proc_start", data)
        self.mod.heartbeat("gw", self.ws)
        self.assertEqual(json.loads(self._lock_path().read_text()).get("proc_start"), start)

    def test_acquire_idempotent(self):
        self.assertEqual(self.mod.acquire("gw", self.ws).status, "acquired")
        self.assertEqual(self.mod.acquire("gw", self.ws).status, "acquired")

    def test_defer_on_fresh_holder(self):
        live = _live_pid()
        self._plant("gw", pid=live, hb_age_s=5)            # fresh + running
        r = self.mod.acquire("gw", self.ws)
        self.assertEqual(r.status, "deferred")
        self.assertEqual(r.holder["pid"], live)
        # holder untouched
        self.assertEqual(json.loads(self._lock_path().read_text())["pid"], live)

    def test_dead_same_host_holder_is_reaped_immediately(self):
        self._plant("gw", pid=FAKE_PID, hb_age_s=1)     # fresh, but nobody behind it
        with self._probing({FAKE_PID: ("dead", None)}):
            r = self.mod.acquire("gw", self.ws)
        self.assertEqual(r.status, "reaped")
        self.assertEqual(json.loads(self._lock_path().read_text())["pid"], os.getpid())

    def test_live_same_host_holder_is_still_deferred(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self._plant("gw", pid=child.pid, hb_age_s=1)
            self.assertEqual(self.mod.acquire("gw", self.ws).status, "deferred")
        finally:
            child.kill(); child.wait()

    def test_holder_alive_under_another_uid_is_deferred(self):
        self._plant("gw", pid=FAKE_PID, hb_age_s=1)
        with self._probing({FAKE_PID: ("unknown", None)}):
            self.assertEqual(self.mod.acquire("gw", self.ws).status, "deferred")

    def test_the_posix_probe_reads_eperm_as_unknown_and_esrch_as_dead(self):
        def kill(err):
            def raise_(pid, sig):
                raise err
            return raise_
        self.mod._IS_WINDOWS = False
        self.mod.proc_start = lambda pid, proc=None: None
        real_os = self.mod.os
        for err, want in ((PermissionError(), ("unknown", None)), (ProcessLookupError(), ("dead", None))):
            self.mod.os = types.SimpleNamespace(**{k: getattr(real_os, k) for k in dir(real_os) if not k.startswith("__")})
            self.mod.os.kill = kill(err)
            self.assertEqual(self.mod._probe(FAKE_PID), want, type(err).__name__)
        self.mod.os = real_os

    def test_dead_pid_on_another_host_follows_heartbeat_only(self):
        dead = FAKE_PID  # another host's pid is never probed
        self._plant("gw", pid=dead, hb_age_s=1, host="otherhost")
        self.assertEqual(self.mod.acquire("gw", self.ws).status, "deferred")
        self._plant("gw", pid=dead, hb_age_s=99999, host="otherhost")
        self.assertEqual(self.mod.acquire("gw", self.ws).status, "reaped")

    def test_reused_pid_reads_alive_and_falls_back_to_heartbeat(self):
        # Residual risk, pinned: no start time is recorded, so a dead holder whose
        # pid now names an unrelated live process is indistinguishable from live.
        self._plant("gw", pid=_live_pid(), hb_age_s=1)
        self.assertEqual(self.mod.acquire("gw", self.ws).status, "deferred")
        self._plant("gw", pid=_live_pid(), hb_age_s=99999)
        self.assertEqual(self.mod.acquire("gw", self.ws).status, "reaped")

    def test_unprobeable_pid_is_not_reaped_while_fresh(self):
        for bad in (0, -1, True, "123", None):
            with self.subTest(pid=bad):
                self._plant("gw", pid=bad, hb_age_s=1)
                self.assertEqual(self.mod.acquire("gw", self.ws).status, "deferred")

    def test_retained_dead_holder_ages_out_by_heartbeat(self):
        p = self._lock_path()
        self._plant("gw", pid=FAKE_PID, hb_age_s=1)  # a retained holder is never probed
        d = json.loads(p.read_text()); d["retained"] = True; p.write_text(json.dumps(d))
        self.assertEqual(self.mod.acquire("gw", self.ws).status, "deferred")
        d["heartbeat_at"] -= 99999; p.write_text(json.dumps(d))
        self.assertEqual(self.mod.acquire("gw", self.ws).status, "reaped")

    def test_retain_marks_only_our_own_lock(self):
        self._plant("gw", pid=_live_pid(), hb_age_s=1)
        self.assertFalse(self.mod.retain("gw", self.ws))
        self.assertNotIn("retained", json.loads(self._lock_path().read_text()))
        self._lock_path().unlink()
        self.assertEqual(self.mod.acquire("gw", self.ws).status, "acquired")
        self.assertTrue(self.mod.retain("gw", self.ws))
        self.assertTrue(json.loads(self._lock_path().read_text())["retained"])

    def test_retained_lock_survives_its_holder_exiting(self):
        code = (f"import sys; sys.path.insert(0, {str(SCRIPT.parent)!r});"
                "import workspace_lock as w;"
                f"assert w.acquire('gw', w.Path({str(self.ws)!r})).status == 'acquired';"
                f"assert w.retain('gw', w.Path({str(self.ws)!r}))")
        env = dict(os.environ); env["SUTANDO_HOST_LABEL"] = HOST
        subprocess.run([sys.executable, "-c", code], check=True, env=env)
        self.assertEqual(self.mod.acquire("gw", self.ws).status, "deferred")

    def test_reap_on_stale_holder(self):
        self._plant("gw", pid=999999, hb_age_s=99999)      # stale
        r = self.mod.acquire("gw", self.ws)
        self.assertEqual(r.status, "reaped")
        self.assertEqual(json.loads(self._lock_path().read_text())["pid"], os.getpid())

    def test_corrupt_lock_is_reaped(self):
        p = self._lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{garbage")
        r = self.mod.acquire("gw", self.ws)
        self.assertIn(r.status, ("reaped", "acquired"))
        self.assertEqual(json.loads(p.read_text())["pid"], os.getpid())

    def test_heartbeat_refreshes_when_holder(self):
        self.mod.acquire("gw", self.ws)
        p = self._lock_path()
        d = json.loads(p.read_text()); d["heartbeat_at"] -= 60; p.write_text(json.dumps(d))
        old = json.loads(p.read_text())["heartbeat_at"]
        self.assertTrue(self.mod.heartbeat("gw", self.ws))
        self.assertGreater(json.loads(p.read_text())["heartbeat_at"], old)

    def test_heartbeat_false_when_not_holder(self):
        self._plant("gw", pid=999999, hb_age_s=5)
        self.assertFalse(self.mod.heartbeat("gw", self.ws))

    def test_release_only_removes_ours(self):
        self.mod.acquire("gw", self.ws)
        self.assertTrue(self._lock_path().exists())
        self.mod.release("gw", self.ws)
        self.assertFalse(self._lock_path().exists())
        # a lock owned by someone else is not released by us
        self._plant("gw", pid=999999, hb_age_s=5)
        self.mod.release("gw", self.ws)
        self.assertTrue(self._lock_path().exists())

    def test_roles_are_independent(self):
        self.assertEqual(self.mod.acquire("gateway-bridge", self.ws).status, "acquired")
        # different role → not blocked by the first
        self.assertEqual(self.mod.acquire("supervisor", self.ws).status, "acquired")

    def test_concurrent_acquire_single_winner(self):
        """Real race: N processes acquire the same role; exactly 1 wins. Each
        racer stays alive until all have tried, so the winner is a live holder."""
        done = self.ws / "all-tried"
        code = (f"import sys, time; sys.path.insert(0, {str(SCRIPT.parent)!r});"
                "import workspace_lock as w; from pathlib import Path;"
                f"r = w.acquire('race', Path({str(self.ws)!r}));"
                "print(r.status, flush=True);"
                f"[time.sleep(0.02) for _ in iter(lambda: Path({str(done)!r}).exists(), True)]")
        env = dict(os.environ); env["SUTANDO_HOST_LABEL"] = HOST
        procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                  text=True, env=env) for _ in range(8)]
        try:
            results = [p.stdout.readline().strip() for p in procs]
        finally:
            done.write_text("1")
            for p in procs:
                p.wait(10)
        acquired = [o for o in results if o in ("acquired", "reaped")]
        deferred = [o for o in results if o == "deferred"]
        self.assertEqual(len(acquired), 1, results)      # exactly one poller
        self.assertEqual(len(deferred), 7, results)


class CliTest(unittest.TestCase):
    """Exercise the CLI main() dispatch in-process (the subprocess concurrency
    test runs in child processes that don't count toward coverage)."""

    def setUp(self):
        self.mod = _load()
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *argv):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.mod.main([*argv, "--workspace", str(self.ws)])
        return rc, out.getvalue()

    def _lock_path(self, role="r"):
        return self.ws / "state" / "locks" / f"{role}.lock"

    def test_cli_acquire_then_status_release(self):
        rc, out = self._run("acquire", "--role", "r")
        self.assertEqual(rc, 0)
        self.assertIn("acquired", out)
        rc, out = self._run("status", "--role", "r")
        self.assertEqual(rc, 0)
        self.assertIn("\"role\": \"r\"", out)
        rc, _ = self._run("heartbeat", "--role", "r")
        self.assertEqual(rc, 0)                 # we hold it
        rc, _ = self._run("release", "--role", "r")
        self.assertEqual(rc, 0)
        self.assertFalse(self._lock_path().exists())

    def test_cli_acquire_deferred_exit3(self):
        p = self._lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"role": "r", "pid": _live_pid(), "host": HOST,
                                 "heartbeat_at": int(time.time()), "schema_version": 1}))
        rc, out = self._run("acquire", "--role", "r")
        self.assertEqual(rc, 3)
        self.assertIn("deferred", out)

    def test_cli_acquire_reap_via_stale_seconds(self):
        p = self._lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"role": "r", "pid": 999999, "host": HOST,
                                 "heartbeat_at": int(time.time()) - 10, "schema_version": 1}))
        rc, out = self._run("acquire", "--role", "r", "--stale-seconds", "1")
        self.assertEqual(rc, 0)                 # 10s old > 1s window → reaped
        self.assertIn("reaped", out)

    def test_cli_heartbeat_not_holder_exit1(self):
        p = self._lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"role": "r", "pid": 999999, "host": HOST,
                                 "heartbeat_at": int(time.time()), "schema_version": 1}))
        rc, _ = self._run("heartbeat", "--role", "r")
        self.assertEqual(rc, 1)

    def test_cli_status_absent_is_empty(self):
        rc, out = self._run("status", "--role", "nope")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_read_holder_and_resolve_workspace(self):
        self.mod.acquire("r", self.ws)
        self.assertEqual(self.mod.read_holder("r", self.ws)["role"], "r")
        self.assertIsNone(self.mod.read_holder("absent", self.ws))
        # non-override branch resolves the real workspace to a Path
        self.assertIsInstance(self.mod._resolve_workspace(None), Path)

    def test_repr(self):
        self.assertIn("LockResult", repr(self.mod.LockResult("acquired")))

    def test_missing_heartbeat_is_stale(self):
        # a holder with NO heartbeat_at field is treated as stale → reaped
        p = self._lock_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"role": "r", "pid": 999999, "host": HOST,
                                 "schema_version": 1}))   # no heartbeat_at
        self.assertEqual(self.mod.acquire("r", self.ws).status, "reaped")

    def test_heartbeat_does_not_clobber_a_reaped_owner(self):
        """P1 regression: a stale holder that resumes into heartbeat() must NOT
        overwrite the owner that reaped it. We acquire as ourselves, then
        simulate another process taking over (different pid, fresh), then
        heartbeat() — it must return False and leave the new owner intact."""
        self.assertEqual(self.mod.acquire("r", self.ws).status, "acquired")
        p = self._lock_path()
        taken = json.loads(p.read_text())
        taken["pid"] = 999999                      # a different process now owns it
        taken["heartbeat_at"] = int(time.time())   # freshly
        p.write_text(json.dumps(taken))
        self.assertFalse(self.mod.heartbeat("r", self.ws))     # we no longer hold it
        self.assertEqual(json.loads(p.read_text())["pid"], 999999)  # owner untouched

    def test_idempotent_reacquire_preserves_generation(self):
        r1 = self.mod.acquire("r", self.ws)
        gen1 = json.loads(self._lock_path().read_text())["acquired_at"]
        r2 = self.mod.acquire("r", self.ws)
        self.assertEqual(r2.status, "acquired")
        self.assertEqual(json.loads(self._lock_path().read_text())["acquired_at"], gen1)


class LeaseReaderTest(unittest.TestCase):
    """read_lease / lease_is_live / lease_process: who holds a role, for a caller about to signal it."""

    setUp, tearDown, _lock_path, _plant = LockTest.setUp, LockTest.tearDown, LockTest._lock_path, LockTest._plant
    _probing = LockTest._probing

    def test_read_lease_distinguishes_absent_from_malformed(self):
        self.assertIsNone(self.mod.read_lease("r", self.ws))
        p = self._lock_path("r")
        p.parent.mkdir(parents=True, exist_ok=True)
        for body, cause in (("garbage", "not a lease record"), ("[1]", "not a lease record"),
                            (json.dumps({"pid": True, "host": HOST}), "no valid pid"),
                            (json.dumps({"pid": 5}), "no host")):
            p.write_text(body)
            with self.assertRaises(self.mod.LeaseIndeterminate, msg=body) as cm:
                self.mod.read_lease("r", self.ws)
            self.assertIn(cause, str(cm.exception))
        p.unlink()
        self.mod.acquire("r", self.ws)
        self.assertEqual(self.mod.read_lease("r", self.ws)["pid"], os.getpid(), "the writer's own record")

    def test_lease_is_live_is_acquires_decision(self):
        for pid, age, host in ((_live_pid(), 0, HOST), (_live_pid(), 10_000, HOST), (FAKE_PID, 0, HOST),
                               (FAKE_PID, 0, "otherhost")):
            self._plant("r", pid, age, host)
            with self._probing({FAKE_PID: ("dead", None)}):
                live = self.mod.lease_is_live(self.mod.read_lease("r", self.ws))
                self.assertEqual(live, self.mod.acquire("r", self.ws).status == "deferred", (pid, age, host))
            self._lock_path("r").unlink()

    def test_lease_process_proves_the_writer_or_refuses(self):
        self.mod.acquire("r", self.ws)
        self.assertEqual(self.mod.lease_process(self.mod.read_lease("r", self.ws)), os.getpid(), "start time matches")
        with self._probing({FAKE_PID: ("dead", None)}):
            self.assertEqual(self.mod.lease_process({"pid": FAKE_PID, "host": HOST}), 0, "gone")
        live = _live_pid()
        if self.mod.proc_start(live):
            self.assertEqual(self.mod.lease_process({"pid": live, "host": HOST, "proc_start": "other-boot:1"}), 0,
                             "a reused pid is not the writer")
        for holder, cause in (({"pid": live, "host": HOST}, "records no start time"),
                              ({"pid": live, "host": "otherhost", "proc_start": "x"}, "host 'otherhost'"),
                              ({"pid": 0, "host": HOST}, "no valid pid")):
            with self.assertRaises(self.mod.LeaseIndeterminate) as cm:
                self.mod.lease_process(holder)
            self.assertIn(cause, str(cm.exception))


class LeaseBindingTest(unittest.TestCase):
    """A lease names the role, workspace and schema it was written for, and its writer's generation."""

    setUp, tearDown, _lock_path, _plant = LockTest.setUp, LockTest.tearDown, LockTest._lock_path, LockTest._plant
    _probing = LockTest._probing

    def _written(self, role="r"):
        self.mod.acquire(role, self.ws)
        return json.loads(self._lock_path(role).read_text())

    def test_a_record_for_another_role_workspace_or_schema_is_indeterminate(self):
        rec = self._written("alpha")
        beta = self._lock_path("beta")
        for change, cause in (({}, "for role 'alpha', not 'beta'"),
                              ({"role": "beta", "workspace": str(self.ws / "other")}, "another workspace"),
                              ({"role": "beta", "workspace": None}, "another workspace"),
                              ({"role": "beta", "schema_version": 2}, "not schema_version 1")):
            beta.write_text(json.dumps(dict(rec, **change)))
            with self.assertRaises(self.mod.LeaseIndeterminate, msg=cause) as cm:
                self.mod.read_lease("beta", self.ws)
            self.assertIn(cause, str(cm.exception))
        beta.write_text(json.dumps(dict(rec, role="beta")))
        self.assertEqual(self.mod.read_lease("beta", self.ws)["pid"], os.getpid(), "bound to beta, it reads")
        linked = Path(self._tmp.name + "-link")
        os.symlink(self.ws, linked)
        try:
            self.assertEqual(self.mod.read_lease("alpha", linked)["pid"], os.getpid(), "the same directory by another path")
        finally:
            linked.unlink()

    def test_an_unreadable_lease_file_is_indeterminate(self):
        self._lock_path("r").mkdir(parents=True)
        with self.assertRaises(self.mod.LeaseIndeterminate) as cm:
            self.mod.read_lease("r", self.ws)
        self.assertIn("unreadable", str(cm.exception))

    def test_ownership_needs_the_writers_start_time_not_just_its_pid(self):
        if not self.mod.proc_start(os.getpid()):
            self.skipTest("no start time on this platform")
        rec = self._written()
        self._lock_path("r").write_text(json.dumps(dict(rec, proc_start="another-generation:1")))
        self.assertFalse(self.mod.heartbeat("r", self.ws), "an earlier process with our pid wrote it")
        self.assertFalse(self.mod.retain("r", self.ws))
        self.mod.release("r", self.ws)
        self.assertTrue(self._lock_path("r").exists(), "release leaves another generation's lease")
        self.assertEqual(self.mod.lease_process(self.mod.read_lease("r", self.ws)), 0, "not the writer")
        self.assertEqual(self.mod.acquire("r", self.ws).status, "reaped", "a reused pid's lease is a dead holder's")
        self.assertTrue(self.mod.heartbeat("r", self.ws))

    def test_a_same_pid_record_from_another_host_is_never_ours(self):
        rec = self._written()
        self._lock_path("r").write_text(json.dumps(dict(rec, host="otherhost")))
        self.assertFalse(self.mod.heartbeat("r", self.ws))
        self.assertFalse(self.mod.retain("r", self.ws))
        self.assertEqual(self.mod.acquire("r", self.ws).status, "deferred", "fresh on another host")
        with self.assertRaises(self.mod.LeaseIndeterminate):
            self.mod.lease_process(self.mod.read_lease("r", self.ws))

    def test_a_live_pid_with_another_start_time_is_reaped_at_once(self):
        live = _live_pid()
        if not self.mod.proc_start(live):
            self.skipTest("no start time on this platform")
        self._plant("r", live, 1)
        rec = json.loads(self._lock_path("r").read_text())
        self._lock_path("r").write_text(json.dumps(dict(rec, proc_start="another-generation:1")))
        self.assertEqual(self.mod.acquire("r", self.ws).status, "reaped")

    def test_lease_stale_in(self):
        self.assertEqual(self.mod.lease_stale_in({"heartbeat_at": int(time.time()) - 500}), 0.0)
        self.assertEqual(self.mod.lease_stale_in({}), 0.0)
        self.assertAlmostEqual(self.mod.lease_stale_in({"heartbeat_at": time.time()}, 30), 30, delta=1)


class WindowsIdentityTest(unittest.TestCase):
    """On Windows the writer and the reader both take the start token from outbox.process_identity."""

    setUp, tearDown, _lock_path = LockTest.setUp, LockTest.tearDown, LockTest._lock_path

    def _stub(self, table):
        sys.path.insert(0, str(SCRIPT.parent))
        import outbox
        self.mod._IS_WINDOWS = True
        self.mod._OWN_START.clear()
        self.mod._windows_identity = lambda pid: outbox.ProcessIdentity(pid, outbox.OwnerState(table.get(pid, ("DEAD",))[0]),
                                                                        *table.get(pid, ("DEAD",))[1:])
        self.mod.os = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
        self.mod.os.kill = lambda *a: self.fail("os.kill terminates a process on Windows")

    def test_writer_and_reader_compare_the_same_birth_token(self):
        me, other = os.getpid(), _live_pid()
        table = {me: ("ALIVE", 1111), other: ("ALIVE", 2222)}
        self._stub(table)
        self.assertEqual(self.mod.acquire("r", self.ws).status, "acquired")
        rec = self.mod.read_lease("r", self.ws)
        self.assertEqual(rec["proc_start"], "win-1111")
        self.assertEqual(self.mod.lease_process(rec), me, "proven by the same primitive that wrote it")
        self.assertTrue(self.mod.heartbeat("r", self.ws))
        table[me] = ("ALIVE", 9999)
        self.assertEqual(self.mod.lease_process(rec), 0, "a later process under that pid")
        table[me] = ("DEAD",)
        self.assertEqual(self.mod.lease_process(rec), 0)
        table[me] = ("UNKNOWN",)
        with self.assertRaises(self.mod.LeaseIndeterminate):
            self.mod.lease_process(rec)
        table[me] = ("ALIVE", None)
        with self.assertRaises(self.mod.LeaseIndeterminate):
            self.mod.lease_process(rec)
        self._lock_path("r").write_text(json.dumps(dict(rec, pid=other, proc_start="win-1")))
        table[me] = ("ALIVE", 1111)
        self.assertEqual(self.mod.acquire("r", self.ws).status, "reaped", "another process under the holder's pid")
        self._lock_path("r").write_text(json.dumps(dict(rec, pid=other, proc_start="win-2222")))
        self.assertEqual(self.mod.acquire("r", self.ws).status, "deferred", "the live writer keeps it")

    def test_one_identity_read_per_decision_and_unknown_never_reaps(self):
        import outbox
        me, other = os.getpid(), _live_pid()
        seq = {other: [("ALIVE", 2222), ("UNKNOWN",)]}
        calls = []

        def identity(pid):
            calls.append(pid)
            state = (seq[pid].pop(0) if seq.get(pid) else ("UNKNOWN",)) if pid == other else ("ALIVE", 1111)
            return outbox.ProcessIdentity(pid, outbox.OwnerState(state[0]), *state[1:])
        self._stub({})
        self.mod._windows_identity = identity
        self.assertEqual(self.mod.acquire("r", self.ws).status, "acquired")
        rec = self.mod.read_lease("r", self.ws)
        self._lock_path("r").write_text(json.dumps(dict(rec, pid=other, proc_start="win-2222")))
        calls.clear()
        self.assertEqual(self.mod.acquire("r", self.ws).status, "deferred", "a fresh live holder keeps the role")
        self.assertEqual(calls.count(other), 1, "state and birth token come from one read")
        self.assertEqual(self.mod.acquire("r", self.ws).status, "deferred", "UNKNOWN is never a generation mismatch")


if __name__ == "__main__":
    unittest.main()
