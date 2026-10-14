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
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "src" / "workspace_lock.py"
HOST = "testhost"
os.environ["SUTANDO_HOST_LABEL"] = HOST


def _live_pid() -> int:
    """A running process that is not us: the test runner's parent."""
    return os.getppid()


def _dead_pid() -> int:
    """The pid of a child that has already exited and been reaped."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    if os.name == "nt":
        return p.pid  # os.kill(pid, 0) would terminate a reuser on Windows
    try:
        os.kill(p.pid, 0)
    except ProcessLookupError:
        return p.pid
    raise unittest.SkipTest(f"pid {p.pid} was reused before the test could plant it")


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

    @unittest.skipIf(os.name == "nt", "no pid probe on Windows: heartbeat rule only")
    def test_dead_same_host_holder_is_reaped_immediately(self):
        self._plant("gw", pid=_dead_pid(), hb_age_s=1)     # fresh, but nobody behind it
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
        # pid 1 exists on every unix host; kill(1, 0) raises EPERM unless root.
        self._plant("gw", pid=1, hb_age_s=1)
        self.assertEqual(self.mod.acquire("gw", self.ws).status, "deferred")

    def test_dead_pid_on_another_host_follows_heartbeat_only(self):
        dead = _dead_pid()
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
        self._plant("gw", pid=_dead_pid(), hb_age_s=1)
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


if __name__ == "__main__":
    unittest.main()
