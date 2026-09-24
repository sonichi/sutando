#!/usr/bin/env python3
"""twilio-setup.py against a fake Twilio (plus a fake phone-server /health):
numbers, buy, set-webhook, status, verbatim error surfacing, credential
resolution, the byte-preserving .env writer (private from its first byte,
line-break and NUL injection refused), the restart round trip — a moved
tunnel is reported as drift and re-pushed, never recorded as
TWILIO_WEBHOOK_URL — and the pin that the phone server resolves its
credentials the way the script does (env, then the Keychain vault).

Run: python3 tests/twilio-setup.test.py
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import stat
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
OWNED2 = {"sid": "PN9", "phone_number": "+14155550199", "voice_url": ""}
AVAILABLE = [{"phone_number": "+14155550101", "locality": "San Francisco", "region": "CA"},
             {"phone_number": "+14155550102", "locality": "Oakland", "region": "CA"}]


class FakeTwilio(BaseHTTPRequestHandler):
    """Twilio's REST shapes plus the phone server's /health on the same port."""
    calls: list = []
    fail_buy: dict | None = None
    trial = False
    owned: list = [OWNED]
    available: list = AVAILABLE
    health_url = ""          # "" → /health answers 503 (server down)
    raw_error = False        # a non-JSON 500 from every route

    def log_message(self, *a):  # quiet
        pass

    def _send(self, status, obj):
        body = json.dumps(obj).encode() if not isinstance(obj, bytes) else obj
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            if FakeTwilio.health_url:
                return self._send(200, {"status": "ok", "webhookUrl": FakeTwilio.health_url})
            return self._send(503, {"status": "down"})
        FakeTwilio.calls.append(("GET", u.path, parse_qs(u.query), self.headers.get("Authorization")))
        if FakeTwilio.raw_error:
            return self._send(500, b"<html>gateway timeout</html>")
        if u.path.endswith(f"/Accounts/{SID}.json"):
            return self._send(200, {"friendly_name": "Acme", "status": "active",
                                    "type": "Trial" if FakeTwilio.trial else "Full"})
        if u.path.endswith("/IncomingPhoneNumbers.json"):
            return self._send(200, {"incoming_phone_numbers": FakeTwilio.owned})
        if "/AvailablePhoneNumbers/US/Local.json" in u.path:
            return self._send(200, {"available_phone_numbers": FakeTwilio.available})
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
        for num in FakeTwilio.owned:
            if f"/IncomingPhoneNumbers/{num['sid']}.json" in u.path:
                return self._send(200, {**num, "voice_url": form.get("VoiceUrl"),
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
        cls.server.server_close()

    def setUp(self):
        FakeTwilio.calls = []
        FakeTwilio.fail_buy = None
        FakeTwilio.trial = False
        FakeTwilio.owned = [OWNED]
        FakeTwilio.available = AVAILABLE
        FakeTwilio.health_url = ""
        FakeTwilio.raw_error = False
        self.tmp = tempfile.mkdtemp(prefix="twilio-setup-")
        self.env_file = pathlib.Path(self.tmp, ".env")
        self.env_file.write_text("GEMINI_API_KEY=g\n# TWILIO_PHONE_NUMBER=+1xxxxxxxxxx\nOTHER=1\n")
        self.envp = mock.patch.dict(os.environ, {
            "TWILIO_API_BASE": f"http://127.0.0.1:{self.port}",
            "PHONE_SERVER_HEALTH": f"http://127.0.0.1:{self.port}/health",
            "TWILIO_ACCOUNT_SID": SID, "TWILIO_AUTH_TOKEN": TOKEN,
        }, clear=False)
        self.envp.start()
        for k in ("TWILIO_PHONE_NUMBER", "TWILIO_WEBHOOK_URL", "WEBHOOK_BASE_URL", "PHONE_PORT"):
            os.environ.pop(k, None)
        self.mod = load_module()

    def tearDown(self):
        self.envp.stop()

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.mod.run(["--env-file", str(self.env_file), *argv])
        return rc, out.getvalue(), err.getvalue()

    def env(self):
        return self.mod.env_file_dict(self.env_file)

    # ---------- commands ----------

    def test_numbers_lists_voice_capable_locals_with_basic_auth(self):
        rc, out, _ = self._run("numbers", "--area", "415", "--limit", "5")
        self.assertEqual(rc, 0)
        self.assertIn("+14155550101", out)
        m, path, q, auth = FakeTwilio.calls[0]
        self.assertIn("/AvailablePhoneNumbers/US/Local.json", path)
        self.assertEqual(q["AreaCode"], ["415"])
        self.assertEqual(q["VoiceEnabled"], ["true"])
        self.assertTrue(auth.startswith("Basic "))

    def test_numbers_with_nothing_available_says_so(self):
        FakeTwilio.available = []
        rc, out, _ = self._run("numbers", "--area", "999")
        self.assertEqual(rc, 0)
        self.assertIn("No voice-capable local numbers available in US area 999", out)

    def test_buy_sets_webhook_and_records_the_pushed_base_not_the_server_key(self):
        rc, out, _ = self._run("buy", "+14155550101", "--base", "https://t.ngrok-free.app/")
        self.assertEqual(rc, 0, out)
        post = [c for c in FakeTwilio.calls if c[0] == "POST"][0]
        self.assertEqual(post[2]["PhoneNumber"], "+14155550101")
        self.assertEqual(post[2]["VoiceUrl"], "https://t.ngrok-free.app/twilio/connect")
        self.assertEqual(post[2]["StatusCallback"], "https://t.ngrok-free.app/twilio/status")
        text = self.env_file.read_text()
        self.assertEqual(text, "GEMINI_API_KEY=g\n# TWILIO_PHONE_NUMBER=+1xxxxxxxxxx\n"
                               "TWILIO_PHONE_NUMBER=+14155550101\nOTHER=1\n"
                               "TWILIO_WEBHOOK_PUSHED=https://t.ngrok-free.app\n")
        self.assertNotIn("TWILIO_WEBHOOK_URL", text, "the server's authoritative key is never written")

    def test_buy_without_a_base_anywhere_buys_and_says_what_is_left(self):
        rc, out, _ = self._run("buy", "+14155550101")
        self.assertEqual(rc, 0)
        self.assertNotIn("VoiceUrl", [c for c in FakeTwilio.calls if c[0] == "POST"][0][2])
        self.assertIn("No webhook base known yet", out)
        self.assertNotIn("TWILIO_WEBHOOK_PUSHED", self.env_file.read_text())

    def test_buy_rejects_a_non_e164_number_before_any_call(self):
        rc, _, err = self._run("buy", "4155550101")
        self.assertEqual(rc, 1)
        self.assertIn("E.164", err)
        self.assertEqual(FakeTwilio.calls, [])

    def test_twilio_error_is_surfaced_verbatim(self):
        FakeTwilio.fail_buy = {"message": "Trial accounts cannot purchase numbers", "code": 21404,
                               "more_info": "https://www.twilio.com/docs/errors/21404"}
        rc, _, err = self._run("buy", "+14155550101", "--base", "https://t.example")
        self.assertEqual(rc, 1)
        self.assertIn("Trial accounts cannot purchase numbers", err)
        self.assertIn("21404", err)
        self.assertIn("more: https://www.twilio.com/docs/errors/21404", err)
        self.assertNotIn("TWILIO_PHONE_NUMBER=+14155550101", self.env_file.read_text())

    def test_a_non_json_error_body_and_a_dead_host_are_surfaced_too(self):
        FakeTwilio.raw_error = True
        rc, _, err = self._run("status")
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 500: <html>gateway timeout</html>", err)
        with mock.patch.dict(os.environ, {"TWILIO_API_BASE": "http://127.0.0.1:1"}):
            self.mod = load_module()
            rc, _, err = self._run("status")
        self.assertEqual(rc, 1)
        self.assertIn("no response: network error", err)

    def test_set_webhook_points_the_configured_number_here(self):
        self.mod.set_env_var(self.env_file, "TWILIO_PHONE_NUMBER", "+14155550100")
        rc, out, err = self._run("set-webhook", "https://new.ngrok-free.app")
        self.assertEqual(rc, 0, err)
        post = [c for c in FakeTwilio.calls if c[0] == "POST"][0]
        self.assertIn("/IncomingPhoneNumbers/PN1.json", post[1])
        self.assertEqual(post[2]["VoiceUrl"], "https://new.ngrok-free.app/twilio/connect")
        self.assertEqual(self.env().get("TWILIO_WEBHOOK_PUSHED"), "https://new.ngrok-free.app")
        self.assertNotIn("TWILIO_WEBHOOK_URL", self.env())

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

    def test_set_webhook_picks_the_only_owned_number_and_records_it(self):
        rc, _, err = self._run("set-webhook", "https://n.example")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.env().get("TWILIO_PHONE_NUMBER"), "+14155550100")

    def test_set_webhook_asks_which_number_when_several_are_owned(self):
        FakeTwilio.owned = [OWNED, OWNED2]
        rc, _, err = self._run("set-webhook", "https://n.example")
        self.assertEqual(rc, 1)
        self.assertIn("which number? owned: +14155550100, +14155550199", err)
        rc, _, err = self._run("set-webhook", "https://n.example", "--number", "+14155550199")
        self.assertEqual(rc, 0, err)
        self.assertIn("/IncomingPhoneNumbers/PN9.json", [c for c in FakeTwilio.calls if c[0] == "POST"][0][1])

    def test_set_webhook_with_no_owned_numbers_is_an_error(self):
        FakeTwilio.owned = []
        rc, _, err = self._run("set-webhook", "https://n.example")
        self.assertEqual(rc, 1)
        self.assertIn("owns no numbers", err)

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
        rc, out, _ = self._run("status")
        self.assertIn("trial account", out)
        self.assertIn("differs from this machine", out)

    def test_status_with_no_numbers_points_at_numbers_then_buy(self):
        FakeTwilio.owned = []
        rc, out, _ = self._run("status")
        self.assertEqual(rc, 0)
        self.assertIn("Numbers: none owned yet", out)

    # ---------- the restart round trip ----------

    def test_a_moved_tunnel_is_drift_and_is_re_pushed_from_the_live_server(self):
        # Boot 1: the server bound tunnel A; buy pushes A and records it as PUSHED.
        FakeTwilio.health_url = "https://a.ngrok-free.app"
        rc, _, err = self._run("buy", "+14155550100")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.env().get("TWILIO_WEBHOOK_PUSHED"), "https://a.ngrok-free.app")
        self.assertNotIn("TWILIO_WEBHOOK_URL", self.env(), "a moving tunnel is never recorded as the fixed URL")
        # Restart: unreserved ngrok, the server bound tunnel B. Twilio still has A.
        FakeTwilio.health_url = "https://b.ngrok-free.app"
        FakeTwilio.owned = [{**OWNED, "voice_url": "https://a.ngrok-free.app/twilio/connect"}]
        rc, out, _ = self._run("--json", "status")
        j = json.loads(out)
        self.assertEqual(j["webhook_base"], "https://b.ngrok-free.app", "the live tunnel, not the record")
        self.assertTrue(j["server_running"])
        self.assertTrue(j["numbers"][0]["webhook_drift"])
        self.assertEqual((j["last_pushed"], j["pushed_stale"]), ("https://a.ngrok-free.app", True))
        rc, out, _ = self._run("status")
        self.assertIn("last pushed to Twilio: https://a.ngrok-free.app — run set-webhook", out)
        # set-webhook with no base pushes B, the runtime tunnel.
        FakeTwilio.calls = []
        rc, _, err = self._run("set-webhook")
        self.assertEqual(rc, 0, err)
        post = [c for c in FakeTwilio.calls if c[0] == "POST"][0]
        self.assertEqual(post[2]["VoiceUrl"], "https://b.ngrok-free.app/twilio/connect")
        self.assertEqual(self.env().get("TWILIO_WEBHOOK_PUSHED"), "https://b.ngrok-free.app")
        FakeTwilio.owned = [{**OWNED, "voice_url": "https://b.ngrok-free.app/twilio/connect"}]
        rc, out, _ = self._run("--json", "status")
        j = json.loads(out)
        self.assertFalse(j["numbers"][0]["webhook_drift"])
        self.assertFalse(j["pushed_stale"])

    def test_the_health_probe_follows_phone_port(self):
        with mock.patch.dict(os.environ, {"PHONE_PORT": "3199"}):
            os.environ.pop("PHONE_SERVER_HEALTH")
            mod = load_module()
        self.assertEqual(mod.LOCAL_HEALTH, "http://localhost:3199/health")

    # ---------- credentials ----------

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

    def test_a_broken_or_absent_resolver_degrades_to_env_and_file(self):
        with mock.patch.object(self.mod, "_resolve", mock.Mock(side_effect=RuntimeError("vault down"))):
            self.assertEqual(self.mod.credential("TWILIO_ACCOUNT_SID", self.env_file), SID)
        with mock.patch.dict(sys.modules, {"channel_token": None}):
            self.assertIsNone(self.mod._load_resolver())
        self.assertEqual(self.mod.env_file_dict(pathlib.Path(self.tmp, "missing.env")), {})

    # ---------- the .env writer ----------

    def test_set_env_var_replaces_active_line_and_keeps_the_rest(self):
        p = pathlib.Path(self.tmp, "x.env")
        p.write_text("A=1\n# B=tpl\nA=2\n")
        self.mod.set_env_var(p, "A", "9")
        self.assertEqual(p.read_text(), "A=9\n# B=tpl\nA=2\n")
        self.mod.set_env_var(p, "B", "live")
        self.assertEqual(p.read_text(), "A=9\n# B=tpl\nB=live\nA=2\n")
        self.mod.set_env_var(p, "C", "new")
        self.assertEqual(p.read_text(), "A=9\n# B=tpl\nB=live\nA=2\nC=new\n")
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".tmp")], [])

    def test_set_env_var_keeps_a_0600_secrets_file_private(self):
        p = pathlib.Path(self.tmp, "s.env")
        p.write_text("TWILIO_AUTH_TOKEN=secret\n")
        os.chmod(p, 0o600)
        self.mod.set_env_var(p, "TWILIO_PHONE_NUMBER", "+1")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(p.read_text(), "TWILIO_AUTH_TOKEN=secret\nTWILIO_PHONE_NUMBER=+1\n")
        # A file the owner made group-readable stays that way: the mode is copied, not chosen.
        os.chmod(p, 0o640)
        self.mod.set_env_var(p, "TWILIO_PHONE_NUMBER", "+2")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o640)
        # A file created from nothing is owner-only: it holds secrets.
        n = pathlib.Path(self.tmp, "new.env")
        self.mod.set_env_var(n, "K", "v")
        self.assertEqual((n.read_text(), stat.S_IMODE(n.stat().st_mode)), ("K=v\n", 0o600))

    def test_set_env_var_keeps_crlf_line_endings_byte_for_byte(self):
        p = pathlib.Path(self.tmp, "w.env")
        p.write_bytes(b"A=1\r\n# B=tpl\r\nOTHER=x\r\n")
        self.mod.set_env_var(p, "A", "9")
        self.assertEqual(p.read_bytes(), b"A=9\r\n# B=tpl\r\nOTHER=x\r\n")
        self.mod.set_env_var(p, "B", "live")
        self.assertEqual(p.read_bytes(), b"A=9\r\n# B=tpl\r\nB=live\r\nOTHER=x\r\n")
        self.mod.set_env_var(p, "C", "new")
        self.assertEqual(p.read_bytes(), b"A=9\r\n# B=tpl\r\nB=live\r\nOTHER=x\r\nC=new\r\n")

    def test_set_env_var_keeps_a_missing_final_newline_and_non_utf8_bytes(self):
        p = pathlib.Path(self.tmp, "n.env")
        p.write_bytes(b"NAME=caf\xe9\nLAST=1")
        self.mod.set_env_var(p, "C", "new")
        self.assertEqual(p.read_bytes(), b"NAME=caf\xe9\nLAST=1\nC=new\n")
        p.write_bytes(b"# K=tpl")
        self.mod.set_env_var(p, "K", "v")
        self.assertEqual(p.read_bytes(), b"# K=tpl\nK=v\n")
        p.write_bytes(b"K=old")
        self.mod.set_env_var(p, "K", "v")
        self.assertEqual(p.read_bytes(), b"K=v\n")

    def test_set_env_var_writes_through_a_symlink_and_keeps_the_link(self):
        target = pathlib.Path(self.tmp, "real.env")
        target.write_text("A=1\n")
        os.chmod(target, 0o600)
        link = pathlib.Path(self.tmp, "link.env")
        link.symlink_to(target)
        self.mod.set_env_var(link, "A", "2")
        self.assertTrue(link.is_symlink(), "the link is not replaced by a regular file")
        self.assertEqual(os.path.realpath(link), os.path.realpath(target))
        self.assertEqual(target.read_text(), "A=2\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_set_env_var_rejects_line_breaks_and_nul_so_nothing_injects_a_line(self):
        p = pathlib.Path(self.tmp, "i.env")
        p.write_text("A=1\n")
        for bad in ("x\nEVIL=1", "x\rEVIL=1", "x\r\nEVIL=1", "x\0"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.mod.set_env_var(p, "A", bad)
        for bad_key in ("A\n", "A\rB", "A B", "1A", "", "A=B", "A\0"):
            with self.assertRaises(ValueError, msg=repr(bad_key)):
                self.mod.set_env_var(p, bad_key, "v")
        self.assertEqual(p.read_text(), "A=1\n")
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".tmp")], [])

    def test_set_env_var_creates_the_temp_file_at_its_final_mode_from_the_first_byte(self):
        p = pathlib.Path(self.tmp, "m.env")
        p.write_text("TWILIO_AUTH_TOKEN=secret\n")
        os.chmod(p, 0o600)
        seen = []
        real_open = os.open

        def spy(path, flags, mode=0o777, *a, **kw):
            fd = real_open(path, flags, mode, *a, **kw)
            if str(path).endswith(".tmp"):
                seen.append((flags, mode, stat.S_IMODE(os.fstat(fd).st_mode)))
            return fd

        old_umask = os.umask(0o000)   # the widest umask: only the creation mode keeps others out
        try:
            with mock.patch("os.open", spy):
                self.mod.set_env_var(p, "TWILIO_PHONE_NUMBER", "+1")
        finally:
            os.umask(old_umask)
        flags, mode, at_creation = seen[0]
        self.assertEqual(flags & (os.O_CREAT | os.O_EXCL), os.O_CREAT | os.O_EXCL)
        self.assertEqual((mode, at_creation), (0o600, 0o600), "the token was readable before the chmod")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        # A restrictive umask narrows the creation mode; the file's own mode is restored.
        os.chmod(p, 0o640)
        old_umask = os.umask(0o077)
        try:
            self.mod.set_env_var(p, "TWILIO_PHONE_NUMBER", "+2")
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o640)
        self.assertEqual(p.read_text(), "TWILIO_AUTH_TOKEN=secret\nTWILIO_PHONE_NUMBER=+2\n")

    def test_set_env_var_replaces_a_leftover_temp_file_instead_of_failing(self):
        p = pathlib.Path(self.tmp, "l.env")
        p.write_text("A=1\n")
        stale = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        stale.write_text("junk from an interrupted run")
        self.mod.set_env_var(p, "A", "2")
        self.assertEqual(p.read_text(), "A=2\n")
        self.assertFalse(stale.exists())

    # ---------- the phone server reads the same vault ----------

    def test_the_phone_server_resolves_credentials_the_way_the_script_does(self):
        repo = _SCRIPT.parents[3]
        server = (repo / "skills" / "phone-conversation" / "scripts" / "conversation-server.ts").read_text()
        self.assertIn("envOrVault('TWILIO_ACCOUNT_SID')", server)
        self.assertIn("envOrVault('TWILIO_AUTH_TOKEN')", server)
        self.assertNotIn("process.env.TWILIO_ACCOUNT_SID", server, "a vault-only setup would start the script but not the server")
        self.assertNotIn("process.env.TWILIO_AUTH_TOKEN", server)
        ts_account = re.search(r"VAULT_KEYCHAIN_ACCOUNT = '([^']+)'", (repo / "src" / "vault-secret.ts").read_text()).group(1)
        py_account = re.search(r'^_ACCOUNT = "([^"]+)"', (repo / "src" / "vault_intercept.py").read_text(), re.M).group(1)
        self.assertEqual(ts_account, py_account, "the server must read the Keychain item `vault set` writes")


if __name__ == "__main__":
    unittest.main(verbosity=1)
