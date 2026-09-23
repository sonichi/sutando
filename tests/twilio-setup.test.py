#!/usr/bin/env python3
"""twilio-setup.py against a fake Twilio: numbers, buy, set-webhook, status,
verbatim error surfacing, credential resolution and the in-place .env writer.

Run: python3 tests/twilio-setup.test.py
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "skills" / "phone-conversation" / "scripts" / "twilio-setup.py"

SID, TOKEN = "ACtest", "secret"
OWNED = {"sid": "PN1", "phone_number": "+14155550100", "voice_url": "https://old.example/twilio/connect"}


class FakeTwilio(BaseHTTPRequestHandler):
    calls: list = []
    fail_buy: dict | None = None
    trial = False

    def log_message(self, *a):  # quiet
        pass

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        FakeTwilio.calls.append(("GET", u.path, parse_qs(u.query), self.headers.get("Authorization")))
        if u.path.endswith(f"/Accounts/{SID}.json"):
            return self._send(200, {"friendly_name": "Acme", "status": "active",
                                    "type": "Trial" if FakeTwilio.trial else "Full"})
        if u.path.endswith("/IncomingPhoneNumbers.json"):
            return self._send(200, {"incoming_phone_numbers": [OWNED]})
        if "/AvailablePhoneNumbers/US/Local.json" in u.path:
            return self._send(200, {"available_phone_numbers": [
                {"phone_number": "+14155550101", "locality": "San Francisco", "region": "CA"},
                {"phone_number": "+14155550102", "locality": "Oakland", "region": "CA"}]})
        self._send(404, {"message": "not found", "code": 20404})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        FakeTwilio.calls.append(("POST", u.path, form, self.headers.get("Authorization")))
        if u.path.endswith("/IncomingPhoneNumbers.json"):
            if FakeTwilio.fail_buy:
                return self._send(400, FakeTwilio.fail_buy)
            return self._send(201, {"sid": "PN2", "phone_number": form["PhoneNumber"],
                                    "voice_url": form.get("VoiceUrl")})
        if "/IncomingPhoneNumbers/PN1.json" in u.path:
            return self._send(200, {**OWNED, "voice_url": form.get("VoiceUrl"),
                                    "status_callback": form.get("StatusCallback")})
        self._send(404, {"message": "not found"})


def load_module():
    spec = importlib.util.spec_from_file_location("twilio_setup", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TwilioSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeTwilio)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeTwilio.calls = []
        FakeTwilio.fail_buy = None
        FakeTwilio.trial = False
        self.tmp = tempfile.mkdtemp(prefix="twilio-setup-")
        self.env_file = pathlib.Path(self.tmp, ".env")
        self.env_file.write_text("GEMINI_API_KEY=g\n# TWILIO_PHONE_NUMBER=+1xxxxxxxxxx\nOTHER=1\n")
        self.envp = mock.patch.dict(os.environ, {
            "TWILIO_API_BASE": f"http://127.0.0.1:{self.port}",
            "PHONE_SERVER_HEALTH": "http://127.0.0.1:1/health",   # nothing listening
            "TWILIO_ACCOUNT_SID": SID, "TWILIO_AUTH_TOKEN": TOKEN,
        }, clear=False)
        self.envp.start()
        for k in ("TWILIO_PHONE_NUMBER", "TWILIO_WEBHOOK_URL", "WEBHOOK_BASE_URL"):
            os.environ.pop(k, None)
        self.mod = load_module()

    def tearDown(self):
        self.envp.stop()

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.mod.run(["--env-file", str(self.env_file), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_numbers_lists_voice_capable_locals_with_basic_auth(self):
        rc, out, _ = self._run("numbers", "--area", "415", "--limit", "5")
        self.assertEqual(rc, 0)
        self.assertIn("+14155550101", out)
        m, path, q, auth = FakeTwilio.calls[0]
        self.assertIn("/AvailablePhoneNumbers/US/Local.json", path)
        self.assertEqual(q["AreaCode"], ["415"])
        self.assertEqual(q["VoiceEnabled"], ["true"])
        self.assertTrue(auth.startswith("Basic "))

    def test_buy_sets_webhook_and_writes_env_in_place(self):
        rc, out, _ = self._run("buy", "+14155550101", "--base", "https://t.ngrok-free.app/")
        self.assertEqual(rc, 0, out)
        post = [c for c in FakeTwilio.calls if c[0] == "POST"][0]
        self.assertEqual(post[2]["PhoneNumber"], "+14155550101")
        self.assertEqual(post[2]["VoiceUrl"], "https://t.ngrok-free.app/twilio/connect")
        self.assertEqual(post[2]["StatusCallback"], "https://t.ngrok-free.app/twilio/status")
        text = self.env_file.read_text()
        self.assertEqual(text, "GEMINI_API_KEY=g\n# TWILIO_PHONE_NUMBER=+1xxxxxxxxxx\n"
                               "TWILIO_PHONE_NUMBER=+14155550101\nOTHER=1\n"
                               "TWILIO_WEBHOOK_URL=https://t.ngrok-free.app\n")

    def test_twilio_error_is_surfaced_verbatim(self):
        FakeTwilio.fail_buy = {"message": "Trial accounts cannot purchase numbers", "code": 21404,
                               "more_info": "https://www.twilio.com/docs/errors/21404"}
        rc, _, err = self._run("buy", "+14155550101", "--base", "https://t.example")
        self.assertEqual(rc, 1)
        self.assertIn("Trial accounts cannot purchase numbers", err)
        self.assertIn("21404", err)
        self.assertNotIn("TWILIO_PHONE_NUMBER=+14155550101", self.env_file.read_text())

    def test_set_webhook_points_the_configured_number_here(self):
        self.mod.set_env_var(self.env_file, "TWILIO_PHONE_NUMBER", "+14155550100")
        rc, out, err = self._run("set-webhook", "https://new.ngrok-free.app")
        self.assertEqual(rc, 0, err)
        post = [c for c in FakeTwilio.calls if c[0] == "POST"][0]
        self.assertIn("/IncomingPhoneNumbers/PN1.json", post[1])
        self.assertEqual(post[2]["VoiceUrl"], "https://new.ngrok-free.app/twilio/connect")
        self.assertIn("TWILIO_WEBHOOK_URL=https://new.ngrok-free.app", self.env_file.read_text())

    def test_set_webhook_base_defaults_to_env_when_server_is_down(self):
        self.mod.set_env_var(self.env_file, "TWILIO_PHONE_NUMBER", "+14155550100")
        self.mod.set_env_var(self.env_file, "WEBHOOK_BASE_URL", "https://from-env.example/")
        rc, _, err = self._run("set-webhook")
        self.assertEqual(rc, 0, err)
        post = [c for c in FakeTwilio.calls if c[0] == "POST"][0]
        self.assertEqual(post[2]["VoiceUrl"], "https://from-env.example/twilio/connect")

    def test_set_webhook_with_no_base_anywhere_is_an_error(self):
        rc, _, err = self._run("set-webhook")
        self.assertEqual(rc, 1)
        self.assertIn("no webhook base", err)

    def test_status_reports_drift_and_trial(self):
        FakeTwilio.trial = True
        self.mod.set_env_var(self.env_file, "TWILIO_PHONE_NUMBER", "+14155550100")
        self.mod.set_env_var(self.env_file, "TWILIO_WEBHOOK_URL", "https://here.example")
        rc, out, _ = self._run("--json", "status")
        self.assertEqual(rc, 0)
        j = json.loads(out)
        self.assertEqual(j["account"]["type"], "Trial")
        self.assertTrue(j["numbers"][0]["configured"])
        self.assertTrue(j["numbers"][0]["webhook_drift"])
        self.assertFalse(j["server_running"])

    def test_missing_credentials_exit_2_with_the_vault_hint(self):
        os.environ.pop("TWILIO_ACCOUNT_SID")
        with mock.patch.object(self.mod, "_resolve", None):
            rc, _, err = self._run("status")
        self.assertEqual(rc, 2)
        self.assertIn("vault set TWILIO_ACCOUNT_SID", err)

    def test_credentials_fall_back_to_the_env_file(self):
        os.environ.pop("TWILIO_ACCOUNT_SID")
        os.environ.pop("TWILIO_AUTH_TOKEN")
        self.mod.set_env_var(self.env_file, "TWILIO_ACCOUNT_SID", SID)
        self.mod.set_env_var(self.env_file, "TWILIO_AUTH_TOKEN", TOKEN)
        with mock.patch.object(self.mod, "_resolve", None):
            rc, out, err = self._run("numbers")
        self.assertEqual(rc, 0, err)

    def test_set_env_var_replaces_active_line_and_keeps_the_rest(self):
        p = pathlib.Path(self.tmp, "x.env")
        p.write_text("A=1\n# B=tpl\nA=2\n")
        self.mod.set_env_var(p, "A", "9")
        self.assertEqual(p.read_text(), "A=9\n# B=tpl\nA=2\n")
        self.mod.set_env_var(p, "B", "live")
        self.assertEqual(p.read_text(), "A=9\n# B=tpl\nB=live\nA=2\n")
        self.mod.set_env_var(p, "C", "new")
        self.assertEqual(p.read_text(), "A=9\n# B=tpl\nB=live\nA=2\nC=new\n")
        self.assertFalse(pathlib.Path(self.tmp, "x.env.tmp").exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
