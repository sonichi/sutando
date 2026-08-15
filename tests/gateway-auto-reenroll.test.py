#!/usr/bin/env python3
"""Bridge self-claim on auth rejection (registry-loss recovery, backend #595).

Pins the client half of the claim -> owner-approval flow:
  - ONE claim per auth-rejection episode; the approval code is surfaced on
    device-visible channels only (log + gateway-status.json reenroll block).
  - The recovery loop probes the gateway with the CURRENT token once a claim
    is pending — re-enrollment revalidates the SAME bearer, so waiting for a
    token-file rotation alone would wait forever.
  - _auth_probe treats only a definite 401/403 as "still rejected"; network
    errors keep waiting (no false recovery on a flaky link).
  - Kill switch REMOTE_REENROLL=0 and missing-identity guard never claim.

Run: python3 tests/gateway-auto-reenroll.test.py
"""
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
    gw._reenroll_state.update({"attempted": False, "code": None,
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
            gw._reenroll_claim()  # attempted, but refuses without identity
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


class _Probe(unittest.TestCase):
    def setUp(self):
        self._req = gw._req

    def tearDown(self):
        gw._req = self._req

    def test_accepted_token_is_recovery(self):
        gw._req = lambda *a, **k: {"agents": []}
        self.assertTrue(gw._auth_probe())

    def test_only_success_is_recovery(self):
        # Review P1: a 5xx proves nothing about auth — resuming into a
        # failing gateway just re-enters the error path. Every non-success
        # keeps waiting.
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
            _reset(code="beef1234", claimed_at=123, attempted=True)
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
        _reset(code="beef1234", claimed_at=123, attempted=True)
        gw._reenroll_clear()   # rotation path: episode superseded, NOT recovered
        self.assertIsNone(gw._reenroll_state["code"])
        self.assertNotIn("recovered_at", gw._reenroll_state)


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
            gw._reenroll_claim = lambda: _reset(attempted=True, code="beef1234",
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
            gw._reenroll_claim = lambda: _reset(attempted=True, code="beef1234",
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

    def test_no_channels_keeps_historical_fatal_contract(self):
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "_reload_rotated_token", "_reenroll_claim")}
        try:
            gw.TOKEN_FILE = ""
            gw._reload_rotated_token = lambda: False
            gw._reenroll_claim = lambda: None   # nothing parked
            self.assertFalse(gw._recover_auth(401))
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)


if __name__ == "__main__":
    import urllib.request  # noqa: F401 — used via gw.urllib in fixtures
    unittest.main(verbosity=1)
