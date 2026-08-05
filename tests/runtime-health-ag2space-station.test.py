#!/usr/bin/env python3
"""AG2 Space app signal + Station reachability in src/runtime-health.py.

Two HONEST, separate tri-state signals (qingyun CR #2680): `ag2space_app_running`
(narrow: is the app UI process up?) and `station_available` (a real reachability
probe of the Station gateway). Neither collapses an unknown to a False.
Run: python3 tests/runtime-health-ag2space-station.test.py  (exit 0 pass / 1 fail)
"""
import importlib.util
import sys
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

    # ---- station_available: real reachability, tri-state ----
    def test_station_true_when_gateway_connects(self):
        self.mod.socket = _fake_socket(resolve=True, connect="ok")
        self.assertTrue(self.mod._station_available())

    def test_station_false_when_resolves_but_refused(self):
        self.mod.socket = _fake_socket(resolve=True, connect="refuse")
        self.assertFalse(self.mod._station_available())

    def test_station_unknown_on_dns_failure(self):
        # DNS/resolver failure is UNKNOWN (could be transient/local), not "unavailable".
        self.mod.socket = _fake_socket(resolve=False)
        self.assertIsNone(self.mod._station_available())

    # ---- derive() surfaces both keys, and does not conflate them ----
    def test_derive_surfaces_both_keys(self):
        self.mod._run = lambda cmd: (None, "")          # everything unknown
        self.mod.socket = _fake_socket(resolve=False)   # station unknown
        out = self.mod.derive()
        self.assertIn("ag2space_app_running", out)
        self.assertIn("station_available", out)
        # both independently None here — station is NOT inferred from the app signal
        self.assertIsNone(out["ag2space_app_running"])
        self.assertIsNone(out["station_available"])

    def test_derive_app_up_but_station_unreachable_are_independent(self):
        self.mod._run = lambda cmd: (0, "1\n")          # app process "running"
        self.mod.socket = _fake_socket(resolve=True, connect="refuse")  # gateway down
        out = self.mod.derive()
        self.assertTrue(out["ag2space_app_running"])
        self.assertFalse(out["station_available"])       # app up != station reachable


if __name__ == "__main__":
    res = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if res.wasSuccessful() else 1)
