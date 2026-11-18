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
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

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

    def test_a_pinned_bridge_with_no_identity_never_claims_from_a_file(self):
        gw.os.environ.pop("AGENT_MXID", None)
        gw.os.environ.pop("AGENT_ID", None)
        saved = gw._config_from_channel_env
        gw._config_from_channel_env = lambda k: "@file.agent:ag2.space"
        gw.urllib.request.urlopen = lambda *a, **k: self.fail("claimed as a file's identity")
        try:
            with mock.patch.dict(gw.os.environ, {gw.agent_identity.PINNED_KEY: "1"}):
                gw._reenroll_claim()
        finally:
            gw.urllib.request.urlopen = urllib.request.urlopen
            gw._config_from_channel_env = saved
        self.assertIsNone(gw._reenroll_state["last_attempt_at"])
        self.assertTrue(any("launcher pinned no agent identity" in line for line in self.logs), self.logs)
        self.assertFalse(any("into the channel .env" in line for line in self.logs), self.logs)

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
                 ("TOKEN_FILE", "TOKEN", "_reload_rotated_token",
                  "_reenroll_claim", "_log", "GATEWAY_STATUS_FILE")}
        tmp = Path(tempfile.mkdtemp()) / "gateway-status.json"
        try:
            _reset(recovered_at=1786767544)   # episode A ended recovered
            gw.GATEWAY_STATUS_FILE = tmp
            gw.TOKEN_FILE = ""
            # A host-resolved TOKEN would (correctly) enter the loop under the
            # narrowed guard — stub it so the FATAL branch is actually taken.
            gw.TOKEN = ""
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


class _Identity(unittest.TestCase):
    """`_reenroll_identity` must reach the durable per-host identity.

    Enrolment writes the agent mxid to `state/auth/ag2space.json`, which
    CLAUDE.md designates as the canonical home for per-host install/identity
    state. Before this the resolver read only the process env and the channel
    .env — so on a host whose .env carries just the token (the shape `connect`
    writes), identity was unreachable and the claim could never be issued:
    `reenroll: agent identity unknown` on every cycle, with the answer sitting
    on disk one directory away.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        (self.state / "auth").mkdir(parents=True)
        self._saved_state = gw._STATE
        gw._STATE = self.state
        self.addCleanup(lambda: setattr(gw, "_STATE", self._saved_state))
        # Neutralise the two earlier candidates so this pins the THIRD one.
        self._env = mock.patch.dict(
            gw.os.environ, {"AGENT_MXID": "", "AGENT_ID": ""}, clear=False)
        self._env.start(); self.addCleanup(self._env.stop)
        for k in ("AGENT_MXID", "AGENT_ID"):
            gw.os.environ.pop(k, None)
        self._chan = mock.patch.object(gw, "_config_from_channel_env", return_value="")
        self._chan.start(); self.addCleanup(self._chan.stop)

    def _write(self, payload):
        (self.state / "auth" / "ag2space.json").write_text(json.dumps(payload))

    def test_identity_resolves_from_the_auth_state_file(self):
        """The load-bearing case: fails before this change, which returns ''."""
        self._write({"agent_id": "@host.agent:ag2.space", "schema_version": 1})
        self.assertEqual(gw._reenroll_identity(), "@host.agent:ag2.space")

    def test_env_still_wins_over_the_state_file(self):
        """Precedence is unchanged — the new source is a LAST resort, so a
        deliberate override cannot be silently outranked by stale enrolment."""
        self._write({"agent_id": "@stale.agent:ag2.space"})
        with mock.patch.dict(gw.os.environ, {"AGENT_MXID": "@live.agent:ag2.space"}):
            self.assertEqual(gw._reenroll_identity(), "@live.agent:ag2.space")

    def test_channel_env_still_wins_over_the_state_file(self):
        self._write({"agent_id": "@stale.agent:ag2.space"})
        with mock.patch.object(gw, "_config_from_channel_env",
                               side_effect=lambda k: "@chan.agent:ag2.space"
                               if k == "AGENT_MXID" else ""):
            self.assertEqual(gw._reenroll_identity(), "@chan.agent:ag2.space")

    def test_every_earlier_candidate_still_outranks_the_state_file(self):
        """REVIEW.md lesson 10 — enumerate the adjacent inputs, not just the one
        you changed. `_reenroll_identity` has SIX inputs; a new last-resort
        source must not quietly outrank any of the five that precede it, so
        walk the whole ladder rather than neutralising it wholesale.
        """
        self._write({"agent_id": "@stale.agent:ag2.space"})
        # 1-2: process env, both spellings.
        for key in ("AGENT_MXID", "AGENT_ID"):
            with mock.patch.dict(gw.os.environ, {key: f"@env-{key}:ag2.space"}):
                self.assertEqual(gw._reenroll_identity(), f"@env-{key}:ag2.space",
                                 f"process env {key} must outrank the state file")
        # 3-5: channel .env, all three spellings it accepts.
        for key in ("AGENT_MXID", "AGENT_ID", "AG2SPACE_USER_ID"):
            with mock.patch.object(gw, "_config_from_channel_env",
                                   side_effect=lambda k, want=key:
                                   f"@chan-{want}:ag2.space" if k == want else ""):
                self.assertEqual(gw._reenroll_identity(), f"@chan-{key}:ag2.space",
                                 f"channel .env {key} must outrank the state file")

    def test_absent_unreadable_and_malformed_all_yield_empty(self):
        """Never raise into the claim path: a missing, corrupt, or
        wrong-shaped record is an unknown identity, not a crash."""
        self.assertEqual(gw._reenroll_identity(), "")          # absent
        (self.state / "auth" / "ag2space.json").write_text("{not json")
        self.assertEqual(gw._reenroll_identity(), "")          # malformed
        self._write({"agent_id": None})
        self.assertEqual(gw._reenroll_identity(), "")          # null field
        self._write({"agent_id": "   "})
        self.assertEqual(gw._reenroll_identity(), "")          # whitespace only

    def test_non_string_agent_id_reads_as_unknown_not_garbage(self):
        """Review P2: a truthy non-string must not be coerced into a garbage
        identity — `_reenroll_claim` gates on `if not agent_id`, so '12345'
        would be POSTed on the cadence and the operator hint never printed."""
        for bad in (12345, ["@x:ag2.space"], {"v": "@x"}, True, 0.5):
            self._write({"agent_id": bad})
            self.assertEqual(gw._reenroll_identity(), "",
                             f"non-string agent_id {bad!r} must read as unknown")

    def test_identity_appearing_later_is_picked_up_without_a_restart(self):
        """Re-read per call, matching the channel-env candidates: an operator
        (or a re-enrolment) landing the file mid-episode must take effect."""
        self.assertEqual(gw._reenroll_identity(), "")
        self._write({"agent_id": "@late.agent:ag2.space"})
        self.assertEqual(gw._reenroll_identity(), "@late.agent:ag2.space")


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

    def test_transient_401_recovers_via_unguarded_probe_even_with_refused_claim(self):
        # A refused claim (409 already_registered) must not gate the probe —
        # the probe must still run and can resume the connection on its own.
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "TOKEN", "REENROLL_ENABLED", "AUTH_RECHECK_INTERVAL",
                  "_reload_rotated_token", "_heartbeat_singleton", "_auth_probe",
                  "_reenroll_claim", "_emit_gateway_status", "_log", "REENROLL_PROBE_EVERY")}
        try:
            gw.TOKEN_FILE = ""
            # A bearer must be present, else _recover_auth's pre-loop guard
            # returns False before the loop (and the probe) ever runs.
            gw.TOKEN = "ab" * 24
            gw.REENROLL_ENABLED = True
            gw.AUTH_RECHECK_INTERVAL = 0
            gw.REENROLL_PROBE_EVERY = 1
            gw._reload_rotated_token = lambda: False
            gw._emit_gateway_status = lambda *a, **k: None
            gw._log = lambda m: None
            # Claim is always refused (409 already_registered) — pending never parks.
            gw._reenroll_claim = lambda: None
            beats = {"n": 0}

            def beat():
                beats["n"] += 1
                # Hard stop: if the fix regresses, this bounds the loop
                # instead of hanging the suite.
                return beats["n"] < 20
            gw._heartbeat_singleton = beat
            probes = {"n": 0}

            def probe():
                probes["n"] += 1
                return probes["n"] >= 3   # token starts being accepted on the 3rd re-check
            gw._auth_probe = probe
            self.assertTrue(gw._recover_auth(401))
            self.assertEqual(probes["n"], 3)
            self.assertIsNone(gw._reenroll_state["code"])
            # Never pending -> was_pending gate withholds recovered_at, same as rotation-wins.
            self.assertNotIn("recovered_at", gw._reenroll_state)
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)
            _reset()

    def test_persistently_failing_probe_keeps_waiting_even_unguarded(self):
        # A probe stubbed to always fail can never return True, so SystemExit
        # here is just the harness's bound; assertGreater below is the real pin.
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "TOKEN", "REENROLL_ENABLED", "AUTH_RECHECK_INTERVAL",
                  "_reload_rotated_token", "_heartbeat_singleton", "_auth_probe",
                  "_reenroll_claim", "_emit_gateway_status", "_log", "REENROLL_PROBE_EVERY")}
        try:
            gw.TOKEN_FILE = ""
            gw.TOKEN = "ab" * 24
            gw.REENROLL_ENABLED = True
            gw.AUTH_RECHECK_INTERVAL = 0
            gw.REENROLL_PROBE_EVERY = 1
            gw._reload_rotated_token = lambda: False
            gw._emit_gateway_status = lambda *a, **k: None
            gw._log = lambda m: None
            gw._reenroll_claim = lambda: None
            beats = {"n": 0}

            def beat():
                beats["n"] += 1
                return beats["n"] < 6
            gw._heartbeat_singleton = beat
            probes = {"n": 0}

            def probe():
                probes["n"] += 1
                return False   # genuinely dead token
            gw._auth_probe = probe
            with self.assertRaises(SystemExit):
                gw._recover_auth(401)
            self.assertGreater(probes["n"], 0)   # it did run, unguarded...
            self.assertIsNone(gw._reenroll_state["code"])   # ...but never resumed
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)
            _reset()

    def test_probe_only_resume_preserves_the_claim_cadence(self):
        # A never-parked episode's cadence must survive the clear, or every
        # future episode re-claims immediately regardless of the 600s throttle.
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "TOKEN", "REENROLL_ENABLED", "AUTH_RECHECK_INTERVAL",
                  "_reload_rotated_token", "_heartbeat_singleton", "_auth_probe",
                  "_reenroll_claim", "_emit_gateway_status", "_log", "REENROLL_PROBE_EVERY")}
        try:
            gw.TOKEN_FILE = ""
            gw.TOKEN = "ab" * 24
            gw.REENROLL_ENABLED = True
            gw.AUTH_RECHECK_INTERVAL = 0
            gw.REENROLL_PROBE_EVERY = 1
            gw._reload_rotated_token = lambda: False
            gw._emit_gateway_status = lambda *a, **k: None
            gw._log = lambda m: None
            gw._reenroll_claim = lambda: None   # always refused, never parks
            gw._heartbeat_singleton = lambda: True
            gw._reenroll_state["last_attempt_at"] = 12345.0   # a real prior POST
            gw._auth_probe = lambda: True   # resolves on cycle 1
            self.assertTrue(gw._recover_auth(401))
            self.assertEqual(gw._reenroll_state["last_attempt_at"], 12345.0)
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)
            _reset()

    def test_probe_ignores_reenroll_kill_switch(self):
        # Documented, intentional: REMOTE_REENROLL=0 disables self-claim, not the probe.
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "TOKEN", "REENROLL_ENABLED", "AUTH_RECHECK_INTERVAL",
                  "_reload_rotated_token", "_heartbeat_singleton", "_auth_probe",
                  "_reenroll_claim", "_emit_gateway_status", "_log", "REENROLL_PROBE_EVERY")}
        try:
            gw.TOKEN_FILE = "/tmp/token-file"   # rotation channel: no fatal exit
            gw.TOKEN = "ab" * 24
            gw.REENROLL_ENABLED = False
            gw.AUTH_RECHECK_INTERVAL = 0
            gw.REENROLL_PROBE_EVERY = 1
            gw._reload_rotated_token = lambda: False
            gw._emit_gateway_status = lambda *a, **k: None
            gw._log = lambda m: None
            gw._heartbeat_singleton = lambda: True
            gw._auth_probe = lambda: True
            self.assertTrue(gw._recover_auth(401))
            self.assertIsNone(gw._reenroll_state["code"])
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)
            _reset()

    def _loop_harness(self, saved_extra=()):
        names = ("TOKEN_FILE", "TOKEN", "AUTH_RECHECK_INTERVAL", "REENROLL_ENABLED",
                 "_reload_rotated_token", "_heartbeat_singleton", "_auth_probe",
                 "_emit_gateway_status", "_log", "REENROLL_PROBE_EVERY",
                 "URL") + tuple(saved_extra)
        saved = {n: getattr(gw, n) for n in names}
        env = {k: gw.os.environ.get(k)
               for k in ("AGENT_MXID", "AGENT_ID", "AG2_DEVICE_ENV",
                         "CLAUDE_CONFIG_DIR")}
        for k in env:
            gw.os.environ.pop(k, None)
        gw.TOKEN_FILE = ""
        gw.TOKEN = "ab" * 24
        gw.URL = "https://chat.example/relay"
        gw.REENROLL_ENABLED = True
        gw.AUTH_RECHECK_INTERVAL = 0
        gw.REENROLL_PROBE_EVERY = 1
        gw._reload_rotated_token = lambda: False
        gw._emit_gateway_status = lambda *a, **k: None
        return saved, env

    def _restore(self, saved, env):
        for n, v in saved.items():
            setattr(gw, n, v)
        for k, v in env.items():
            if v is None:
                gw.os.environ.pop(k, None)
            else:
                gw.os.environ[k] = v
        _reset()

    def test_pointered_identity_written_midepisode_is_detected_without_restart(self):
        # Issue #2924 review split, half 1: with a channel-env POINTER present,
        # the REAL file-reading path detects a newly written AGENT_MXID —
        # no mocks on the config reader, a real temp .env.
        saved, env = self._loop_harness()
        import tempfile
        envfile = Path(tempfile.mkdtemp()) / "device.env"
        envfile.write_text("REMOTE_TASK_TOKEN=unused\n")
        gw.os.environ["AG2_DEVICE_ENV"] = str(envfile)
        logs = []
        gw._log = logs.append
        beats = {"n": 0}

        def beat():
            beats["n"] += 1
            if beats["n"] == 2:   # operator writes the fix mid-episode
                envfile.write_text("AGENT_MXID=@late.agent:ag2.space\n")
            return beats["n"] < 25
        gw._heartbeat_singleton = beat

        def fake_urlopen(req, timeout=0):
            resp = io.BytesIO(json.dumps(
                {"ok": True, "pending": True,
                 "approval_code": "late1234"}).encode())
            resp.__enter__ = lambda *a: resp
            resp.__exit__ = lambda *a: False
            return resp
        real = gw.urllib.request.urlopen
        gw.urllib.request.urlopen = fake_urlopen
        # Probe succeeds only once a code is parked — else the now-unguarded
        # probe would resolve on cycle 1, before AGENT_MXID is ever written.
        gw._auth_probe = lambda: bool(gw._reenroll_state["code"])
        try:
            self.assertTrue(gw._recover_auth(401))
            self.assertIn("recovered_at", gw._reenroll_state)
            self.assertTrue(any("without restart" in ln for ln in logs))
        finally:
            gw.urllib.request.urlopen = real
            self._restore(saved, env)

    def test_no_pointer_cohort_waits_stably_and_names_the_restart(self):
        # Half 2: no claim-side fix possible (no identity) — the now-unguarded
        # probe still runs, but a persistently-failing one must still hard-stop.
        saved, env = self._loop_harness()
        logs = []
        gw._log = logs.append
        beats = {"n": 0}

        def beat():
            beats["n"] += 1
            return beats["n"] < 6   # bounded observation window, no wall clock
        gw._heartbeat_singleton = beat
        probes = {"n": 0}

        def probe():
            probes["n"] += 1
            return False   # genuinely no fix available in this cohort
        gw._auth_probe = probe
        real = gw.urllib.request.urlopen   # capture BEFORE mutating — the
        gw.urllib.request.urlopen = lambda *a, **k: self.fail("no POST expected")
        try:
            with self.assertRaises(SystemExit):   # singleton hard-stop, not FATAL-401
                gw._recover_auth(401)
            self.assertTrue(any("RESTART the wrapper" in ln for ln in logs))
            self.assertIsNone(gw._reenroll_state["code"])
            self.assertGreater(probes["n"], 0)   # ran unguarded...
        finally:
            # module object is shared, so name-restoring reads the mock back
            gw.urllib.request.urlopen = real
            self._restore(saved, env)


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


class _Refused(unittest.TestCase):
    """A refused claim is surfaced in gateway-status.json, not only logged."""

    def setUp(self):
        self._saved = {n: getattr(gw, n) for n in
                       ("URL", "TOKEN", "REENROLL_ENABLED", "_log",
                        "GATEWAY_STATUS_FILE")}
        self._env = gw.os.environ.get("AGENT_MXID")
        gw.os.environ["AGENT_MXID"] = "@probe.agent:ag2.space"
        gw.URL = "https://chat.example/relay"
        gw.TOKEN = "ab" * 24
        gw.REENROLL_ENABLED = True
        gw._log = lambda m: None
        self.status = Path(tempfile.mkdtemp()) / "gateway-status.json"
        gw.GATEWAY_STATUS_FILE = self.status
        _reset()

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(gw, n, v)
        if self._env is None:
            gw.os.environ.pop("AGENT_MXID", None)
        else:
            gw.os.environ["AGENT_MXID"] = self._env
        _reset()

    def _claim_answering(self, status, body):
        def opener(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, status, "x", {},
                                         io.BytesIO(body))
        real = gw.urllib.request.urlopen
        gw.urllib.request.urlopen = opener
        try:
            gw._reenroll_claim()
        finally:
            gw.urllib.request.urlopen = real

    def _block(self):
        gw._emit_gateway_status(False, error="auth rejected HTTP 401")
        return json.loads(self.status.read_text()).get("reenroll")

    def test_disabled_window_is_published_with_the_server_error(self):
        self._claim_answering(503, b'{"error":"reenroll_disabled"}')
        block = self._block()
        self.assertEqual(
            {k: block[k] for k in ("pending", "refused", "refused_status",
                                   "refused_error")},
            {"pending": False, "refused": True, "refused_status": 503,
             "refused_error": "reenroll_disabled"})
        self.assertIsInstance(block["refused_at"], int)
        self.assertNotIn("approval_code", block)

    def test_already_registered_is_published_too(self):
        self._claim_answering(409, b'{"error":"already_registered"}')
        self.assertEqual(self._block()["refused_error"], "already_registered")

    def test_non_json_body_keeps_the_status_and_no_error_name(self):
        self._claim_answering(502, b"<html>bad gateway</html>")
        block = self._block()
        self.assertEqual((block["refused_status"], block["refused_error"]),
                         (502, None))

    def test_non_string_error_field_is_not_published(self):
        self._claim_answering(503, b'{"error":{"nested":1}}')
        self.assertIsNone(self._block()["refused_error"])

    def test_a_later_parked_claim_replaces_the_refusal(self):
        self._claim_answering(503, b'{"error":"reenroll_disabled"}')
        gw._reenroll_state["last_attempt_at"] = None   # cadence elapsed

        def park(req, timeout=0):
            resp = io.BytesIO(json.dumps({"pending": True,
                                          "approval_code": "c0de"}).encode())
            resp.__enter__ = lambda *a: resp
            resp.__exit__ = lambda *a: False
            return resp
        real = gw.urllib.request.urlopen
        gw.urllib.request.urlopen = park
        try:
            gw._reenroll_claim()
        finally:
            gw.urllib.request.urlopen = real
        self.assertNotIn("refused", gw._reenroll_state)
        self.assertEqual(self._block(),
                         {"pending": True, "approval_code": "c0de",
                          "claimed_at": gw._reenroll_state["claimed_at"]})

    def test_episode_end_drops_the_refusal(self):
        self._claim_answering(503, b'{"error":"reenroll_disabled"}')
        gw._reenroll_clear()
        self.assertIsNone(self._block())

    def test_a_new_episode_starts_without_the_old_refusal(self):
        self._claim_answering(503, b'{"error":"reenroll_disabled"}')
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "_reload_rotated_token", "_reenroll_claim")}
        try:
            gw.TOKEN_FILE = ""
            gw.TOKEN = ""   # no bearer: _recover_auth returns before the loop
            gw._reload_rotated_token = lambda: False
            gw._reenroll_claim = lambda: None
            self.assertFalse(gw._recover_auth(401))
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)
        self.assertNotIn("refused", gw._reenroll_state)

    def test_wait_loop_status_names_the_refusal(self):
        errors = []
        saved = {n: getattr(gw, n) for n in
                 ("TOKEN_FILE", "AUTH_RECHECK_INTERVAL", "REENROLL_PROBE_EVERY",
                  "_reload_rotated_token", "_heartbeat_singleton", "_auth_probe",
                  "_reenroll_claim", "_emit_gateway_status")}

        def refuse_once():
            if not gw._reenroll_state.get("refused"):
                gw._reenroll_state["refused"] = {"status": 503,
                                                 "error": "reenroll_disabled",
                                                 "at": 1}
        beats = {"n": 0}

        def beat():
            beats["n"] += 1
            return True
        try:
            gw.TOKEN_FILE = ""
            gw.AUTH_RECHECK_INTERVAL = 0
            gw.REENROLL_PROBE_EVERY = 1
            gw._reload_rotated_token = lambda: False
            gw._heartbeat_singleton = beat
            gw._auth_probe = lambda: beats["n"] >= 2
            gw._reenroll_claim = refuse_once
            gw._emit_gateway_status = lambda c, **k: errors.append(k.get("error"))
            self.assertTrue(gw._recover_auth(401))
        finally:
            for n, v in saved.items():
                setattr(gw, n, v)
        self.assertEqual(errors[0], "auth rejected HTTP 401 — re-link claim "
                                    "refused (HTTP 503 reenroll_disabled)")


class _Deleted(unittest.TestCase):
    """An agent its owner deleted: the claim's 410 agent_deleted ends retrying and unbinds the file."""

    SECRET = "cd" * 24
    OTHER = "ef" * 24

    def setUp(self):
        names = ("URL", "TOKEN", "TOKEN_FILE", "REENROLL_ENABLED", "AUTH_RECHECK_INTERVAL",
                 "REENROLL_PROBE_EVERY", "GATEWAY_STATUS_FILE", "_log", "_heartbeat_singleton",
                 "_auth_probe", "_bearer_check")
        self._saved = {n: getattr(gw, n) for n in names}
        self._saved_reload = gw._reload_rotated_token
        self._env = gw.os.environ.get("AGENT_MXID")
        gw.os.environ["AGENT_MXID"] = "@gone.agent:ag2.space"
        self.dir = Path(tempfile.mkdtemp())
        self.env = self.dir / ".env"
        self.env.write_text(
            "# desktop connect\n"
            f"AG2_REMOTE_TOKEN='https://chat.example/relay|{self.SECRET}'\n"
            "AGENT_MXID=@gone.agent:ag2.space\n"
            f"AG2_REMOTE_TOKEN_S_DSPACE='https://chat.s.example/relay|{self.OTHER}'\n"
            "REMOTE_PROACTIVE_ROOM=!keep:ag2.space\n")
        self.env.chmod(0o600)
        gw.URL = "https://chat.example/relay"
        gw.TOKEN = self.SECRET
        gw.TOKEN_FILE = str(self.env)
        gw.REENROLL_ENABLED = True
        gw.AUTH_RECHECK_INTERVAL = 0
        gw.REENROLL_PROBE_EVERY = 1
        gw.GATEWAY_STATUS_FILE = self.dir / "gateway-status.json"
        gw._log = lambda m: None
        gw._auth_probe = lambda: self.fail("probed a deleted agent's bearer")
        self.checks = []
        gw._bearer_check = lambda: self.checks.append("refused") or "refused"   # the deleted bearer, refused
        self.claims = []
        _reset()

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(gw, n, v)
        if self._env is None:
            gw.os.environ.pop("AGENT_MXID", None)
        else:
            gw.os.environ["AGENT_MXID"] = self._env
        _reset()

    def _answer(self, status, body):
        def opener(req, timeout=0):
            self.claims.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(body))
        real = gw.urllib.request.urlopen
        gw.urllib.request.urlopen = opener
        self.addCleanup(setattr, gw.urllib.request, "urlopen", real)

    def _status(self):
        return json.loads(gw.GATEWAY_STATUS_FILE.read_text())

    def test_deleted_agent_stops_retrying_unbinds_and_resumes_on_a_new_credential(self):
        self._answer(410, b'{"error":"agent_deleted"}')
        beats = {"n": 0}
        seen = []

        def beat():
            beats["n"] += 1
            if beats["n"] == 1:
                seen.append(self._status())
                seen.append(self.env.read_text())
            if beats["n"] == 3:   # a new agent's connect writes a fresh credential
                self.env.write_text(self.env.read_text()
                                    + "AG2_REMOTE_TOKEN='https://chat.example/relay|" + "12" * 24 + "'\n")
            return True
        gw._heartbeat_singleton = beat
        self.assertTrue(gw._recover_auth(401))
        self.assertEqual(len(self.claims), 1, "a deleted agent is claimed once, never again")
        status, unbound = seen
        self.assertEqual(status["binding"], "deleted")
        self.assertEqual(status["reenroll"]["deleted"], True)
        self.assertEqual(status["reenroll"]["agent_id"], "@gone.agent:ag2.space")
        self.assertTrue(status["reenroll"]["credential_retired"])
        self.assertIn("agent deleted", status["error"])
        self.assertNotIn(self.SECRET, unbound)
        self.assertNotIn("AGENT_MXID", unbound)
        self.assertIn(self.OTHER, unbound, "another lane's credential stays")
        self.assertIn("REMOTE_PROACTIVE_ROOM=!keep:ag2.space", unbound)
        retired = Path(str(self.env) + ".retired")
        self.assertIn(self.SECRET, retired.read_text())
        self.assertIn("AGENT_MXID=@gone.agent:ag2.space", retired.read_text())
        self.assertEqual(retired.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(gw.TOKEN, "12" * 24)
        self.assertNotIn("deleted", gw._reenroll_state)
        self.assertIsNone(gw.os.environ.get("AGENT_MXID"), "the deleted id no longer names this process")

    def test_other_refusals_keep_the_binding(self):
        for status, body in ((410, b'{"error":"gone"}'), (403, b'{"error":"reenroll_blocked"}'),
                             (503, b'{"error":"agent_deleted"}')):
            _reset()
            self._answer(status, body)
            gw._reenroll_claim()
            self.assertNotIn("deleted", gw._reenroll_state, (status, body))
        self.assertIn(self.SECRET, self.env.read_text())

    def test_split_retired_moves_only_the_dead_secret_and_id(self):
        from ag2_sparrow import gateway_credential_sources as sources
        text = ("export REMOTE_TASK_TOKEN=" + self.SECRET + "\n"
                "AGENT_ID=@someone.else:ag2.space\n"
                "AGENT_MXID='@gone.agent:ag2.space'\n"
                "https://chat.example/relay|" + self.SECRET + "\n"
                "AG2_REMOTE_TOKEN='https://chat.example/relay|" + self.OTHER + "'\n")
        # The file names another agent too: it does not tie the deleted id to this bearer.
        self.assertEqual(sources.split_deleted_binding(text, secret=self.SECRET, agent_id="@gone.agent:ag2.space"),
                         (text, [], []))
        text = text.replace("AGENT_ID=@someone.else:ag2.space\n", "")
        kept, retired, keys = sources.split_deleted_binding(text, secret=self.SECRET,
                                                            agent_id="@gone.agent:ag2.space")
        self.assertEqual(keys, ["REMOTE_TASK_TOKEN", "AGENT_MXID", "token"])
        self.assertEqual(kept, "AG2_REMOTE_TOKEN='https://chat.example/relay|" + self.OTHER + "'\n")
        self.assertEqual(len(retired), 3)
        # Only the id, no bearer: nothing to retire.
        self.assertEqual(sources.split_deleted_binding("AGENT_MXID=@gone.agent:ag2.space\n", secret=self.SECRET,
                                                       agent_id="@gone.agent:ag2.space")[2], [])

    def test_src_owner_classifies_the_same_lines(self):
        import importlib.util
        sys.path.insert(0, str(_REPO / "shared"))
        self.addCleanup(sys.path.remove, str(_REPO / "shared"))
        spec = importlib.util.spec_from_file_location("gcs_src_deleted", _REPO / "src" / "gateway_credential_sources.py")
        src = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = src
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(src)
        text = ("# note\nAG2_REMOTE_TOKEN='https://chat.example/relay|" + self.SECRET + "'\n"
                "AGENT_MXID=@gone.agent:ag2.space\n" + self.SECRET + "\nREMOTE_TASK_TOKEN=" + self.OTHER)
        kept, removed, keys = src.split_deleted_binding(text, secret=self.SECRET, agent_id="@gone.agent:ag2.space")
        self.assertEqual(keys, ["AG2_REMOTE_TOKEN", "AGENT_MXID", "token"])
        self.assertEqual(kept, "# note\nREMOTE_TASK_TOKEN=" + self.OTHER)
        self.assertTrue(all(line.endswith("\n") for line in removed))
        self.assertEqual(src.retirement_paths({"CLAUDE_CONFIG_DIR": "/w", "SUTANDO_APP_SUPPORT": "/app"}, "/t.env"),
                         ["/t.env", "/w/channels/ag2space/.env", "/app/channels/ag2space/.env"])

    def test_retire_leaves_a_file_without_the_binding_alone(self):
        from ag2_sparrow import deleted_binding
        self.env.write_text("REMOTE_PROACTIVE_ROOM=!keep:ag2.space\n")
        self.assertEqual(deleted_binding.retire(str(self.env), secret=self.SECRET, agent_id="@gone.agent:ag2.space"), [])
        self.assertFalse(Path(str(self.env) + ".retired").exists())

    def test_a_failed_replace_keeps_the_env_and_leaves_no_temp_file(self):
        from ag2_sparrow import deleted_binding
        before = self.env.read_text()

        def boom(*a, **k):
            raise OSError("disk full")
        with mock.patch.object(deleted_binding.os, "replace", boom):
            with self.assertRaises(OSError):
                deleted_binding.retire(str(self.env), secret=self.SECRET, agent_id="@gone.agent:ag2.space")
        self.assertEqual(self.env.read_text(), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [".env", ".env.retired"])

    def test_retire_needs_a_token_file_and_survives_an_os_error(self):
        gw.TOKEN_FILE = ""
        self.assertFalse(gw._retire_deleted_binding("@gone.agent:ag2.space"))
        gw.TOKEN_FILE = str(self.env)
        logs = []
        gw._log = logs.append
        with mock.patch.object(gw.deleted_binding, "retire", side_effect=OSError("read-only")):
            self.assertFalse(gw._retire_deleted_binding("@gone.agent:ag2.space"))
        self.assertTrue(any("could not retire" in line for line in logs))
        self.assertIn(self.SECRET, self.env.read_text())

    def test_deletion_learned_inside_the_wait_loop_ends_it(self):
        calls = {"n": 0}

        def claim():
            calls["n"] += 1
            if calls["n"] == 2:   # the first claim failed in transit; the retry is answered 410
                gw._reenroll_state["deleted"] = {"at": 1, "agent_id": "@gone.agent:ag2.space"}
        saved = gw._reenroll_claim
        gw._reenroll_claim = claim
        self.addCleanup(setattr, gw, "_reenroll_claim", saved)
        gw._auth_probe = lambda: False
        beats = {"n": 0}

        def beat():
            beats["n"] += 1
            return beats["n"] < 3
        gw._heartbeat_singleton = beat
        with self.assertRaises(SystemExit) as stop:
            gw._recover_auth(401)
        self.assertIn("deletion", str(stop.exception))
        self.assertEqual(calls["n"], 2, "no claim after the deletion")
        self.assertNotIn(self.SECRET, self.env.read_text())

    def test_a_stale_deleted_id_never_retires_another_agents_live_token(self):
        # The launcher still exports the deleted id; the file holds a new agent's working token.
        live = "34" * 24
        self.env.write_text(f"AG2_REMOTE_TOKEN='https://chat.example/relay|{live}'\n"
                            "AGENT_MXID=@new.agent:ag2.space\n")
        before = self.env.read_text()
        gw.TOKEN = live
        self._answer(410, b'{"error":"agent_deleted"}')
        probes = {"n": 0}

        def probe():
            probes["n"] += 1
            return "accepted"   # the bearer is fine: the 401 was transient
        gw._bearer_check = probe
        beats = {"n": 0}

        def beat():   # bounded: a regression that stops probing exits instead of hanging
            beats["n"] += 1
            return beats["n"] < 5
        gw._heartbeat_singleton = beat
        statuses = []
        real_emit = gw._emit_gateway_status

        def emit(*a, **k):
            real_emit(*a, **k)
            statuses.append(self._status())
        gw._emit_gateway_status = emit
        self.addCleanup(setattr, gw, "_emit_gateway_status", real_emit)
        self.assertTrue(gw._recover_auth(401))
        self.assertEqual(self.env.read_text(), before, "the live token stays")
        self.assertFalse(Path(str(self.env) + ".retired").exists())
        self.assertEqual(statuses, [], "resumed on the first check, before any deleted status")
        self.assertEqual(probes["n"], 1)
        self.assertIsNone(gw.os.environ.get("AGENT_MXID"), "the stale id is dropped on resume")

    def test_a_403_never_retires(self):
        self._answer(410, b'{"error":"agent_deleted"}')
        beats = {"n": 0}

        def beat():
            beats["n"] += 1
            return beats["n"] < 2
        gw._heartbeat_singleton = beat
        gw._auth_probe = lambda: False
        with self.assertRaises(SystemExit):
            gw._recover_auth(403)
        self.assertIn(self.SECRET, self.env.read_text())

    def test_a_pinned_deleted_identity_restarts_instead_of_resuming(self):
        gw.os.environ[gw.agent_identity.PINNED_KEY] = "1"
        self.addCleanup(gw.os.environ.pop, gw.agent_identity.PINNED_KEY, None)
        with self.assertRaises(SystemExit) as stop:
            gw._forget_deleted_identity("@gone.agent:ag2.space")
        self.assertEqual(stop.exception.code, gw.POLL_STALL_EXIT_CODE)
        gw._forget_deleted_identity("")   # nothing named: nothing to do

    def test_writer_contract_links_encodings_line_ends_and_races(self):
        from ag2_sparrow import deleted_binding
        args = dict(secret=self.SECRET, agent_id="@gone.agent:ag2.space")
        # CRLF stays CRLF, byte for byte.
        self.env.write_bytes(self.env.read_bytes().replace(b"\n", b"\r\n"))
        deleted_binding.retire(str(self.env), **args)
        self.assertTrue(self.env.read_bytes().endswith(b"!keep:ag2.space\r\n"))
        self.assertNotIn(b"\n", self.env.read_bytes().replace(b"\r\n", b""))
        # A wider pre-existing sidecar is narrowed.
        retired = Path(str(self.env) + ".retired")
        retired.chmod(0o644)
        self.env.write_text(f"AG2_REMOTE_TOKEN={self.SECRET}\nAGENT_MXID=@gone.agent:ag2.space\n")
        self.assertEqual(deleted_binding.retire(str(self.env), **args), ["AG2_REMOTE_TOKEN", "AGENT_MXID"])
        self.assertEqual(retired.stat().st_mode & 0o777, 0o600)
        # A symlink and a non-UTF-8 file are left alone.
        target = self.dir / "real.env"
        target.write_text(f"AG2_REMOTE_TOKEN={self.SECRET}\nAGENT_MXID=@gone.agent:ag2.space\n")
        link = self.dir / "link.env"
        link.symlink_to(target)
        self.assertEqual(deleted_binding.retire(str(link), **args), [])
        self.assertTrue(link.is_symlink())
        bad = self.dir / "bad.env"
        bad.write_bytes(b"AG2_REMOTE_TOKEN=" + self.SECRET.encode() + b"\n\xff\xfe\n")
        self.assertEqual(deleted_binding.retire(str(bad), **args), [])
        # A file rewritten between the read and the replace is not overwritten, and no sidecar line is added.
        race = self.dir / "race.env"
        race.write_text(f"AG2_REMOTE_TOKEN={self.SECRET}\nAGENT_MXID=@gone.agent:ag2.space\n")
        real_same = deleted_binding._same

        def connect_writes(path, text, st):
            Path(path).write_text("AG2_REMOTE_TOKEN=" + "56" * 24 + "\n")
            return real_same(path, text, st)
        with mock.patch.object(deleted_binding, "_same", connect_writes):
            self.assertEqual(deleted_binding.retire(str(race), **args), [])
        self.assertEqual(race.read_text(), "AG2_REMOTE_TOKEN=" + "56" * 24 + "\n")
        self.assertFalse(Path(str(race) + ".retired").exists())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith(".race")), [])

    PRIMARY_ID = "@primary.agent:ag2.space"
    LANE_ID = "@lane.agent:s.space"

    def _desktop_files(self):
        """A desktop install with an s.space sign-in, as the connections launcher runs its lane: the bridge's
        token file is $CLAUDE_CONFIG_DIR/channels/s.space/.env (<workspace>/.claude-sutando), and the host
        keeps its own copies under SUTANDO_APP_SUPPORT: channels/ag2space/.env with the primary bearer, its one
        AGENT_MXID and the lane's launch key, and channels/s.space/.env with the lane's bearer and id."""
        app, ccd = self.dir / "app", self.dir / "workspace" / ".claude-sutando"
        primary = app / "channels" / "ag2space" / ".env"
        host_lane = app / "channels" / "s.space" / ".env"
        lane = ccd / "channels" / "s.space" / ".env"
        for f in (primary, host_lane, lane):
            f.parent.mkdir(parents=True)
        primary.write_text(f"AG2_REMOTE_TOKEN='https://chat.example/relay|{self.OTHER}'\n"
                           f"AGENT_MXID={self.PRIMARY_ID}\n"
                           f"AG2_REMOTE_TOKEN_S_DSPACE='https://chat.s.example/relay|{self.SECRET}'\n")
        for f in (host_lane, lane):
            f.write_text(f"AG2_REMOTE_TOKEN='https://chat.s.example/relay|{self.SECRET}'\n"
                         f"AGENT_MXID={self.LANE_ID}\n")
        env = {"CLAUDE_CONFIG_DIR": str(ccd), "REMOTE_TASK_CHANNEL_DIR": "s.space",
               "SUTANDO_APP_SUPPORT": str(app)}
        saved = {k: gw.os.environ.get(k) for k in (*env, "AG2_DEVICE_ENV")}
        gw.os.environ.pop("AG2_DEVICE_ENV", None)   # the lane launcher drops it for a named lane
        gw.os.environ.update(env)

        def restore():
            for k, v in saved.items():
                if v is None:
                    gw.os.environ.pop(k, None)
                else:
                    gw.os.environ[k] = v
        self.addCleanup(restore)
        return primary, host_lane, lane

    def test_a_deleted_lane_agent_is_cleared_from_its_workspace_file_and_the_hosts_copies(self):
        primary, host_lane, lane = self._desktop_files()
        gw.TOKEN, gw.TOKEN_FILE = self.SECRET, str(lane)
        self.assertTrue(gw._retire_deleted_binding(self.LANE_ID))
        self.assertEqual(lane.read_text(), "", "the bridge's own token file")
        self.assertEqual(host_lane.read_text(), "", "the host's lane file")
        self.assertEqual(primary.read_text(), f"AG2_REMOTE_TOKEN='https://chat.example/relay|{self.OTHER}'\n"
                                              f"AGENT_MXID={self.PRIMARY_ID}\n", "the primary agent stays")
        self.assertIn("AG2_REMOTE_TOKEN_S_DSPACE", Path(str(primary) + ".retired").read_text())

    def test_the_single_id_naming_the_other_lane_proves_nothing(self):
        primary, host_lane, lane = self._desktop_files()
        for f in (host_lane, lane):
            f.unlink()
        before = primary.read_text()
        gw.TOKEN, gw.TOKEN_FILE = self.SECRET, str(primary)
        self.assertFalse(gw._retire_deleted_binding(self.LANE_ID))
        self.assertEqual(primary.read_text(), before)

    def test_a_deleted_primary_agent_keeps_the_lane_key(self):
        primary, host_lane, lane = self._desktop_files()
        lane_before = lane.read_text()
        gw.TOKEN, gw.TOKEN_FILE = self.OTHER, str(primary)
        self.assertTrue(gw._retire_deleted_binding(self.PRIMARY_ID))
        self.assertEqual(primary.read_text(),
                         f"AG2_REMOTE_TOKEN_S_DSPACE='https://chat.s.example/relay|{self.SECRET}'\n")
        self.assertEqual((lane.read_text(), host_lane.read_text()), (lane_before, lane_before))

    def test_retirement_paths_name_the_token_file_channel_files_and_the_hosts_copies(self):
        from ag2_sparrow import gateway_credential_sources as sources
        env = {"AG2_DEVICE_ENV": "/a/dev.env", "CLAUDE_CONFIG_DIR": "/w/.claude-sutando",
               "REMOTE_TASK_CHANNEL_DIR": "s.space", "SUTANDO_APP_SUPPORT": "/app"}
        self.assertEqual(sources.retirement_paths(env, "/w/.claude-sutando/channels/s.space/.env"),
                         ["/w/.claude-sutando/channels/s.space/.env", "/a/dev.env",
                          "/app/channels/ag2space/.env", "/app/channels/s.space/.env"])
        self.assertEqual(sources.retirement_paths({}, ""), [])

    def test_a_proven_bearer_moves_without_a_tie_but_never_another_agents_id(self):
        from ag2_sparrow import gateway_credential_sources as sources
        text = f"AG2_REMOTE_TOKEN_S_DSPACE={self.SECRET}\nAGENT_MXID={self.PRIMARY_ID}\n"
        self.assertEqual(sources.split_deleted_binding(text, secret=self.SECRET, agent_id=self.LANE_ID)[2], [])
        kept, _removed, keys = sources.split_deleted_binding(text, secret=self.SECRET, agent_id=self.LANE_ID,
                                                             bearer_proven=True)
        self.assertEqual((kept, keys), (f"AGENT_MXID={self.PRIMARY_ID}\n", ["AG2_REMOTE_TOKEN_S_DSPACE"]))
        self.assertEqual(sources.split_deleted_binding(f"AGENT_MXID={self.LANE_ID}\n", secret=self.SECRET,
                                                       agent_id=self.LANE_ID, bearer_proven=True)[2], [])

    def test_the_sidecar_is_never_written_through_a_link(self):
        from ag2_sparrow import deleted_binding
        self.env.write_text(f"AG2_REMOTE_TOKEN={self.SECRET}\nAGENT_MXID=@gone.agent:ag2.space\n")
        elsewhere = self.dir / "elsewhere"
        elsewhere.write_text("")
        Path(str(self.env) + ".retired").symlink_to(elsewhere)
        with self.assertRaises(OSError):
            deleted_binding.retire(str(self.env), secret=self.SECRET, agent_id="@gone.agent:ag2.space")
        self.assertEqual(elsewhere.read_text(), "")
        self.assertIn(self.SECRET, self.env.read_text())

    def test_a_live_bearer_beside_a_stale_deleted_id_in_the_file_is_never_retired(self):
        # The file itself records the deleted id beside a token that still works: one transient 401,
        # then the claim's id-only 410. The bearer is tested before anything moves, and it is accepted.
        before = self.env.read_text()
        self._answer(410, b'{"error":"agent_deleted"}')
        gw._bearer_check = lambda: "accepted"
        gw._heartbeat_singleton = lambda: self.fail("waited instead of testing the bearer first")
        self.assertTrue(gw._recover_auth(401))
        self.assertEqual(self.env.read_text(), before, "the binding stays live")
        self.assertFalse(Path(str(self.env) + ".retired").exists())
        self.assertNotIn("deleted", gw._reenroll_state)

    def test_an_unknown_bearer_check_never_retires(self):
        self._answer(410, b'{"error":"agent_deleted"}')
        answers = iter(["unknown", "unknown", "refused"])
        gw._bearer_check = lambda: next(answers)
        seen = []

        def beat():
            seen.append(self.SECRET in self.env.read_text())
            return len(seen) < 3
        gw._heartbeat_singleton = beat
        with self.assertRaises(SystemExit):
            gw._recover_auth(401)
        self.assertEqual(seen, [True, True, False], "retired only after the bearer itself was refused")

    def test_the_bearer_check_reads_only_a_401_as_refused(self):
        def raising(exc):
            def req(*a, **k):
                raise exc
            return req
        saved = gw._req
        self.addCleanup(setattr, gw, "_req", saved)
        gw._bearer_check = self._saved["_bearer_check"]
        for exc, want in ((urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b"")), "refused"),
                          (urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(b"")), "unknown"),
                          (urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(
                              b'{"code":"AGENT_CREDENTIAL_REVOKED"}')), "unknown"),
                          (urllib.error.HTTPError("u", 502, "x", {}, io.BytesIO(b"")), "unknown"),
                          (OSError("down"), "unknown")):
            gw._req = raising(exc)
            self.assertEqual(gw._bearer_check(), want, exc)
        gw._req = lambda *a, **k: {"agents": []}
        self.assertEqual(gw._bearer_check(), "accepted")

    def test_an_edge_403_after_the_id_only_410_never_retires_a_live_token(self):
        # The reviewer's sequence: the file pairs a stale deleted id with a working token; one transient
        # 401, the claim's id-only 410, then the bearer check meets a Cloudflare 1020 page, not the gateway.
        before = self.env.read_text()
        self._answer(410, b'{"error":"agent_deleted"}')
        gw._bearer_check = self._saved["_bearer_check"]
        cloudflare = (b"<!DOCTYPE html><title>Access denied | chat.example used Cloudflare to restrict access"
                      b"</title><p>Error code 1020</p>")
        answers = iter([urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(cloudflare)), None])

        def req(*a, **k):
            exc = next(answers)
            if exc:
                raise exc
            return {"agents": []}   # the next pass: the token works
        saved = gw._req
        gw._req = req
        self.addCleanup(setattr, gw, "_req", saved)
        beats = {"n": 0}

        def beat():   # bounded: a regression that retires exits instead of hanging
            beats["n"] += 1
            return beats["n"] < 3
        gw._heartbeat_singleton = beat
        gw._reload_rotated_token = lambda: False
        self.addCleanup(setattr, gw, "_reload_rotated_token", self._saved_reload)
        self.assertTrue(gw._recover_auth(401))
        self.assertEqual(self.env.read_text(), before, "the live token stays")
        self.assertFalse(Path(str(self.env) + ".retired").exists())

    def test_joined_and_bare_lines_holding_the_same_secret_both_match(self):
        # connections hands a lane its bearer joined to its gateway (<url>|<secret>); the bridge holds
        # the bare secret either way, and a file line matches in every shape a writer may use.
        from ag2_sparrow import gateway_credential_sources as sources
        url = "https://chat.example/relay"
        for raw in (f"{url}|{self.SECRET}", f"{url}%7C{self.SECRET}", self.SECRET):
            found = sources.discover({"REMOTE_TASK_TOKEN": raw, "REMOTE_TASK_URL": "" if "|" in raw or "%7C" in raw
                                      else url})
            self.assertEqual(found.token, self.SECRET, raw)
        for line in (f"AG2_REMOTE_TOKEN='{url}|{self.SECRET}'", f"AG2_REMOTE_TOKEN={url}%7C{self.SECRET}",
                     f"REMOTE_TASK_TOKEN={self.SECRET}", f'export REMOTE_TASK_TOKEN="{self.SECRET}"'):
            text = f"{line}\nAGENT_MXID=@gone.agent:ag2.space\nREMOTE_TASK_URL={url}\n"
            kept, _r, keys = sources.split_deleted_binding(text, secret=self.SECRET, agent_id="@gone.agent:ag2.space")
            self.assertEqual((kept, len(keys)), (f"REMOTE_TASK_URL={url}\n", 2), line)
        both = (f"AG2_REMOTE_TOKEN='{url}|{self.SECRET}'\nREMOTE_TASK_TOKEN={self.SECRET}\n"
                f"AGENT_MXID=@gone.agent:ag2.space\n")
        self.assertEqual(sources.split_deleted_binding(both, secret=self.SECRET, agent_id="@gone.agent:ag2.space")[2],
                         ["AG2_REMOTE_TOKEN", "REMOTE_TASK_TOKEN", "AGENT_MXID"])
        # A joined value is never the secret itself: matching it unsplit would miss every joined line.
        self.assertEqual(sources.split_deleted_binding(both, secret=f"{url}|{self.SECRET}",
                                                       agent_id="@gone.agent:ag2.space")[2], [])

    def test_the_bearer_check_sends_the_bare_secret(self):
        sent = []
        saved = gw._req
        self.addCleanup(setattr, gw, "_req", saved)
        gw._bearer_check = self._saved["_bearer_check"]
        gw._req = lambda *a, **k: sent.append(gw._AUTH_HEADERS["Authorization"]) or {}
        gw._AUTH_HEADERS["Authorization"] = f"Bearer {self.SECRET}"
        self.addCleanup(gw._AUTH_HEADERS.__setitem__, "Authorization", f"Bearer {self._saved['TOKEN']}")
        self.assertEqual(gw._bearer_check(), "accepted")
        self.assertEqual(sent, [f"Bearer {self.SECRET}"])

if __name__ == "__main__":
    import urllib.request  # noqa: F401 — used via gw.urllib in fixtures
    unittest.main(verbosity=1)
