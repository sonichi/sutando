#!/usr/bin/env python3
"""Antigravity on the Gemini API key saved in the AG2 Space app (adapters/antigravity/gemini_key.py).

agy ignores GEMINI_API_KEY while a Google sign-in exists; ACP `authenticate` gemini-api-key with the key in
`_meta.apiKey` after `initialize` is what switches that one process to the key. These tests drive the
connector's owner and guest turns, and the setup probe, with a fake ACP client and a fake Keychain
(`security` stand-in), with dummy keys only.

Run:
    python3 -m pytest -q packages/ag2-sparrow/tests/test_antigravity_gemini_key.py
"""
import base64
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ, isolated_child_env  # noqa: E402

isolate_os_environ()

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import acp_host  # noqa: E402
from ag2_sparrow.adapters.antigravity import antigravity_connector as gc, cli, gemini_key as gk  # noqa: E402

DUMMY_KEY = "AIzaDUMMY-test-key-0123456789abcdef"
AGENT = "@agy-bot:s.space"
METHODS = [{"id": "oauth-personal", "name": "Log in with Google"},
           {"id": "gemini-api-key", "name": "Use Gemini API key"}]
MODES = {"currentModeId": "default", "availableModes": [{"id": "default"}, {"id": "plan"}]}


def wrapped(key, tag="t1"):
    """The app's Keychain value: base64 JSON {"tag", "value"}."""
    return base64.b64encode(json.dumps({"tag": tag, "value": key}).encode()).decode()


class FakeKeychain:
    """`/usr/bin/security find-generic-password -a ACCOUNT -s SERVICE -w` over a dict {service: stored}."""

    def __init__(self, items=None):
        self.items = dict(items or {})
        self.asked = []

    def __call__(self, argv, **kw):
        service = argv[argv.index("-s") + 1]
        account = argv[argv.index("-a") + 1]
        self.asked.append((argv[0], service, account))
        stored = self.items.get(service) if account == gk.ACCOUNT else None
        if stored is None:
            return subprocess.CompletedProcess(argv, 44, "", "security: SecKeychainSearchCopyNext: not found\n")
        return subprocess.CompletedProcess(argv, 0, stored + "\n", "")


class FakeAgy:
    """An ACP client stand-in recording every request; one per spawned agy process."""
    spawned = []

    def __init__(self, argv, cwd=None, env=None, stderr_path=None, on_permission=None, on_update=None,
                 methods=METHODS, auth_error=None):
        self.argv, self.env, self.calls = list(argv), dict(env or {}), []
        self.methods, self.auth_error = methods, auth_error
        FakeAgy.spawned.append(self)

    def initialize(self, timeout):
        self.calls.append(("initialize", {}))
        return {"protocolVersion": 1, "authMethods": self.methods,
                "agentCapabilities": {"loadSession": True}, "agentInfo": {"name": "Antigravity", "version": "1.5.13"}}

    def call(self, method, params, timeout):
        self.calls.append((method, params))
        if method == "authenticate":
            if self.auth_error:
                raise self.auth_error
            return {}
        if method == "session/new":
            return {"sessionId": "s-%d" % len(FakeAgy.spawned), "modes": MODES}
        if method == "session/prompt":
            return {"stopReason": "end_turn"}
        return {}

    def notify(self, method, params):
        self.calls.append((method, params))

    def close(self, grace=5.0):
        pass

    def methods_called(self):
        return [m for m, _ in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        FakeAgy.spawned = []
        self.tmp = Path(tempfile.mkdtemp(prefix="agy-gemini-key-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "cwd").mkdir()
        self.keychain = FakeKeychain()
        # The connector reads the Keychain through gemini_api_key_from_app; here on "darwin", with the fake.
        real = gk.gemini_api_key_from_app
        patcher = mock.patch.object(gk, "gemini_api_key_from_app",
                                    lambda agent_id="": real(agent_id, run=self.keychain, platform="darwin",
                                                             environ={}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self, *extra):
        a = gc.build_parser().parse_args(["--work", str(self.tmp / "work"), "--cwd", str(self.tmp / "cwd"),
                                          "--acp-command", "/opt/agy/bin/agy acp", "--agent-id", AGENT, *extra])
        gc._prepare(a)
        gc._paths(a, str(self.tmp / "work"))
        a.child_env = isolated_child_env(HOME=str(self.tmp))
        return a

    def owner_turn(self, conn, task="task-1", factory=FakeAgy):
        conn.client_factory = factory
        headers = {"channel_id": "!r:s.space", "access_tier": "owner", "user_id": "@susan:s.space"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, _, detail, _ = conn.run_turn(task, headers, "hello", True)
        return status, detail, err.getvalue()

    def guest_turn(self, conn):
        conn.client_factory = FakeAgy
        headers = {"channel_id": "!r:s.space", "access_tier": "public", "user_id": "@guest:s.space"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, _, detail, _ = conn.run_turn("task-g", headers, "hello", False)
        return status, detail, err.getvalue()

    @staticmethod
    def authenticates(agy):
        return [p for m, p in agy.calls if m == "authenticate"]


class OwnerTurns(Base):
    def test_owner_turn_authenticates_with_the_agents_own_key_before_any_session(self):
        self.keychain.items[gk.agent_service(AGENT)] = wrapped(DUMMY_KEY)
        self.keychain.items["space.ag2.app.voice-byok"] = wrapped("AIzaVOICE-other")
        status, _, logs = self.owner_turn(gc.Connector(self.args()))
        self.assertEqual(status, "completed")
        (agy,) = FakeAgy.spawned
        self.assertEqual(agy.methods_called()[:3], ["initialize", "authenticate", "session/new"])
        self.assertEqual(self.authenticates(agy), [{"methodId": "gemini-api-key", "_meta": {"apiKey": DUMMY_KEY}}])
        self.assertEqual(self.keychain.asked[0], ("/usr/bin/security", "space.ag2.app.agy-gemini-key." + AGENT,
                                                  "GEMINI_API_KEY"))
        self.assertEqual(len(self.keychain.asked), 1)   # only this agent's item; never the voice key
        self.assertIn("using the Gemini API key saved in the app for this agent", logs)
        # Never in agy's argv or environment, never in the log.
        self.assertNotIn(DUMMY_KEY, " ".join(agy.argv) + json.dumps(agy.env))
        self.assertNotIn(DUMMY_KEY, logs)

    def test_the_apps_voice_key_is_never_used(self):
        self.keychain.items["space.ag2.app.voice-byok"] = wrapped(DUMMY_KEY)
        self.owner_turn(gc.Connector(self.args()))
        (agy,) = FakeAgy.spawned
        self.assertNotIn("authenticate", agy.methods_called())
        self.assertNotIn("space.ag2.app.voice-byok", [s for _, s, _ in self.keychain.asked])

    def test_no_item_no_authenticate(self):
        status, _, _ = self.owner_turn(gc.Connector(self.args()))
        self.assertEqual(status, "completed")
        (agy,) = FakeAgy.spawned
        self.assertNotIn("authenticate", agy.methods_called())

    def test_method_not_advertised_no_authenticate_and_no_keychain_read(self):
        self.keychain.items[gk.agent_service(AGENT)] = wrapped(DUMMY_KEY)
        self.owner_turn(gc.Connector(self.args()),
                        factory=lambda *a, **k: FakeAgy(*a, methods=METHODS[:1], **k))
        (agy,) = FakeAgy.spawned
        self.assertNotIn("authenticate", agy.methods_called())
        self.assertEqual(self.keychain.asked, [])

    def test_a_refused_authenticate_keeps_the_turn_on_the_sign_in_and_logs_no_key(self):
        self.keychain.items[gk.agent_service(AGENT)] = wrapped(DUMMY_KEY)
        boom = gc.acp_host.AcpError("authenticate: bad key %s" % DUMMY_KEY, -32602, None)
        status, _, logs = self.owner_turn(gc.Connector(self.args()),
                                          factory=lambda *a, **k: FakeAgy(*a, auth_error=boom, **k))
        self.assertEqual(status, "completed")
        self.assertIn("not accepted (AcpError); staying on the Google sign-in", logs)
        self.assertNotIn(DUMMY_KEY, logs)


class SessionsKeepTheirAuth(Base):
    """A Session opened on the Google sign-in still bills that account's quota when resumed (session/load)
    under the key ("Individual quota reached"): the auth mode is saved with the Session and a mismatch opens a
    new one."""

    def test_a_sign_in_session_is_not_resumed_on_the_key_and_a_key_session_is(self):
        conn = gc.Connector(self.args())
        self.owner_turn(conn, "task-1")                       # no key yet: on the sign-in
        rec = conn.store.get(("!r:s.space", "owner"))
        self.assertEqual(rec["auth"], acp_host.SIGNED_IN_AUTH)
        first = rec["session_id"]

        self.keychain.items[gk.agent_service(AGENT)] = wrapped(DUMMY_KEY)
        _, _, logs = self.owner_turn(conn, "task-2")          # key saved: a new Session, not session/load
        agy = FakeAgy.spawned[-1]
        self.assertNotIn("session/load", agy.methods_called())
        self.assertIn("session/new", agy.methods_called())
        self.assertIn("not resuming session %s" % first, logs)
        rec = conn.store.get(("!r:s.space", "owner"))
        self.assertEqual(rec["auth"], gk.API_KEY_AUTH)
        self.assertNotEqual(rec["session_id"], first)

        self.owner_turn(conn, "task-3")                       # still on the key: resumed
        agy = FakeAgy.spawned[-1]
        self.assertEqual([p["sessionId"] for m, p in agy.calls if m == "session/load"], [rec["session_id"]])
        self.assertNotIn("session/new", agy.methods_called())

    def test_a_session_saved_before_this_change_counts_as_sign_in(self):
        conn = gc.Connector(self.args())
        conn.store.remember(("!r:s.space", "owner"), "old-1", str(self.tmp / "cwd"), 3)   # no auth field
        self.owner_turn(conn)                                   # no key: resumed as before
        self.assertIn("session/load", FakeAgy.spawned[-1].methods_called())
        conn.store.remember(("!r:s.space", "owner"), "old-2", str(self.tmp / "cwd"), 3)
        self.keychain.items[gk.agent_service(AGENT)] = wrapped(DUMMY_KEY)
        self.owner_turn(conn, "task-2")                         # key: not resumed
        self.assertNotIn("session/load", FakeAgy.spawned[-1].methods_called())


class RestrictedTurns(Base):
    def test_a_guest_turn_never_gets_the_owners_key(self):
        self.keychain.items[gk.agent_service(AGENT)] = wrapped(DUMMY_KEY)
        self.keychain.items[gk.agent_service(AGENT)] = wrapped(DUMMY_KEY)
        status, detail, logs = self.guest_turn(gc.Connector(self.args()))
        self.assertEqual(status, "completed", detail)
        (agy,) = FakeAgy.spawned
        self.assertIn("--gemini-dir", agy.argv)
        self.assertNotIn("authenticate", agy.methods_called())
        self.assertEqual(self.keychain.asked, [])
        self.assertNotIn(DUMMY_KEY, logs)

    def test_the_helper_itself_refuses_a_gemini_dir_command_line(self):
        agy = FakeAgy(["agy", "acp", "--gemini-dir", "/tmp/x"])
        sent = gk.authenticate_with_app_key(agy, agy.initialize(1), 1, agy.argv,
                                            key_source=lambda _a: DUMMY_KEY)
        self.assertFalse(sent)
        self.assertNotIn("authenticate", agy.methods_called())


class Probe(Base):
    def test_setup_check_counts_a_saved_key_as_signed_in(self):
        self.keychain.items[gk.agent_service(AGENT)] = wrapped(DUMMY_KEY)
        said, why = cli.probe({"ACP_COMMAND": "/opt/agy/bin/agy acp", "AGENT_ID": AGENT}, factory=FakeAgy)
        self.assertEqual(why, "")
        self.assertIn("on the Gemini API key saved in the app", said)
        (agy,) = FakeAgy.spawned
        self.assertEqual(agy.methods_called(), ["initialize", "authenticate", "session/new"])


class KeychainItem(unittest.TestCase):
    def test_unwraps_the_apps_base64_json(self):
        self.assertEqual(gk.unwrap(wrapped(DUMMY_KEY)), DUMMY_KEY)
        self.assertEqual(gk.unwrap(wrapped(DUMMY_KEY).rstrip("=")), DUMMY_KEY)   # padding dropped
        self.assertEqual(gk.unwrap(json.dumps({"tag": "x", "value": DUMMY_KEY})), DUMMY_KEY)
        self.assertEqual(gk.unwrap(DUMMY_KEY + "\n"), DUMMY_KEY)                 # a bare key

    def test_unusable_values_are_empty(self):
        for bad in ("", "  ", "eyJnot-base64!!", wrapped(""), base64.b64encode(b'{"tag":"x"}').decode(),
                    base64.b64encode(b'{"value": 5}').decode(), "two words"):
            self.assertEqual(gk.unwrap(bad), "", bad)

    def test_not_macos_or_opted_out_reads_nothing(self):
        chain = FakeKeychain({gk.agent_service(AGENT): wrapped(DUMMY_KEY)})
        self.assertEqual(gk.gemini_api_key_from_app(AGENT, run=chain, platform="linux", environ={}), "")
        self.assertEqual(gk.gemini_api_key_from_app(AGENT, run=chain, platform="darwin",
                                                    environ={gk.OPT_OUT_ENV: "0"}), "")
        self.assertEqual(gk.gemini_api_key_from_app("", run=chain, platform="darwin", environ={}), "")
        self.assertEqual(chain.asked, [])
        self.assertEqual(gk.gemini_api_key_from_app(AGENT, run=chain, platform="darwin", environ={}), DUMMY_KEY)

    def test_a_hung_or_missing_security_reads_nothing(self):
        def hung(argv, **kw):
            self.assertEqual(kw.get("timeout"), gk.READ_TIMEOUT_S)
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
        self.assertEqual(gk.gemini_api_key_from_app(AGENT, run=hung, platform="darwin", environ={}), "")
        self.assertEqual(gk.gemini_api_key_from_app(
            AGENT, run=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()), platform="darwin",
            environ={}), "")


if __name__ == "__main__":
    unittest.main()
