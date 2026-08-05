#!/usr/bin/env python3
"""AG2 Space app signal + Station reachability in src/runtime-health.py.

Two HONEST, separate tri-state signals (qingyun CR #2680): `ag2space_app_running`
(narrow: is the app UI process up?) and `station_available` (a real reachability
probe of the Station gateway). Neither collapses an unknown to a False.
Run: python3 tests/runtime-health-ag2space-station.test.py  (exit 0 pass / 1 fail)
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    sys.path.insert(0, str(REPO / "src"))  # sibling imports (util_paths) resolve
    spec = importlib.util.spec_from_file_location(
        "runtime_health_ag2space_under_test", REPO / "src" / "runtime-health.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_socket(*, resolve=True, connect="ok"):
    """A stand-in socket module. connect: 'ok' | 'refuse' | (resolve=False -> DNS fail)."""
    class _Sock:
        def __init__(self, *a): pass
        def settimeout(self, t): pass
        def connect(self, addr):
            if connect == "refuse":
                raise ConnectionRefusedError()
        def close(self): pass
    import socket as _rs
    ns = types.SimpleNamespace(**{k: getattr(_rs, k) for k in dir(_rs) if not k.startswith("__")})
    def getaddrinfo(host, port, **k):
        if not resolve:
            raise OSError("name resolution failed")
        return [(2, 1, 6, "", ("1.2.3.4", 443))]
    ns.getaddrinfo = getaddrinfo
    ns.socket = lambda *a: _Sock()
    return ns


class TestAg2SpaceStationSignals(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.ws = tempfile.mkdtemp()  # a throwaway workspace for the file cache

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _seed_cache(self, **fields):
        p = self.mod._station_cache_file(self.ws)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(fields, f)

    # ---- ag2space_app_running: narrow + tri-state ----
    def test_app_running_true(self):
        seen = []
        def fake_run(cmd):
            seen.append(cmd)
            return (0, "82653\n")
        self.mod._run = fake_run
        self.assertTrue(self.mod._ag2space_app_running())
        # narrow marker (app bundle), NOT the broad engine tree
        self.assertTrue(any(self.mod._AG2SPACE_APP_MARKER in c for c in seen))
        self.assertNotIn("space.ag2.app/engine", self.mod._AG2SPACE_APP_MARKER)

    def test_app_running_false(self):
        self.mod._run = lambda cmd: (1, "")
        self.assertFalse(self.mod._ag2space_app_running())

    def test_app_running_unknown_when_pgrep_unexecutable(self):
        # rc None (pgrep missing) must be UNKNOWN, never a False down-vote.
        self.mod._run = lambda cmd: (None, "")
        self.assertIsNone(self.mod._ag2space_app_running())

    # ---- _probe_station: the BLOCKING reachability logic, tri-state ----
    def test_probe_true_when_gateway_connects(self):
        self.mod.socket = _fake_socket(resolve=True, connect="ok")
        self.assertTrue(self.mod._probe_station())

    def test_probe_false_when_resolves_but_refused(self):
        self.mod.socket = _fake_socket(resolve=True, connect="refuse")
        self.assertFalse(self.mod._probe_station())

    def test_probe_unknown_on_dns_failure(self):
        # DNS/resolver failure is UNKNOWN (could be transient/local), not "unavailable".
        self.mod.socket = _fake_socket(resolve=False)
        self.assertIsNone(self.mod._probe_station())

    # ---- _station_available: FILE-cached, no threads (qingyun CR #2680) ----
    def test_serves_fresh_cache_without_probing(self):
        self._seed_cache(value=True, value_ts=100.0, attempt_ts=100.0)
        called = {"n": 0}
        self.mod._probe_station = lambda timeout=1.0: called.__setitem__("n", called["n"] + 1) or True
        self.assertTrue(self.mod._station_available(self.ws, now=110.0, ttl=60.0))  # 10<60 fresh
        self.assertEqual(called["n"], 0)                                            # no network

    def test_one_shot_cold_cache_returns_real_value_not_none(self):
        # Blocker 1: a fresh one-shot process with no cache must PROBE and return a
        # real verdict (then persist it), NOT return None like the async design.
        self.mod._probe_station = lambda timeout=1.0: True
        self.assertTrue(self.mod._station_available(self.ws, now=1000.0))
        d = json.load(open(self.mod._station_cache_file(self.ws)))
        self.assertTrue(d["value"])                    # persisted for the next process
        self.assertIsNotNone(d["value_ts"])

    def test_reprobes_when_stale_and_publishes_new_verdict(self):
        self._seed_cache(value=True, value_ts=100.0, attempt_ts=100.0)
        called = {"n": 0}
        self.mod._probe_station = lambda timeout=1.0: called.__setitem__("n", called["n"] + 1) or False
        val = self.mod._station_available(self.ws, now=1000.0, ttl=60.0, cooldown=15.0)  # 900>60 stale
        self.assertEqual(called["n"], 1)
        self.assertFalse(val)

    def test_cooldown_prevents_reprobe_of_a_hung_resolver(self):
        # Blocker 2: stale value + a very recent attempt => do NOT probe again. No
        # threads, so no worker pile-up; the cooldown stops a stall/attempt storm.
        self._seed_cache(value=None, value_ts=None, attempt_ts=995.0)
        called = {"n": 0}
        self.mod._probe_station = lambda timeout=1.0: called.__setitem__("n", called["n"] + 1) or True
        self.mod._station_available(self.ws, now=1000.0, cooldown=15.0)  # attempt 5s ago < 15
        self.assertEqual(called["n"], 0)

    def test_claims_attempt_on_disk_before_probing(self):
        # attempt_ts is persisted BEFORE the probe runs, so a concurrent caller
        # (or the next 3s tick) backs off instead of launching a second probe.
        seen = {}
        def probe(timeout=1.0):
            seen["attempt_ts"] = json.load(
                open(self.mod._station_cache_file(self.ws))).get("attempt_ts")
            return True
        self.mod._probe_station = probe
        self.mod._station_available(self.ws, now=2000.0)
        self.assertEqual(seen["attempt_ts"], 2000.0)

    def test_probe_exception_publishes_unknown_not_false(self):
        self._seed_cache(value=True, value_ts=100.0, attempt_ts=100.0)
        def boom(timeout=1.0):
            raise RuntimeError("resolver blew up")
        self.mod._probe_station = boom
        val = self.mod._station_available(self.ws, now=1000.0, ttl=60.0, cooldown=15.0)
        self.assertIsNone(val)  # unknown, never a spurious False

    # ---- derive() surfaces both keys, and does not conflate them ----
    def test_derive_surfaces_both_keys(self):
        self.mod._run = lambda cmd: (None, "")             # everything unknown
        self.mod.socket = _fake_socket(resolve=False)      # station probe -> None
        self.mod._resolve_workspace = lambda repo: self.ws  # cache -> throwaway ws
        out = self.mod.derive()
        self.assertIn("ag2space_app_running", out)
        self.assertIn("station_available", out)
        # both independently None here — station is NOT inferred from the app signal
        self.assertIsNone(out["ag2space_app_running"])
        self.assertIsNone(out["station_available"])

    def test_derive_app_up_but_station_unreachable_are_independent(self):
        self.mod._run = lambda cmd: (0, "1\n")             # app process "running"
        self.mod.socket = _fake_socket(resolve=True, connect="refuse")  # gateway down -> False
        self.mod._resolve_workspace = lambda repo: self.ws
        out = self.mod.derive()
        self.assertTrue(out["ag2space_app_running"])
        self.assertFalse(out["station_available"])          # app up != station reachable


if __name__ == "__main__":
    res = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if res.wasSuccessful() else 1)
