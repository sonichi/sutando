#!/usr/bin/env python3
"""Security regression: validate_twilio_signature must fail CLOSED when
TWILIO_AUTH_TOKEN is not set.

Finding #2 from the 2026-07-06 source-code security audit: the original
implementation returned True (accept all) when auth_token was empty,
allowing unauthenticated Twilio webhook requests to create tasks in the
agent.

The fix: return False when TWILIO_AUTH_TOKEN is unset so that /twilio/*
endpoints always reject unauthenticated requests. Operators must set the
token in .env for Twilio webhooks to work.

Run: python3 tests/agent-api-twilio-auth.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parent.parent


def _load_agent_api() -> object:
    spec = importlib.util.spec_from_file_location("agent_api", REPO / "src" / "agent-api.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TwilioAuthTests(unittest.TestCase):
    def setUp(self):
        # Ensure no token in env for the fail-closed tests.
        self._orig = os.environ.pop("TWILIO_AUTH_TOKEN", None)
        self.mod = _load_agent_api()

    def tearDown(self):
        if self._orig is not None:
            os.environ["TWILIO_AUTH_TOKEN"] = self._orig
        else:
            os.environ.pop("TWILIO_AUTH_TOKEN", None)

    def _make_handler(self, headers: dict | None = None) -> MagicMock:
        h = MagicMock()
        h.headers = {**(headers or {})}
        h.path = "/twilio/voice"
        return h

    def test_no_token_rejects_request(self):
        """TWILIO_AUTH_TOKEN unset → validate_twilio_signature returns False (fail closed)."""
        os.environ.pop("TWILIO_AUTH_TOKEN", None)
        handler = self._make_handler()
        result = self.mod.validate_twilio_signature(handler, "CallSid=CA123&To=%2B1555")
        self.assertFalse(result, "Must reject when TWILIO_AUTH_TOKEN is not configured")

    def test_empty_token_rejects_request(self):
        """TWILIO_AUTH_TOKEN='' (explicitly empty) → rejects."""
        os.environ["TWILIO_AUTH_TOKEN"] = ""
        handler = self._make_handler()
        result = self.mod.validate_twilio_signature(handler, "CallSid=CA123")
        self.assertFalse(result)

    def test_valid_token_with_missing_signature_rejects(self):
        """Token configured but X-Twilio-Signature header absent → rejects."""
        os.environ["TWILIO_AUTH_TOKEN"] = "test_token_abc"
        handler = self._make_handler({"Host": "example.com"})
        result = self.mod.validate_twilio_signature(handler, "CallSid=CA123")
        self.assertFalse(result)

    def test_valid_token_with_wrong_signature_rejects(self):
        """Token configured but signature doesn't match → rejects."""
        os.environ["TWILIO_AUTH_TOKEN"] = "test_token_abc"
        handler = self._make_handler({
            "Host": "example.com",
            "X-Forwarded-Proto": "https",
            "X-Twilio-Signature": "badsig",
        })
        os.environ["TWILIO_WEBHOOK_URL"] = "https://example.com"
        try:
            result = self.mod.validate_twilio_signature(handler, "CallSid=CA123")
        finally:
            os.environ.pop("TWILIO_WEBHOOK_URL", None)
        self.assertFalse(result)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
