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
import time
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

    def _sabotage_probes(self):
        """Make ANY probe attempt an immediate failure, to prove a code path
        (derive / _station_cached) never touches the network."""
        def boom(*a, **k):
            raise AssertionError("this path must not probe the network")
        self.mod._probe_station = boom
        self.mod._probe_station_bounded = boom

    # ---- _station_cached: READ-ONLY, never probes (qingyun CR #2680 hot path) ----
    def test_station_cached_cold_is_none_and_never_probes(self):
        self._sabotage_probes()
        self.assertIsNone(self.mod._station_cached(self.ws))  # no cache, no probe

    def test_station_cached_returns_fresh_persisted_value_without_probing(self):
        self._sabotage_probes()
        self._seed_cache(value=True, value_ts=1.0, attempt_ts=1.0)
        self.assertTrue(self.mod._station_cached(self.ws, now=1.5, ttl=60.0))  # 0.5s old

    def test_station_cached_none_when_value_is_expired(self):
        # qingyun CR #2680 repro: a value older than the TTL must read as None
        # (unknown), NOT a stale confident True that hides Station going down.
        self._sabotage_probes()
        self._seed_cache(value=True, value_ts=1000.0, attempt_ts=1000.0)
        self.assertIsNone(self.mod._station_cached(self.ws, now=1000.0 + 3600, ttl=60.0))
        # ...but still fresh within the TTL:
        self.assertTrue(self.mod._station_cached(self.ws, now=1000.0 + 30, ttl=60.0))

    def test_malformed_cache_degrades_to_unknown_never_raises(self):
        # qingyun CR #2680: the cache is a mutable workspace file — a corrupt /
        # synced / hand-edited record must degrade to None, never raise (else
        # core-input-watch's unguarded ~3s derive() dies on one bad record).
        self._sabotage_probes()
        for bad in ({"value": True, "value_ts": "bad", "attempt_ts": "bad"},
                    {"value": "yes", "value_ts": 100.0},   # non-tri-state value
                    {"value": True, "value_ts": True},      # bool is not a timestamp
                    {"value": True, "value_ts": None},
                    {"value": True}):                       # no timestamp at all
            self._seed_cache(**bad)
            self.assertIsNone(self.mod._station_cached(self.ws, now=200.0))

    def test_refresh_survives_malformed_timestamps(self):
        # A malformed value_ts/attempt_ts must not raise in _refresh_station's
        # freshness/cooldown arithmetic — it falls through to a fresh probe.
        self._seed_cache(value=True, value_ts="bad", attempt_ts="bad")
        self.assertFalse(self.mod._refresh_station(self.ws, now=200.0, probe=lambda: False))

    # ---- qingyun CR #2680 round 2: strict schema, not equality-membership ----
    def test_cache_verdict_uses_identity_not_equality_membership(self):
        # 1 == True and 0 == False in Python, so `v in (True, False, None)` would
        # let an integer masquerade as the bool verdict and be returned unchanged,
        # violating the bool|null field contract. Identity rejects it -> None.
        self.assertIsNone(self.mod._cache_verdict(1))
        self.assertIsNone(self.mod._cache_verdict(0))
        self.assertIsNone(self.mod._cache_verdict(1.0))
        # ...while the genuine tri-state still passes through unchanged.
        self.assertIs(self.mod._cache_verdict(True), True)
        self.assertIs(self.mod._cache_verdict(False), False)
        self.assertIsNone(self.mod._cache_verdict(None))

    def test_station_cached_integer_verdict_reads_as_unknown(self):
        # exact-head repro: {"value": 1, "value_ts": 100.0} at now=101 must NOT
        # return the integer 1 — the on-disk contract is bool|null.
        self._sabotage_probes()
        self._seed_cache(value=1, value_ts=100.0, attempt_ts=100.0)
        self.assertIsNone(self.mod._station_cached(self.ws, now=101.0, ttl=60.0))
        self._seed_cache(value=0, value_ts=100.0, attempt_ts=100.0)
        self.assertIsNone(self.mod._station_cached(self.ws, now=101.0, ttl=60.0))

    def test_cache_ts_rejects_non_finite(self):
        # NaN/±inf are floats but poison every comparison: (now - NaN) >= ttl is
        # always False, so a NaN value_ts would read as permanently fresh.
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertIsNone(self.mod._cache_ts(bad))
        self.assertEqual(self.mod._cache_ts(100.0), 100.0)  # finite still passes

    def test_station_cached_nan_timestamp_does_not_freeze_verdict(self):
        # exact-head repro: {"value": True, "value_ts": NaN} at now=1_000_000 must
        # read as unknown, not a permanently-fresh stale True.
        self._sabotage_probes()
        self._seed_cache(value=True, value_ts=float("nan"), attempt_ts=float("nan"))
        self.assertIsNone(self.mod._station_cached(self.ws, now=1_000_000.0, ttl=60.0))

    def test_station_cached_implausible_future_timestamp_reads_unknown(self):
        # A synced/corrupt cache can carry a far-future value_ts; without a future
        # guard, (now - value_ts) is very negative -> reads fresh forever and
        # freezes the verdict. Beyond `ttl` in the future -> unknown.
        self._sabotage_probes()
        self._seed_cache(value=True, value_ts=1_000_000.0, attempt_ts=1_000_000.0)
        self.assertIsNone(self.mod._station_cached(self.ws, now=1000.0, ttl=60.0))
        # ...but a small forward skew (within ttl) is tolerated as benign.
        self._seed_cache(value=True, value_ts=1010.0, attempt_ts=1010.0)
        self.assertTrue(self.mod._station_cached(self.ws, now=1000.0, ttl=60.0))

    def test_refresh_reprobes_when_cached_timestamp_is_in_the_future(self):
        # The same future-freeze must not stop _refresh_station from re-probing:
        # a far-future value_ts is not "still fresh", so a real probe runs.
        self._seed_cache(value=True, value_ts=1_000_000.0, attempt_ts=1_000_000.0)
        self.assertFalse(
            self.mod._refresh_station(self.ws, now=1000.0, probe=lambda: False))

    def test_derive_survives_a_corrupt_cache(self):
        # The whole point: one bad record can't crash the supervisor tick.
        self._sabotage_probes()
        self.mod._run = lambda cmd: (None, "")
        self.mod._resolve_workspace = lambda repo: self.ws
        self._seed_cache(value=True, value_ts="bad")
        self.assertIsNone(self.mod.derive()["station_available"])  # no raise, unknown

    def test_derive_never_probes_even_when_the_resolver_would_stall(self):
        # THE fix: derive() runs on the 3s supervisor loop, so it must NEVER
        # touch the network — a stalled/hung resolver can't delay the tick.
        self._sabotage_probes()
        self.mod._run = lambda cmd: (None, "")
        self.mod._resolve_workspace = lambda repo: self.ws
        out = self.mod.derive()                       # would raise if it probed
        self.assertIsNone(out["station_available"])   # cold cache => unknown, promptly

    # ---- _refresh_station: off-loop, bounded, cooldown (blocker 1 + hung one-shot) ----
    def test_refresh_persists_verdict_read_by_derive(self):
        # The one-shot refresh writes the cache; derive() then reads a REAL value.
        self.mod._resolve_workspace = lambda repo: self.ws
        self.mod._run = lambda cmd: (None, "")
        self.mod._refresh_station(self.ws, now=1000.0, probe=lambda: False)
        self.assertFalse(self.mod.derive()["station_available"])

    def test_refresh_skips_probe_when_cache_still_fresh(self):
        # qingyun CR #2680: the one-shot refresh honors the TTL too — a still-fresh
        # verdict is served without re-probing (not just gated on the cooldown).
        self._seed_cache(value=True, value_ts=1000.0, attempt_ts=1000.0)
        called = {"n": 0}
        self.mod._refresh_station(self.ws, now=1030.0, ttl=60.0,  # 30s old (<60) -> fresh
                                  probe=lambda: called.__setitem__("n", 1) or True)
        self.assertEqual(called["n"], 0)

    def test_refresh_cooldown_skips_probe_of_a_recent_attempt(self):
        self._seed_cache(value=None, value_ts=None, attempt_ts=995.0)
        called = {"n": 0}
        self.mod._refresh_station(self.ws, now=1000.0, cooldown=15.0,
                                  probe=lambda: called.__setitem__("n", 1) or True)
        self.assertEqual(called["n"], 0)              # attempt 5s ago (<15) -> no probe

    def test_refresh_claims_attempt_before_probing(self):
        seen = {}
        def probe():
            seen["attempt_ts"] = json.load(
                open(self.mod._station_cache_file(self.ws))).get("attempt_ts")
            return True
        self.mod._refresh_station(self.ws, now=2000.0, probe=probe)
        self.assertEqual(seen["attempt_ts"], 2000.0)  # persisted BEFORE the probe

    def test_refresh_probe_exception_publishes_unknown_not_false(self):
        self._seed_cache(value=True, value_ts=1.0, attempt_ts=1.0)
        def boom():
            raise RuntimeError("resolver blew up")
        val = self.mod._refresh_station(self.ws, now=1000.0, cooldown=15.0, probe=boom)
        self.assertIsNone(val)

    # ---- _probe_station_bounded: a KILLABLE end-to-end deadline ----
    def test_probe_bounded_kills_a_hung_probe_and_returns_unknown(self):
        # A child that sleeps past the deadline is killed -> None (the real bound
        # on getaddrinfo, which settimeout can't cap).
        hung = [sys.executable, "-c", "import time; time.sleep(30)"]
        self.assertIsNone(self.mod._probe_station_bounded(deadline=0.4, argv=hung))

    def test_probe_bounded_maps_child_output_to_tristate(self):
        say = lambda s: [sys.executable, "-c", f"print({s!r})"]
        self.assertIs(self.mod._probe_station_bounded(deadline=5, argv=say("true")), True)
        self.assertIs(self.mod._probe_station_bounded(deadline=5, argv=say("false")), False)
        self.assertIsNone(self.mod._probe_station_bounded(deadline=5, argv=say("")))

    # ---- derive() surfaces both keys, and does not conflate them ----
    def test_derive_surfaces_both_keys(self):
        self.mod._run = lambda cmd: (None, "")             # everything unknown
        self.mod._resolve_workspace = lambda repo: self.ws  # cold cache -> station None
        out = self.mod.derive()
        self.assertIn("ag2space_app_running", out)
        self.assertIn("station_available", out)
        # both independently None here — station is NOT inferred from the app signal
        self.assertIsNone(out["ag2space_app_running"])
        self.assertIsNone(out["station_available"])

    def test_derive_app_up_but_station_unreachable_are_independent(self):
        self.mod._run = lambda cmd: (0, "1\n")             # app process "running"
        self.mod._resolve_workspace = lambda repo: self.ws
        self._seed_cache(value=False, value_ts=time.time(), attempt_ts=time.time())  # fresh, cached down
        out = self.mod.derive()
        self.assertTrue(out["ag2space_app_running"])
        self.assertFalse(out["station_available"])          # app up != station reachable


if __name__ == "__main__":
    res = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if res.wasSuccessful() else 1)
