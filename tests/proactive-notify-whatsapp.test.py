#!/usr/bin/env python3
"""Tests for skills/proactive-notify/scripts/actions/whatsapp.py (closes #965)."""
from __future__ import annotations
import importlib.util
import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
MOD_PATH = REPO / "skills" / "proactive-notify" / "scripts" / "actions" / "whatsapp.py"

spec = importlib.util.spec_from_file_location("whatsapp", MOD_PATH)
wa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wa)

BASE_ENV = {
    "TWILIO_ACCOUNT_SID": "ACtest",
    "TWILIO_AUTH_TOKEN": "token123",
    "OWNER_WHATSAPP_NUMBER": "+15550001234",
}


class TestWaPrefix(unittest.TestCase):
    def test_adds_prefix(self):
        self.assertEqual(wa._wa("+15550001234"), "whatsapp:+15550001234")

    def test_no_double_prefix(self):
        self.assertEqual(wa._wa("whatsapp:+15550001234"), "whatsapp:+15550001234")

    def test_strips_whitespace(self):
        self.assertEqual(wa._wa("  +15550001234  "), "whatsapp:+15550001234")


class TestSendMissingCreds(unittest.TestCase):
    def _send(self, env: dict) -> dict:
        with patch.dict("os.environ", env, clear=True):
            with patch.object(wa, "_load_env", return_value={}):
                return wa.send(None, "test message")

    def test_fails_without_sid(self):
        env = {k: v for k, v in BASE_ENV.items() if k != "TWILIO_ACCOUNT_SID"}
        result = self._send(env)
        self.assertFalse(result["ok"])
        self.assertIn("TWILIO_ACCOUNT_SID", result["error"])

    def test_fails_without_token(self):
        env = {k: v for k, v in BASE_ENV.items() if k != "TWILIO_AUTH_TOKEN"}
        result = self._send(env)
        self.assertFalse(result["ok"])
        self.assertIn("TWILIO_AUTH_TOKEN", result["error"])

    def test_fails_without_owner_number(self):
        env = {k: v for k, v in BASE_ENV.items() if k not in ("OWNER_WHATSAPP_NUMBER",)}
        result = self._send(env)
        self.assertFalse(result["ok"])
        self.assertIn("OWNER_WHATSAPP_NUMBER", result["error"])


class TestSendOwnerNumberFallback(unittest.TestCase):
    """OWNER_NUMBER is used when OWNER_WHATSAPP_NUMBER is absent."""

    def test_falls_back_to_owner_number(self):
        env = dict(BASE_ENV)
        del env["OWNER_WHATSAPP_NUMBER"]
        env["OWNER_NUMBER"] = "+15559999"

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"sid": "SMtest"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.dict("os.environ", env, clear=True):
            with patch.object(wa, "_load_env", return_value={}):
                with patch("urllib.request.urlopen", return_value=mock_resp) as mock_ul:
                    result = wa.send(None, "hello")

        self.assertTrue(result["ok"])
        called_req = mock_ul.call_args[0][0]
        body = called_req.data.decode()
        self.assertIn("whatsapp%3A%2B15559999", body)  # URL-encoded whatsapp:+15559999


class TestSendRequest(unittest.TestCase):
    def _mock_send(self, extra_env: dict | None = None) -> tuple[dict, "MagicMock"]:
        env = dict(BASE_ENV)
        if extra_env:
            env.update(extra_env)

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"sid": "SMwhatsapp123"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.dict("os.environ", env, clear=True):
            with patch.object(wa, "_load_env", return_value={}):
                with patch("urllib.request.urlopen", return_value=mock_resp) as mock_ul:
                    result = wa.send(None, "Meeting in 10 min")
                    return result, mock_ul

    def test_returns_ok_with_sid(self):
        result, _ = self._mock_send()
        self.assertTrue(result["ok"])
        self.assertEqual(result["id"], "SMwhatsapp123")

    def test_from_uses_sandbox_by_default(self):
        result, mock_ul = self._mock_send()
        req = mock_ul.call_args[0][0]
        body = req.data.decode()
        # URL-encoded "whatsapp:+14155238886"
        self.assertIn("whatsapp%3A%2B14155238886", body)

    def test_from_uses_custom_whatsapp_number(self):
        result, mock_ul = self._mock_send({"TWILIO_WHATSAPP_NUMBER": "+18005551234"})
        req = mock_ul.call_args[0][0]
        body = req.data.decode()
        self.assertIn("whatsapp%3A%2B18005551234", body)

    def test_to_has_whatsapp_prefix(self):
        result, mock_ul = self._mock_send()
        req = mock_ul.call_args[0][0]
        body = req.data.decode()
        # owner number +15550001234 → whatsapp:+15550001234
        self.assertIn("whatsapp%3A%2B15550001234", body)

    def test_posts_to_correct_twilio_url(self):
        result, mock_ul = self._mock_send()
        req = mock_ul.call_args[0][0]
        self.assertIn("ACtest", req.full_url)
        self.assertIn("Messages.json", req.full_url)

    def test_network_error_returns_not_ok(self):
        env = dict(BASE_ENV)
        with patch.dict("os.environ", env, clear=True):
            with patch.object(wa, "_load_env", return_value={}):
                with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
                    result = wa.send(None, "hello")
        self.assertFalse(result["ok"])
        self.assertIn("timeout", result["error"])

    def test_body_included_in_request(self):
        result, mock_ul = self._mock_send()
        req = mock_ul.call_args[0][0]
        body = req.data.decode()
        self.assertIn("Meeting+in+10+min", body)


class TestWhatsappModuleExists(unittest.TestCase):
    def test_module_file_exists(self):
        self.assertTrue(MOD_PATH.exists(), f"whatsapp.py not found at {MOD_PATH}")

    def test_send_function_callable(self):
        self.assertTrue(callable(wa.send))

    def test_sandbox_number_constant(self):
        self.assertEqual(wa.TWILIO_SANDBOX_NUMBER, "+14155238886")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestWaPrefix, TestSendMissingCreds, TestSendOwnerNumberFallback,
        TestSendRequest, TestWhatsappModuleExists,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
