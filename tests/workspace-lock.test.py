#!/usr/bin/env python3
"""Tests for src/workspace_lock.py (MC1 atomic per-workspace role lock).

Covers acquire on absent / idempotent re-acquire / defer-on-fresh-holder /
reap-on-stale-holder / corrupt-lock-reaped, heartbeat (holder vs not), release
(only-if-ours), and a real-subprocess concurrency test asserting O_EXCL yields
exactly one winner. Liveness is heartbeat-freshness (not pid-alive), so tests
control it via heartbeat_at; the host label is pinned for determinism.
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
        self._plant("gw", pid=999999, hb_age_s=5)          # fresh
        r = self.mod.acquire("gw", self.ws)
        self.assertEqual(r.status, "deferred")
        self.assertEqual(r.holder["pid"], 999999)
        # holder untouched
        self.assertEqual(json.loads(self._lock_path().read_text())["pid"], 999999)

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
        """Real O_EXCL race: N processes acquire the same role; exactly 1 wins."""
        def one(_):
            env = dict(os.environ); env["SUTANDO_HOST_LABEL"] = HOST
            r = subprocess.run(
                [sys.executable, str(SCRIPT), "acquire", "--role", "race",
                 "--workspace", str(self.ws)],
                capture_output=True, text=True, env=env)
            return (r.returncode, r.stdout.strip())
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            results = list(ex.map(one, range(8)))
        acquired = [o for rc, o in results if rc == 0 and o in ("acquired", "reaped")]
        deferred = [o for rc, o in results if rc == 3]
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
        p.write_text(json.dumps({"role": "r", "pid": 999999, "host": HOST,
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

    def test_deferred_after_exhausted_contention(self):
        # never win the create, always see a stale holder → loop exhausts → defer
        orig_create = self.mod._try_create
        self.mod._try_create = lambda *a, **k: False
        self.mod._read = lambda path: {"role": "r", "pid": 999999, "host": HOST,
                                       "heartbeat_at": 0, "schema_version": 1}
        try:
            self.assertEqual(self.mod.acquire("r", self.ws).status, "deferred")
        finally:
            self.mod._try_create = orig_create


if __name__ == "__main__":
    unittest.main()
