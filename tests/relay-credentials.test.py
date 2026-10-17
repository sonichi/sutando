#!/usr/bin/env python3
"""The relay credential resolves without the process environment.

The core session no longer inherits AG2_REMOTE_* / REMOTE_TASK_*, so each
in-session consumer must find the token in the channel env or the repo `.env`
on its own. Three layers:

  * the resolver's tier order (src/relay_credentials.py);
  * each consumer, with an EMPTY relay environment, resolving a token that lives
    only in (a) the channel env, (b) the repo `.env`;
  * delegation: each consumer gets its answer from the shared resolver.

Hermetic: CLAUDE_CONFIG_DIR is a temp dir, the vault is a stub module, the repo
`.env` is a temp file patched in, and every value is fake.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]

_VAULT: dict = {}


def _fake_get_vault_key(var):
    if var in _VAULT:
        return _VAULT[var]
    raise KeyError(var)


_FAKE_VI = types.ModuleType("vault_intercept")
_FAKE_VI.get_vault_key = _fake_get_vault_key
sys.modules["vault_intercept"] = _FAKE_VI

sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))
sys.path.insert(0, str(REPO / "skills" / "agent-room-ops"))

import relay_credentials as rc  # noqa: E402

_REAL_REPO_ENV_FILE = rc.repo_env_file

RELAY_KEYS = ("GATEWAY_TOKEN", "RELAY_TOKEN", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN",
              "GATEWAY_URL", "RELAY_URL", "REMOTE_TASK_URL", "AG2_REMOTE_URL",
              "REMOTE_TASK_CHANNEL_DIR", "AG2_DEVICE_ENV", "SUTANDO_APP_SUPPORT")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Stub(BaseHTTPRequestHandler):
    auth: list = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        _Stub.auth.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"event_id": "$fake-event"}')

    def log_message(self, *a):
        pass


class _Hermetic(unittest.TestCase):
    """Empty relay env, temp CLAUDE_CONFIG_DIR, stub vault, no repo `.env`."""

    URL = "https://gw.example.invalid"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="relay-creds-"))
        env = {k: v for k, v in os.environ.items() if k not in RELAY_KEYS}
        env["CLAUDE_CONFIG_DIR"] = str(self.tmp / "claude")
        env.pop("CLAUDE_HOME", None)
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.repo_env = None
        p = mock.patch.object(rc, "repo_env_file", lambda: self.repo_env)
        p.start()
        self.addCleanup(p.stop)
        _VAULT.clear()

    def channel_env(self, body, source="ag2space"):
        d = self.tmp / "claude" / "channels" / source
        d.mkdir(parents=True, exist_ok=True)
        (d / ".env").write_text(body)
        return d / ".env"

    def write_repo_env(self, body):
        self.repo_env = self.tmp / "repo.env"
        self.repo_env.write_text(body)
        return self.repo_env

    def only_in_channel(self, url=None):
        self.channel_env(f"REMOTE_TASK_URL={url or self.URL}\nREMOTE_TASK_TOKEN=fake-channel-secret\n")
        return "fake-channel-secret"

    def only_in_repo(self, url=None):
        self.write_repo_env(f"OTHER=1\nAG2_REMOTE_TOKEN={url or self.URL}|fake-repo-secret\n")
        return "fake-repo-secret"


class ResolverOrder(_Hermetic):
    def test_channel_env_alone(self):
        tok = self.only_in_channel()
        c = rc.resolve_relay_credentials()
        self.assertEqual((c.base_url, c.token, c.origin), (self.URL, tok, "channel-env"))

    def test_repo_env_alone(self):
        tok = self.only_in_repo()
        c = rc.resolve_relay_credentials()
        self.assertEqual((c.base_url, c.token, c.origin), (self.URL, tok, "repo-env"))
        self.assertEqual(c.raw, f"{self.URL}|{tok}")

    def test_nothing_resolves(self):
        c = rc.resolve_relay_credentials()
        self.assertEqual((c.base_url, c.token, c.origin), ("", "", "none"))

    def test_order_env_channel_vault_repo(self):
        self.write_repo_env(f"REMOTE_TASK_TOKEN={self.URL}|repo\n")
        self.assertEqual(rc.resolve_relay_credentials().token, "repo")
        _VAULT["REMOTE_TASK_TOKEN"] = f"{self.URL}|vault"
        self.assertEqual(rc.resolve_relay_credentials().token, "vault")
        self.channel_env(f"REMOTE_TASK_TOKEN={self.URL}|channel\n")
        self.assertEqual(rc.resolve_relay_credentials().token, "channel")
        os.environ["REMOTE_TASK_TOKEN"] = f"{self.URL}|env"
        self.assertEqual(rc.resolve_relay_credentials().token, "env")

    def test_lane_never_borrows_the_primary_vault_or_repo_env(self):
        self.only_in_repo()
        _VAULT["REMOTE_TASK_TOKEN"] = f"{self.URL}|vault"
        self.assertEqual(rc.resolve_relay_credentials("dev-ag2space").token, "")

    def test_repo_url_does_not_repoint_another_tier(self):
        self.write_repo_env("AG2_REMOTE_URL=https://repo.example.invalid\n")
        self.channel_env("REMOTE_TASK_TOKEN=https://chan.example.invalid|s\n")
        self.assertEqual(rc.resolve_relay_credentials().base_url, "https://chan.example.invalid")

    def test_first_non_empty_alias_wins_in_a_file(self):
        self.channel_env(f"RELAY_TOKEN=\nREMOTE_TASK_URL={self.URL}\nREMOTE_TASK_TOKEN=second\n")
        self.assertEqual(rc.resolve_relay_credentials().token, "second")

    def test_undecodable_file_is_absence(self):
        d = self.tmp / "claude" / "channels" / "ag2space"
        d.mkdir(parents=True)
        (d / ".env").write_bytes(b"REMOTE_TASK_TOKEN=\xff\xfe\x00\n")
        self.assertEqual(rc._first(d / ".env", ("REMOTE_TASK_TOKEN",)), "")

    def test_sibling_env_file_is_selected_like_channel_env_sh(self):
        d = self.tmp / "claude" / "channels" / "ag2space"
        d.mkdir(parents=True)
        (d / ".env").write_text("MATRIX_USER=x\n")
        (d / "relay-client.env").write_text(f"REMOTE_TASK_TOKEN={self.URL}|sib\n")
        self.assertEqual(rc.resolve_relay_credentials().token, "sib")

    def test_environment_is_never_written(self):
        self.only_in_repo()
        before = dict(os.environ)
        rc.resolve_relay_credentials()
        self.assertEqual(dict(os.environ), before)

    def test_repr_hides_the_secret(self):
        tok = self.only_in_channel()
        self.assertNotIn(tok, repr(rc.resolve_relay_credentials()))


class Consumers(_Hermetic):
    """Each consumer, empty relay env, token in (a) the channel env, (b) the repo `.env`."""

    @classmethod
    def setUpClass(cls):
        cls.bee = _load("bee_actions_relay_test",
                        REPO / "skills" / "bee-actions" / "scripts" / "bee_actions.py")
        cls.notify = _load("notify_relay_test",
                           REPO / "skills" / "task-progress" / "scripts" / "notify.py")
        cls.hc = _load("hc_relay_test", REPO / "src" / "health-check.py")
        cls.cron = _load("cron_notify_relay_test",
                         REPO / "skills" / "schedule-crons" / "cron-notify.py")
        import dispatcher
        import _gateway
        cls.dispatcher, cls.gateway = dispatcher, _gateway
        cls.httpd = HTTPServer(("127.0.0.1", 0), _Stub)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.stub = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _each_tier(self, check):
        for name, put in (("channel", self.only_in_channel), ("repo", self.only_in_repo)):
            with self.subTest(tier=name):
                self.setUp()
                tok = put()
                check(tok)

    def test_bee_actions(self):
        def check(tok):
            base, headers = self.bee._gateway_creds()
            self.assertEqual((base, headers["Authorization"]), (self.URL, f"Bearer {tok}"))
        self._each_tier(check)

    def test_room_ops(self):
        def check(tok):
            base, headers = self.gateway.gateway()
            self.assertEqual((base, headers.get("Authorization")), (self.URL, f"Bearer {tok}"))
        self._each_tier(check)

    def test_task_progress_notify(self):
        def check(tok):
            self.assertEqual(self.notify._gateway_config("ag2space"), (self.URL, tok))
        self._each_tier(check)

    def test_health_check_sender_creds(self):
        def check(tok):
            self.assertEqual(self.hc._gateway_creds(), (self.URL, tok))
        self._each_tier(check)

    def test_health_check_gateway_relaunch(self):
        def check(tok):
            with mock.patch.object(self.hc, "_bridge_interpreter", return_value="python3"):
                plan = self.hc._bridge_launch_plan("gateway-bridge")
            self.assertIsNotNone(plan)
            self.assertTrue(plan[1]["REMOTE_TASK_TOKEN"].endswith(tok))
        self._each_tier(check)

    def test_cron_notify(self):
        def check(tok):
            self.assertEqual(self.cron._load_gateway(), (self.URL, tok))
        self._each_tier(check)

    def test_ensure_cron_room_explicit_repo(self):
        self.only_in_channel()
        repo = self.tmp / "repo"
        repo.mkdir()
        resolve = self.cron._canonical_resolver()
        self.assertEqual(resolve(str(repo)), (self.URL, "fake-channel-secret"))
        (repo / ".env").write_text(f"AG2_REMOTE_TOKEN={self.URL}|fake-repo-secret\n")
        (self.tmp / "claude" / "channels" / "ag2space" / ".env").unlink()
        self.assertEqual(resolve(str(repo)), (self.URL, "fake-repo-secret"))

    def test_runtime_api_message_send(self):
        def check(tok):
            _Stub.auth.clear()
            out = self.dispatcher._exec_message_send(
                {"resource": {"roomId": "!fake:room"}, "input": {"body": "hi"}})
            self.assertEqual(out["eventId"], "$fake-event")
            self.assertEqual(_Stub.auth, [f"Bearer {tok}"])
        for name, put in (("channel", self.only_in_channel), ("repo", self.only_in_repo)):
            with self.subTest(tier=name):
                self.setUp()
                check(put(url=self.stub))


class Delegation(_Hermetic):
    """Each consumer's answer comes from the shared resolver, not a private chain."""

    SENTINEL = rc.RelayCredentials("https://sentinel.invalid", "sentinel-token",
                                   "https://sentinel.invalid|sentinel-token", "env:X")

    @classmethod
    def setUpClass(cls):
        Consumers.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _spy(self):
        return mock.patch.object(rc, "resolve_relay_credentials",
                                 mock.Mock(return_value=self.SENTINEL))

    def test_bee_actions(self):
        with self._spy() as m:
            base, headers = self.bee._gateway_creds()
        m.assert_called_once()
        self.assertEqual(headers["Authorization"], "Bearer sentinel-token")

    def test_room_ops(self):
        with self._spy() as m:
            base, headers = self.gateway.gateway()
        m.assert_called_once()
        self.assertEqual(base, "https://sentinel.invalid")

    def test_task_progress_notify(self):
        self.assertEqual(self.notify._resolve_relay.__module__, rc.__name__)
        with mock.patch.object(self.notify, "_resolve_relay",
                               mock.Mock(return_value=self.SENTINEL)) as m:
            got = self.notify._gateway_config("ag2space")
        m.assert_called_once()
        self.assertEqual(m.call_args.args, ("ag2space",))
        self.assertEqual(got, ("https://sentinel.invalid", "sentinel-token"))

    def test_health_check(self):
        self.assertIs(self.hc.relay_credentials, rc)
        with self._spy() as m:
            self.assertEqual(self.hc._gateway_creds()[1], "sentinel-token")
            with mock.patch.object(self.hc, "_bridge_interpreter", return_value="python3"):
                plan = self.hc._bridge_launch_plan("gateway-bridge")
        self.assertEqual(m.call_count, 2)
        self.assertEqual(plan[1]["REMOTE_TASK_TOKEN"], self.SENTINEL.raw)

    def test_cron_notify(self):
        with self._spy() as m:
            got = self.cron._load_gateway()
        m.assert_called_once()
        self.assertEqual(got, ("https://sentinel.invalid", "sentinel-token"))

    def test_runtime_api_dispatcher(self):
        _Stub.auth.clear()
        creds = rc.RelayCredentials(self.stub, "sentinel-token", "", "env:X")
        with mock.patch.object(rc, "resolve_relay_credentials",
                               mock.Mock(return_value=creds)) as m:
            self.dispatcher._exec_message_send(
                {"resource": {"roomId": "!fake:room"}, "input": {"body": "hi"}})
        m.assert_called_once()
        self.assertEqual(_Stub.auth, ["Bearer sentinel-token"])


class Degrades(_Hermetic):
    """Paths taken when the resolver or its inputs are unavailable."""

    @classmethod
    def setUpClass(cls):
        Consumers.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_room_ops_without_core_src_reads_env_only(self):
        self.only_in_channel()
        with mock.patch.object(self.gateway, "_core_src_on_path", lambda: False):
            self.assertEqual(self.gateway.gateway()[0], "")
            os.environ["REMOTE_TASK_TOKEN"] = f"{self.URL}|fake-env"
            base, headers = self.gateway.gateway()
        self.assertEqual((base, headers["Authorization"]), (self.URL, "Bearer fake-env"))

    def test_notify_without_resolver_sends_nothing(self):
        self.only_in_channel()
        with mock.patch.object(self.notify, "_resolve_relay", None):
            self.assertIsNone(self.notify._gateway_config("ag2space"))

    def test_notify_refuses_an_escaping_channel_env(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / ".env").write_text(f"REMOTE_TASK_TOKEN={self.URL}|fake\n")
        d = self.tmp / "claude" / "channels"
        d.mkdir(parents=True)
        (d / "ag2space").symlink_to(outside)
        self.assertIsNone(self.notify._gateway_config("ag2space"))

    def test_notify_reports_absence(self):
        self.assertIsNone(self.notify._gateway_config("ag2space"))

    def test_vault_lookup_failure_is_absence(self):
        def boom(_var):
            raise RuntimeError("keychain locked")
        self.assertEqual(rc.vault_token(boom), "")

    def test_locators_degrade_to_none(self):
        with mock.patch.dict(sys.modules, {"channel_env_resolve": None}):
            self.assertIsNone(rc.channel_env_file())
        with mock.patch.dict(sys.modules, {"sutando_config": None}):
            self.assertIsNone(_REAL_REPO_ENV_FILE())


if __name__ == "__main__":
    unittest.main(verbosity=2)
