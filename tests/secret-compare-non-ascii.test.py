#!/usr/bin/env python3
"""A credential value holding a non-ASCII character is refused like any wrong value.

`hmac.compare_digest` raises TypeError on a non-ASCII `str`; each request-side check
must answer with its normal refusal instead, and a valid value must still pass.

Run: python3 tests/secret-compare-non-ascii.test.py   (stdlib only)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import importlib.util
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
_TMP = tempfile.TemporaryDirectory()
os.environ["SUTANDO_WORKSPACE"] = _TMP.name
os.environ["SUTANDO_TELEMETRY"] = "0"
sys.path.insert(0, str(REPO / "src"))

import local_api_gate  # noqa: E402
from secret_compare import secret_equal  # noqa: E402

NON_ASCII = "tést-☃"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class SecretEqualTests(unittest.TestCase):
    def test_match_and_mismatch(self):
        self.assertTrue(secret_equal("abc123", "abc123"))
        self.assertFalse(secret_equal("abc124", "abc123"))

    def test_non_ascii_is_a_mismatch(self):
        self.assertFalse(secret_equal(NON_ASCII, "abc123"))
        self.assertFalse(secret_equal("abc123", NON_ASCII + "x"))
        self.assertTrue(secret_equal(NON_ASCII, NON_ASCII))

    def test_malformed_values_are_mismatches(self):
        for presented, expected in [(None, "a"), (b"a", "a"), ("", ""), ("a", ""),
                                    ("\ud800", "\ud800")]:
            self.assertFalse(secret_equal(presented, expected), (presented, expected))

    def test_bearer_gate(self):
        self.assertTrue(local_api_gate.bearer_matches("Bearer tok-1", "tok-1"))
        self.assertFalse(local_api_gate.bearer_matches("Bearer " + NON_ASCII, "tok-1"))


class TwilioSignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load("agent_api_nonascii", "src/agent-api.py")

    def _handler(self, signature: str):
        h = mock.MagicMock()
        h.headers = {"X-Twilio-Signature": signature, "Host": "example.com"}
        h.path = "/twilio/voice"
        return h

    def test_non_ascii_refused_valid_passes(self):
        body = "CallSid=CA123"
        with mock.patch.dict(os.environ, {"TWILIO_AUTH_TOKEN": "tok"}), \
             mock.patch.object(self.mod, "config_get", return_value=""):
            self.assertFalse(self.mod.validate_twilio_signature(self._handler(NON_ASCII), body))
            mac = hmac.new(b"tok", b"https://example.com/twilio/voiceCallSidCA123", hashlib.sha1)
            good = base64.b64encode(mac.digest()).decode()
            self.assertTrue(self.mod.validate_twilio_signature(self._handler(good), body))


class GithubSignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load("github_webhook_nonascii", "src/github-webhook.py")

    def test_non_ascii_refused_valid_passes(self):
        body = b'{"action":"opened"}'
        with mock.patch.object(self.mod, "WEBHOOK_SECRET", "s3cret"):
            self.assertFalse(self.mod.verify_github_signature(body, "sha256=" + NON_ASCII))
            self.assertFalse(self.mod.verify_github_signature(body, None))
            good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
            self.assertTrue(self.mod.verify_github_signature(body, good))


class CaptureTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load("screen_capture_nonascii", "src/screen-capture-server.py")

    def _check(self, supplied: str):
        h = self.mod.Handler.__new__(self.mod.Handler)
        h.headers = {"X-Sutando-Capture-Token": supplied}
        sent = []
        h._send_json = lambda code, payload: sent.append(code)
        with mock.patch.object(self.mod, "CAPTURE_TOKEN", "cap-token"):
            return h._require_capture_token(), sent

    def test_non_ascii_refused_valid_passes(self):
        self.assertEqual(self._check(NON_ASCII), (False, [403]))
        self.assertEqual(self._check("cap-token"), (True, []))


class WorkspaceServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        serve = _load("lws_serve_nonascii", "skills/local-workspace-server/scripts/serve.py")
        cls.root = tempfile.TemporaryDirectory()
        (Path(cls.root.name) / "index.html").write_text("<h1>deck</h1>")
        cls.srv, cls.cap = serve.build_server(Path(cls.root.name), port=0, ttl=3600)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.root.cleanup()

    def _status(self, path: str) -> int:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_non_ascii_capability_refused_valid_passes(self):
        self.assertEqual(self._status("/%C3%A9%E2%98%83/index.html"), 404)
        self.assertEqual(self._status(f"/{self.cap}/index.html"), 200)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        _TMP.cleanup()
