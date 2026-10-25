#!/usr/bin/env python3
"""Contract tests for src/local_api_gate.py — the one gate for the loopback services.

Pins: the per-install bearer is minted once (0600, atomic), SUTANDO_API_TOKEN wins over
it, the bearer compare is constant-time and refuses an empty expectation, the
connections gate refuses a foreign Host, a wrong port, a non-loopback Origin and a
missing bearer in that order, and the dashboard's mutation gate is this module's.

Run: python3 tests/local-api-gate.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import local_api_gate as G  # noqa: E402


class Token(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_minted_once_private_and_stable(self):
        first = G.local_api_token(self.ws)
        path = self.ws / "state" / G.LOCAL_API_TOKEN_NAME
        self.assertTrue(len(first) >= 32)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(path.read_text(), first + "\n")
        self.assertEqual(G.local_api_token(self.ws), first)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), [G.LOCAL_API_TOKEN_NAME], "no temp file left")

    def test_configured_token_wins_and_an_empty_file_is_reminted(self):
        self.assertEqual(G.local_api_token(self.ws, "operator"), "operator")
        self.assertFalse((self.ws / "state" / G.LOCAL_API_TOKEN_NAME).exists())
        path = self.ws / "state" / G.LOCAL_API_TOKEN_NAME
        path.parent.mkdir(parents=True)
        path.write_text("\n")
        self.assertTrue(G.local_api_token(self.ws))

    def test_bearer_compare(self):
        self.assertTrue(G.bearer_matches("Bearer abc", "abc"))
        self.assertTrue(G.bearer_matches("bearer abc", "abc"))
        self.assertTrue(G.bearer_matches("abc", "abc"))
        self.assertFalse(G.bearer_matches("Bearer abd", "abc"))
        self.assertFalse(G.bearer_matches("", "abc"))
        self.assertFalse(G.bearer_matches("Bearer ", "abc"))
        self.assertFalse(G.bearer_matches("Bearer x", ""), "an empty expectation never matches")
        self.assertFalse(G.bearer_matches("Bearer caf\u00e9", "cafe"), "a non-ASCII header is a mismatch, not an error")
        self.assertTrue(G.bearer_matches("Bearer caf\u00e9", "caf\u00e9"))
        self.assertEqual(G.bearer_request_allowed(None, "127.0.0.1", "Bearer caf\u00e9", expected_token="t",
                                                  bound_port=None), (401, "unauthorized"))


class Gate(unittest.TestCase):
    def allowed(self, origin=None, host="127.0.0.1:7843", auth="Bearer t", expected="t", port=7843):
        return G.bearer_request_allowed(origin, host, auth, expected_token=expected, bound_port=port)

    def test_happy_paths(self):
        self.assertEqual(self.allowed(), (200, None))
        self.assertEqual(self.allowed(origin="http://localhost:5173"), (200, None))
        self.assertEqual(self.allowed(host="localhost:7843"), (200, None))
        self.assertEqual(self.allowed(host="[::1]:7843"), (200, None))
        self.assertEqual(self.allowed(host="127.0.0.1"), (200, None), "a Host without a port names the default")
        self.assertEqual(self.allowed(port=None), (200, None))

    def test_refusals_in_order(self):
        self.assertEqual(self.allowed(host="evil.example:7843"), (403, "host not allowed"))
        self.assertEqual(self.allowed(host=""), (403, "host not allowed"))
        self.assertEqual(self.allowed(host="127.0.0.1:7844"), (403, "host not allowed"))
        self.assertEqual(self.allowed(host="127.0.0.1:bad"), (200, None), "an unparseable port is no port")
        self.assertEqual(self.allowed(origin="https://evil.example"), (403, "cross-origin request refused"))
        self.assertEqual(self.allowed(origin="null"), (403, "cross-origin request refused"))
        self.assertEqual(self.allowed(origin="http://[bad"), (403, "cross-origin request refused"))
        self.assertEqual(self.allowed(auth=""), (401, "unauthorized"))
        self.assertEqual(self.allowed(auth="Bearer wrong"), (401, "unauthorized"))
        self.assertEqual(self.allowed(origin="https://evil.example", auth=""), (403, "cross-origin request refused"),
                         "the origin refusal comes before the bearer, so an attacker learns nothing about it")

    def test_helpers(self):
        self.assertEqual(G._authority_host("[::1]:80"), "::1")
        self.assertEqual(G._authority_host("Localhost:1"), "localhost")
        self.assertEqual(G._authority_port("[::1]:80"), 80)
        self.assertIsNone(G._authority_port("localhost"))
        self.assertIsNone(G._authority_port("x:y:z"))
        self.assertTrue(G.loopback_origin("") and G.loopback_origin("app://x.localhost"))
        self.assertFalse(G.loopback_origin("http://localhost.evil.example"))


class PrivateHistoryGate(unittest.TestCase):
    REFUSED = (403, "task history requires a local proxy or API token")

    def allowed(self, ip="127.0.0.1", origin=None, auth="", token=""):
        return G.private_history_request_allowed(ip, origin, auth, configured_token=token)

    def test_configured_token_decides_alone(self):
        self.assertEqual(self.allowed(auth="Bearer t", token="t"), (200, None))
        self.assertEqual(self.allowed(ip="203.0.113.9", origin="https://evil.example", auth="Bearer t", token="t"),
                         (200, None), "with a token the client address and Origin do not matter")
        self.assertEqual(self.allowed(auth="Bearer wrong", token="t"), (401, "unauthorized"))
        self.assertEqual(self.allowed(auth="", token="t"), (401, "unauthorized"), "no header is 401, never 403")
        self.assertEqual(self.allowed(auth="Bearer café", token="t"), (401, "unauthorized"),
                         "a non-ASCII header is a mismatch, not an error")

    def test_without_a_token_only_a_silent_loopback_client_passes(self):
        self.assertEqual(self.allowed(), (200, None))
        self.assertEqual(self.allowed(ip="::1"), (200, None))
        self.assertEqual(self.allowed(origin=""), (200, None), "an empty Origin counts as absent")
        self.assertEqual(self.allowed(origin="http://127.0.0.1:5173"), self.REFUSED,
                         "any Origin, loopback included, is a browser page")
        self.assertEqual(self.allowed(ip="203.0.113.9"), self.REFUSED)
        self.assertEqual(self.allowed(ip="not-an-ip"), self.REFUSED)
        self.assertEqual(self.allowed(ip=""), self.REFUSED)
        self.assertEqual(self.allowed(ip="203.0.113.9", auth="Bearer anything"), self.REFUSED,
                         "a bearer buys nothing when no token is configured")


class DashboardDelegation(unittest.TestCase):
    def test_dashboard_uses_this_gate(self):
        spec = importlib.util.spec_from_file_location("dashboard_under_test", ROOT / "src" / "dashboard.py")
        dashboard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dashboard)
        self.assertIs(dashboard.mutation_request_allowed, G.mutation_request_allowed)
        self.assertIs(dashboard._authority_host, G._authority_host)
        ok, why = G.mutation_request_allowed("http://127.0.0.1:8080", "127.0.0.1:8080", "application/json",
                                             expect_body=True)
        self.assertEqual((ok, why), (True, None))
        self.assertEqual(G.mutation_request_allowed(None, "127.0.0.1", None, expect_body=False)[1], "missing Origin")
        self.assertIn("wildcard", G.mutation_request_allowed("http://x", "x", None, expect_body=False, bind="0.0.0.0")[1])


if __name__ == "__main__":
    unittest.main(verbosity=1)
