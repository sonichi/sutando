#!/usr/bin/env python3
"""Bridge self-claim on auth rejection (registry-loss recovery, backend #595).
Run: python3 tests/gateway-auto-reenroll.test.py"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from ag2_sparrow import remote_gateway_bridge as gw  # noqa: E402


def _reset(**over):
    gw._reenroll_state.clear()
    gw._reenroll_state.update({"last_attempt_at": None, "code": None,
                               "claimed_at": None})
    gw._reenroll_state.update(over)


class _Claim(unittest.TestCase):
    def setUp(self):
        self._saved = {n: getattr(gw, n) for n in
                       ("URL", "TOKEN", "REENROLL_ENABLED", "_log")}
        self._env = {k: gw.os.environ.get(k) for k in ("AGENT_MXID", "AGENT_ID")}
        gw.os.environ["AGENT_MXID"] = "@probe.agent:ag2.space"
        gw.URL = "https://chat.example/relay"
        gw.TOKEN = "ab" * 24
        gw.REENROLL_ENABLED = True
        self.logs = []
        gw._log = lambda m: self.logs.append(str(m))
        self.posted = []
        _reset()

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(gw, n, v)
        for k, v in self._env.items():
            if v is None:
                gw.os.environ.pop(k, None)
            else:
                gw.os.environ[k] = v
        _reset()

    def _serve(self, body):
        def opener(req, timeout=0):
            self.posted.append((req.full_url, json.loads(req.data)))
            resp = io.BytesIO(json.dumps(body).encode())
            resp.__enter__ = lambda *a: resp
            resp.__exit__ = lambda *a: False
            return resp
        return opener

    def test_provision_base_derivation(self):
        self.assertEqual(gw._provision_base(), "https://chat.example/api")
        gw.URL = "https://chat.example/relay/v2"
        self.assertEqual(gw._provision_base(), "https://chat.example/api")

    def test_claim_parks_once_and_surfaces_code_device_side(self):
        real = gw.urllib.request.urlopen
        gw.urllib.request.urlopen = self._serve(
            {"ok": True, "pending": True, "approval_code": "beef1234"})
        try:
            gw._reenroll_claim()
            gw._reenroll_claim()  # same episode: no second POST
        finally:
            gw.urllib.request.urlopen = real
        self.assertEqual(len(self.posted), 1)
        url, payload = self.posted[0]
        self.assertEqual(url, "https://chat.example/api/connect/reenroll")
        self.assertEqual(payload, {"agent_id": "@probe.agent:ag2.space",
                                   "bearer": "ab" * 24})
        self.assertEqual(gw._reenroll_state["code"], "beef1234")
        self.assertTrue(any("beef1234" in line for line in self.logs))

    def test_kill_switch_and_missing_identity_never_claim(self):
        gw.REENROLL_ENABLED = False
        gw.urllib.request.urlopen = lambda *a, **k: self.fail("claimed while disabled")
        try:
            gw._reenroll_claim()
            self.assertIsNone(gw._reenroll_state["code"])
            gw.REENROLL_ENABLED = True
            _reset()
            gw.os.environ.pop("AGENT_MXID", None)
            gw.os.environ.pop("AGENT_ID", None)
            saved_cfg = gw._config_from_channel_env
            gw._config_from_channel_env = lambda k: ""
            self.addCleanup(setattr, gw, "_config_from_channel_env", saved_cfg)
            gw._reenroll_claim()  # runs, but refuses without identity
            self.assertIsNone(gw._reenroll_state["code"])
        finally:
            gw.urllib.request.urlopen = urllib.request.urlopen

    def test_claim_refusal_is_swallowed(self):
        real = gw.urllib.request.urlopen

        def refuse(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 409, "conflict", {},
                                         io.BytesIO(b'{"error":"already_registered"}'))
        gw.urllib.request.urlopen = refuse
        try:
            gw._reenroll_claim()  # must not raise
        finally:
            gw.urllib.request.urlopen = real
        self.assertIsNone(gw._reenroll_state["code"])

    def test_transient_claim_failure_retries_after_cadence(self):
        # Review P1: a failed POST must not burn the episode — it retries
        # once REENROLL_CLAIM_RETRY_S has passed, and stops once parked.
        real = gw.urllib.request.urlopen
        calls = {"n": 0}

        def flaky(req, timeout=0):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("connection refused")  # recovery service deploying
            return self._serve({"ok": True, "pending": True,
                                "approval_code": "beef1234"})(req, timeout)
        gw.urllib.request.urlopen = flaky
        try:
            gw._reenroll_claim()                       # fails, stamps cadence
            self.assertIsNone(gw._reenroll_state["code"])
            gw._reenroll_claim()                       # inside cadence: no POST
            self.assertEqual(calls["n"], 1)
            gw._reenroll_state["last_attempt_at"] = None  # never attempted
            gw._reenroll_claim()                       # retried -> parks
            self.assertEqual(gw._reenroll_state["code"], "beef1234")
            gw._reenroll_state["last_attempt_at"] = None
            gw._reenroll_claim()                       # parked: one-claim invariant
            self.assertEqual(calls["n"], 2)
        finally:
            gw.urllib.request.urlopen = real


    def test_identity_falls_back_to_the_channel_env_file(self):
        # G3 (live-confirmed on a real install): desktop launchers export
        # NEITHER AGENT_MXID nor AGENT_ID — the channel .env is the source.
        gw.os.environ.pop("AGENT_MXID", None)
        gw.os.environ.pop("AGENT_ID", None)
        saved = gw._config_from_channel_env
        gw._config_from_channel_env = (
            lambda k: "@file.agent:ag2.space" if k == "AG2SPACE_USER_ID" else "")
        real = gw.urllib.request.urlopen
        gw.urllib.request.urlopen = self._serve(
            {"ok": True, "pending": True, "approval_code": "beef1234"})
        try:
            gw._reenroll_claim()
        finally:
            gw.urllib.request.urlopen = real
            gw._config_from_channel_env = saved
        self.assertEqual(self.posted[-1][1]["agent_id"], "@file.agent:ag2.space")
        self.assertEqual(gw._reenroll_state["code"], "beef1234")

    def test_missing_identity_does_not_consume_the_cadence(self):
        gw.os.environ.pop("AGENT_MXID", None)
        gw.os.environ.pop("AGENT_ID", None)
        saved_cfg = gw._config_from_channel_env
        gw._config_from_channel_env = lambda k: ""   # no .env identity either
        self.addCleanup(setattr, gw, "_config_from_channel_env", saved_cfg)
        gw._reenroll_claim()   # no POST issued
        self.assertIsNone(gw._reenroll_state["last_attempt_at"])
        gw.os.environ["AGENT_MXID"] = "@probe.agent:ag2.space"
        real = gw.urllib.request.urlopen
        gw.urllib.request.urlopen = self._serve(
            {"ok": True, "pending": True, "approval_code": "beef1234"})
        try:
            gw._reenroll_claim()   # identity appeared -> claims immediately
        finally:
            gw.urllib.request.urlopen = real
        self.assertEqual(gw._reenroll_state["code"], "beef1234")


class _Probe(unittest.TestCase):
    def setUp(self):
        self._req = gw._req

    def tearDown(self):
        gw._req = self._req

    def test_accepted_token_is_recovery(self):
        gw._req = lambda *a, **k: {"agents": []}
        self.assertTrue(gw._auth_probe())

    def test_only_success_is_recovery(self):
        # Review P1: an error proves nothing about auth — every non-success
        # keeps waiting rather than resuming into a failing gateway.
        for exc in (urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"{}")),
                    urllib.error.HTTPError("u", 403, "no", {}, io.BytesIO(b"{}")),
                    urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b"{}")),
                    urllib.error.HTTPError("u", 503, "err", {}, io.BytesIO(b"{}")),
                    OSError("connection refused"), TimeoutError("slow")):
            def raiser(*a, _e=exc, **k):
                raise _e
            gw._req = raiser
            self.assertFalse(gw._auth_probe(), f"probe passed on {exc!r}")


class _Status(unittest.TestCase):
    def test_status_lifecycle_pending_then_explicit_recovered_terminal(self):
        saved = gw.GATEWAY_STATUS_FILE
        tmp = Path(tempfile.mkdtemp()) / "gateway-status.json"
        gw.GATEWAY_STATUS_FILE = tmp
        try:
            _reset(code="beef1234", claimed_at=123)
            gw._emit_gateway_status(False, error="auth rejected")
            payload = json.loads(tmp.read_text())
            self.assertEqual(payload["reenroll"],
                             {"pending": True, "approval_code": "beef1234",
                              "claimed_at": 123})
            # Approval-probe success -> EXPLICIT recovered terminal (P1: the
            # desktop must never infer success from mere disappearance).
            gw._reenroll_clear(recovered=True)
            gw._emit_gateway_status(True)
            block = json.loads(tmp.read_text())["reenroll"]
            self.assertEqual((block["pending"], block["recovered"]), (False, True))
            self.assertNotIn("approval_code", block)
            # Fresh process / no episode -> NO block at all (unknown, not success).
            _reset()
            gw._emit_gateway_status(True)
            self.assertNotIn("reenroll", json.loads(tmp.read_text()))
        finally:
            gw.GATEWAY_STATUS_FILE = saved
            _reset()

    def test_rotation_win_clears_pending_without_claiming_recovery(self):
        _reset(code="beef1234", claimed_at=123)
        gw._reenroll_clear()   # rotation path: episode superseded, NOT recovered
        self.assertIsNone(gw._reenroll_state["code"])
        self.assertNotIn("recovered_at", gw._reenroll_state)

    def test_second_episode_never_republishes_the_old_recovered_terminal(self):
        # Review P1: a NEW rejection after a recovered episode must not
        # keep advertising the stale recovered:true terminal.
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "_reload_rotated_token", "_reenroll_claim",
                  "_log", "GATEWAY_STATUS_FILE")}
        tmp = Path(tempfile.mkdtemp()) / "gateway-status.json"
        try:
            _reset(recovered_at=1786767544)   # episode A ended recovered
            gw.GATEWAY_STATUS_FILE = tmp
            gw.TOKEN_FILE = ""
            gw._log = lambda m: None
            gw._reload_rotated_token = lambda: False
            gw._reenroll_claim = lambda: None   # claim fails to park (409/net)
            self.assertFalse(gw._recover_auth(401))   # falls to FATAL contract
            self.assertNotIn("recovered_at", gw._reenroll_state)
            gw._emit_gateway_status(False, error="auth rejected HTTP 401")
            self.assertNotIn("reenroll", json.loads(tmp.read_text()))
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)
            _reset()


class _ClaimClock(unittest.TestCase):
    def test_wall_clock_regression_does_not_suppress_retry(self):
        # Review P2: the cadence must ride time.monotonic() — a backward
        # wall-clock step must never suppress claims.
        import unittest.mock as mock
        saved_enabled = gw.REENROLL_ENABLED

        def run_claim(last_offset, wall):
            # Identity refusal sits after the cadence gate, so reaching it
            # proves the gate passed; gate() counts those arrivals.
            gates = {"n": 0}
            with mock.patch.object(gw.time, "monotonic", return_value=100000.0), \
                 mock.patch.object(gw.time, "time", return_value=wall), \
                 mock.patch.object(gw, "_reenroll_identity",
                                   side_effect=lambda: gates.__setitem__("n", gates["n"] + 1) or ""), \
                 mock.patch.object(gw, "_log", lambda m: None):
                gw._reenroll_state["last_attempt_at"] = 100000.0 - last_offset
                gw._reenroll_claim()
            return gates["n"]
        try:
            gw.REENROLL_ENABLED = True
            _reset()
            # Cadence elapsed on monotonic + wall clock stepped far BACK: fires.
            self.assertEqual(run_claim(gw.REENROLL_CLAIM_RETRY_S + 1, 100.0), 1)
            # Not elapsed on monotonic + wall clock far AHEAD: suppressed.
            self.assertEqual(run_claim(1, 9e9), 0)
        finally:
            gw.REENROLL_ENABLED = saved_enabled
            _reset()


class _RecoverLoop(unittest.TestCase):
    def test_pending_claim_resumes_on_same_token_acceptance(self):
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "AUTH_RECHECK_INTERVAL", "_reload_rotated_token",
                  "_heartbeat_singleton", "_auth_probe", "_reenroll_claim",
                  "_emit_gateway_status", "_log", "REENROLL_PROBE_EVERY")}
        try:
            gw.TOKEN_FILE = ""          # no rotation channel at all
            gw.AUTH_RECHECK_INTERVAL = 0
            gw.REENROLL_PROBE_EVERY = 1
            gw._reload_rotated_token = lambda: False
            gw._heartbeat_singleton = lambda: True
            gw._emit_gateway_status = lambda *a, **k: None
            gw._log = lambda m: None
            gw._reenroll_claim = lambda: _reset(code="beef1234",
                                                claimed_at=1)
            probes = {"n": 0}

            def probe():
                probes["n"] += 1
                return probes["n"] >= 3   # accepted on the third re-check
            gw._auth_probe = probe
            self.assertTrue(gw._recover_auth(401))
            self.assertEqual(probes["n"], 3)
            self.assertIsNone(gw._reenroll_state["code"])  # episode cleared
            self.assertIn("recovered_at", gw._reenroll_state)  # explicit terminal
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)
            _reset()

    def test_transiently_failed_claim_retries_and_parks_later(self):
        # Review blocker: a transiently-failed claim must retry — retrying
        # while nothing is parked is safe (no code to supersede).
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "AUTH_RECHECK_INTERVAL", "_reload_rotated_token",
                  "_heartbeat_singleton", "_auth_probe", "_reenroll_claim",
                  "_emit_gateway_status", "_log", "REENROLL_PROBE_EVERY")}
        try:
            gw.TOKEN_FILE = "/tmp/token-file"   # rotation channel exists: no fatal exit
            gw.AUTH_RECHECK_INTERVAL = 0
            gw.REENROLL_PROBE_EVERY = 1
            gw._reload_rotated_token = lambda: False
            gw._emit_gateway_status = lambda *a, **k: None
            gw._log = lambda m: None
            beats = {"n": 0}

            def beat():
                beats["n"] += 1
                # Hard stop: a no-retry regression must FAIL (SystemExit),
                # never hang the suite.
                return beats["n"] < 20
            gw._heartbeat_singleton = beat
            calls = {"n": 0}

            def claim():
                calls["n"] += 1
                if calls["n"] >= 2:   # first POST fails transiently; retry parks
                    gw._reenroll_state["code"] = "cafe5678"
                    gw._reenroll_state["claimed_at"] = 1
            gw._reenroll_claim = claim
            gw._auth_probe = lambda: True
            self.assertTrue(gw._recover_auth(401))
            self.assertGreaterEqual(calls["n"], 2)
            self.assertIn("recovered_at", gw._reenroll_state)  # explicit terminal
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)
            _reset()

    def test_rotation_wins_over_pending_claim_and_clears_it(self):
        # Review P1: rotation used to return True with the stale code still
        # published — the desktop would keep presenting a dead approval code.
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "AUTH_RECHECK_INTERVAL", "_reload_rotated_token",
                  "_heartbeat_singleton", "_reenroll_claim",
                  "_emit_gateway_status", "_log")}
        try:
            gw.TOKEN_FILE = "/tmp/token-file"
            gw.AUTH_RECHECK_INTERVAL = 0
            gw._heartbeat_singleton = lambda: True
            gw._emit_gateway_status = lambda *a, **k: None
            gw._log = lambda m: None
            gw._reenroll_claim = lambda: _reset(code="beef1234",
                                                claimed_at=1)
            rotations = {"n": 0}

            def rotate():
                rotations["n"] += 1
                return rotations["n"] >= 2   # in-loop rotation wins
            gw._reload_rotated_token = rotate
            self.assertTrue(gw._recover_auth(401))
            self.assertIsNone(gw._reenroll_state["code"])
            self.assertNotIn("recovered_at", gw._reenroll_state)
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)
            _reset()


    def test_identity_missing_with_token_loops_and_claims_when_identity_appears(self):
        # Issue #2924 (Chi's-host cohort): token via bare env var, no channel
        # env pointers -> the OLD contract fatal-crash-looped with the claim
        # code present but unreachable. Now: enter the loop, retry identity
        # each cycle, park once it appears — no restart needed.
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "TOKEN", "AUTH_RECHECK_INTERVAL", "REENROLL_ENABLED",
                  "_reload_rotated_token", "_heartbeat_singleton", "_auth_probe",
                  "_config_from_channel_env", "_emit_gateway_status", "_log",
                  "REENROLL_PROBE_EVERY", "URL")}
        env = {k: gw.os.environ.get(k) for k in ("AGENT_MXID", "AGENT_ID")}
        try:
            gw.TOKEN_FILE = ""
            gw.TOKEN = "ab" * 24
            gw.URL = "https://chat.example/relay"
            gw.REENROLL_ENABLED = True
            gw.AUTH_RECHECK_INTERVAL = 0
            gw.REENROLL_PROBE_EVERY = 1
            gw._reload_rotated_token = lambda: False
            gw._heartbeat_singleton = lambda: True
            gw._emit_gateway_status = lambda *a, **k: None
            gw._log = lambda m: None
            gw.os.environ.pop("AGENT_MXID", None)
            gw.os.environ.pop("AGENT_ID", None)
            reads = {"n": 0}   # operator writes the .env mid-episode

            def cfg(k):
                if k != "AG2SPACE_USER_ID":
                    return ""
                reads["n"] += 1
                return "@late.agent:ag2.space" if reads["n"] >= 2 else ""
            gw._config_from_channel_env = cfg
            polls = {"n": 0}

            def fake_urlopen(req, timeout=0):
                resp = io.BytesIO(json.dumps(
                    {"ok": True, "pending": True,
                     "approval_code": "late1234"}).encode())
                resp.__enter__ = lambda *a: resp
                resp.__exit__ = lambda *a: False
                return resp
            real = gw.urllib.request.urlopen
            gw.urllib.request.urlopen = fake_urlopen

            gw._auth_probe = lambda: True
            try:
                self.assertTrue(gw._recover_auth(401))   # loops; never fatal
            finally:
                gw.urllib.request.urlopen = real
            self.assertIn("recovered_at", gw._reenroll_state)
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)
            for k, v in env.items():
                if v is None:
                    gw.os.environ.pop(k, None)
                else:
                    gw.os.environ[k] = v
            _reset()

    def test_fatal_contract_survives_only_where_recovery_impossible(self):
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "TOKEN", "REENROLL_ENABLED",
                  "_reload_rotated_token", "_reenroll_claim")}
        try:
            gw.TOKEN_FILE = ""
            gw._reload_rotated_token = lambda: False
            gw._reenroll_claim = lambda: None
            gw.REENROLL_ENABLED = False    # reenroll off -> fatal preserved
            gw.TOKEN = "ab" * 24
            self.assertFalse(gw._recover_auth(401))
            gw.REENROLL_ENABLED = True     # no bearer at all -> fatal preserved
            gw.TOKEN = ""
            self.assertFalse(gw._recover_auth(401))
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)
            _reset()

    def test_no_channels_keeps_historical_fatal_contract(self):
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "TOKEN", "_reload_rotated_token", "_reenroll_claim")}
        try:
            gw.TOKEN_FILE = ""
            gw.TOKEN = ""
            gw._reload_rotated_token = lambda: False
            gw._reenroll_claim = lambda: None   # nothing parked
            self.assertFalse(gw._recover_auth(401))
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)


if __name__ == "__main__":
    import urllib.request  # noqa: F401 — used via gw.urllib in fixtures
    unittest.main(verbosity=1)
