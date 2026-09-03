#!/usr/bin/env python3
"""`backoff_s` is a TRANSPORT retry estimate; the auth-wait loop must not set it.

`_recover_auth`'s wait loop re-checks every `AUTH_RECHECK_INTERVAL` while it
waits for a human to rotate a token or approve a re-link. Emitting that cadence
as `backoff_s` made a human-blocked wait indistinguishable from a reconnecting
transport, because the only thing separating the two in the sidecar is whether
`backoff_s` is truthy.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from ag2_sparrow import remote_gateway_bridge as rgb  # noqa: E402


class AuthWaitBackoff(unittest.TestCase):
    def _emit(self, **kw) -> dict:
        """Emit into a scratch sidecar and return the payload the writer produced."""
        with tempfile.TemporaryDirectory() as d:
            # Must stay a Path: the writer calls .parent.mkdir()/.with_suffix(),
            # and it SWALLOWS every error, so a str here writes nothing, silently.
            path = Path(d) / "gateway-status.json"
            orig = rgb.GATEWAY_STATUS_FILE
            rgb.GATEWAY_STATUS_FILE = path
            try:
                rgb._emit_gateway_status(False, **kw)
                return json.loads(path.read_text())
            finally:
                rgb.GATEWAY_STATUS_FILE = orig

    def test_auth_wait_emits_zero_backoff(self):
        """The exact call the wait loop makes: no backoff_s, so it defaults to 0."""
        payload = self._emit(error="auth rejected HTTP 401 — waiting for re-connect")
        self.assertEqual(payload["backoff_s"], 0)
        self.assertFalse(payload["connected"])

    def test_relink_pending_variant_also_zero(self):
        """The other branch of the same emit (a re-link code is parked)."""
        payload = self._emit(error="auth rejected HTTP 401 — relink pending (code ABC)")
        self.assertEqual(payload["backoff_s"], 0)

    def test_transport_retry_still_reports_backoff(self):
        """Positive control: without this the assertions above pass on a writer
        that can no longer report ANY backoff, which would hide the real signal."""
        payload = self._emit(error="network: timed out", backoff_s=8)
        self.assertEqual(payload["backoff_s"], 8)

    def test_real_wait_loop_emits_zero_backoff(self):
        """Drives the PRODUCTION `_recover_auth` for one iteration and reads what
        it actually wrote — the regression lives at the call site, so asserting on
        `_emit_gateway_status` alone cannot catch it."""
        written: list[dict] = []
        reload_calls = {"n": 0}

        def _reload():
            # False before the loop (no rotation yet), True after one emit so the
            # loop returns instead of spinning.
            reload_calls["n"] += 1
            return reload_calls["n"] > 1

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "gateway-status.json"
            saved = {n: getattr(rgb, n) for n in (
                "GATEWAY_STATUS_FILE", "TOKEN_FILE", "_reload_rotated_token",
                "_heartbeat_singleton", "_reenroll_claim", "_reenroll_clear",
                "_log", "time")}
            try:
                rgb.GATEWAY_STATUS_FILE = path
                rgb.TOKEN_FILE = str(Path(d) / "token")
                rgb._reload_rotated_token = _reload
                rgb._heartbeat_singleton = lambda: True
                rgb._reenroll_claim = lambda *a, **k: None
                rgb._reenroll_clear = lambda *a, **k: None
                rgb._log = lambda *a, **k: None

                class _NoSleep:
                    sleep = staticmethod(lambda s: None)
                    time = staticmethod(lambda: 1_000_000.0)
                rgb.time = _NoSleep

                self.assertTrue(rgb._recover_auth(401))
                written.append(json.loads(path.read_text()))
            finally:
                for n, v in saved.items():
                    setattr(rgb, n, v)

        self.assertEqual(len(written), 1)
        self.assertEqual(
            written[0]["backoff_s"], 0,
            "the auth-wait loop emitted a transport backoff for a human-blocked wait")
        self.assertIn("auth rejected", written[0]["error"])


if __name__ == "__main__":
    unittest.main()
