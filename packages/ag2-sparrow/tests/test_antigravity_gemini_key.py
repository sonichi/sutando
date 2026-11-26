#!/usr/bin/env python3
"""Antigravity on the Gemini API key saved in the AG2 Space app (adapters/antigravity/gemini_key.py).

agy ignores GEMINI_API_KEY while a Google sign-in exists; ACP `authenticate` gemini-api-key with the key in
`_meta.apiKey` after `initialize` is what switches that one process to the key. These tests drive the
connector's owner and guest turns, and the setup probe, with a fake ACP client and a fake Keychain
(`security` stand-in), with dummy keys only.

Run:
    python3 -m pytest -q packages/ag2-sparrow/tests/test_antigravity_gemini_key.py
"""
import argparse
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
from ag2_sparrow import acp_client, acp_host  # noqa: E402
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


    def test_an_unanswered_authenticate_stops_the_turn_and_no_session_is_opened_or_recorded(self):
        self.keychain.items[gk.agent_service(AGENT)] = wrapped(DUMMY_KEY)
        for boom in (acp_client.AcpTimeout("authenticate: no response in 30s"),     # a delayed acknowledgement
                     acp_client.AcpGone("authenticate: the ACP agent exited before answering"),
                     BrokenPipeError("stdin closed")):                               # it could not be sent
            with self.subTest(error=type(boom).__name__):
                FakeAgy.spawned = []
                shutil.rmtree(self.tmp / "work", True)      # each case starts with no saved Session
                conn = gc.Connector(self.args())
                status, detail, logs = self.owner_turn(conn, factory=lambda *a, **k: FakeAgy(*a, auth_error=boom, **k))
                self.assertNotEqual(status, "completed", "not run on a process whose account is unknown")
                (agy,) = FakeAgy.spawned
                self.assertEqual(agy.methods_called(), ["initialize", "authenticate"], "no Session on it")
                self.assertIsNone(conn.store.get(("!r:s.space", "owner")), "and none recorded, under any mode")
                self.assertIn("this agy is not used", logs)
                self.assertNotIn(DUMMY_KEY, logs + str(detail))
                # The next turn is a fresh agy that answers: it runs on the key and records that.
                status, _, _ = self.owner_turn(conn, task="task-2")
                self.assertEqual(status, "completed")
                self.assertEqual(conn.store.get(("!r:s.space", "owner"))["auth"], gk.API_KEY_AUTH)
                self.assertEqual(len(FakeAgy.spawned), 2, "a new process, not the one left in doubt")

    def test_the_setup_check_reports_an_unanswered_authenticate_instead_of_calling_it_signed_in(self):
        self.keychain.items[gk.agent_service(AGENT)] = wrapped(DUMMY_KEY)
        boom = acp_client.AcpTimeout("authenticate: no response in 30s")
        said, why = cli.probe({"ACP_COMMAND": "/opt/agy/bin/agy acp", "AGENT_ID": AGENT},
                              factory=lambda *a, **k: FakeAgy(*a, auth_error=boom, **k))
        self.assertEqual(said, "")
        self.assertIn("did not answer", why)
        self.assertNotIn("session/new", FakeAgy.spawned[0].methods_called())


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


class KeyOnlyAgy(FakeAgy):
    """agy with no Google sign-in: `session/new` is refused until `authenticate` gemini-api-key was accepted."""

    def call(self, method, params, timeout):
        if method == "session/new" and "authenticate" not in self.methods_called():
            self.calls.append((method, params))
            raise acp_client.AcpError("session/new: Authentication required", acp_client.AUTH_REQUIRED)
        return super().call(method, params, timeout)


class InstallChecksTheAgentsOwnKey(Base):
    """The install entry points, not only `probe`: an agent with a saved key and no Google sign-in installs."""

    def setUp(self):
        super().setUp()
        real = cli.probe
        for target, name, value in (
                (cli, "probe", lambda cfg: real(cfg, factory=KeyOnlyAgy)),
                (cli, "acp_command", lambda acp: ("/opt/agy/bin/agy acp", "")),
                (cli.cb, "host_problem", lambda command: ""),
                (cli.cb, "recorded_install_args", lambda args, local, recorded: ""),
                (cli, "resolve_root", lambda args: ({"root": str(self.tmp / "root"), "mode": cli.UNIT,
                                                     "agent_id": AGENT, "token_file": "/seat/.env"}, "")),
                # The install stops right after its checks: what is under test is which of them refuse.
                (cli.cm, "agent_job", lambda local, agent_id: (local, "stopped after the checks"))):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def install_args(self, **over):
        base = dict(work=None, token_env_file="/seat/.env", token_stdin=False, agent=None, key=None, single=False,
                    acp_command=None, cwd=None, session_store=None, allow_non_owner=False, mcp_proxy_command=None,
                    argv=[], parser=None)
        return argparse.Namespace(**dict(base, **over))

    def run_install(self, args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.install(args)
        return rc, err.getvalue()

    def test_install_passes_the_sign_in_check_on_the_key_saved_for_the_agent_it_resolved(self):
        self.keychain.items[gk.agent_service(AGENT)] = wrapped(DUMMY_KEY)
        args = self.install_args()
        rc, err = self.run_install(args)
        self.assertIn("stopped after the checks", err, "it got past the sign-in check")
        self.assertNotIn("not signed in", err)
        self.assertTrue(args.signed_in)
        (agy,) = FakeAgy.spawned
        self.assertEqual(agy.methods_called(), ["initialize", "authenticate", "session/new"])
        self.assertEqual(self.authenticates(agy)[0]["_meta"]["apiKey"], DUMMY_KEY)
        self.assertEqual([service for _, service, _ in self.keychain.asked], [gk.agent_service(AGENT)],
                         "this agent's item and no other")

    def test_install_still_refuses_an_agent_with_no_key_and_no_sign_in(self):
        rc, err = self.run_install(self.install_args())
        self.assertEqual(rc, cli.EXIT_UNUSABLE)
        self.assertIn("not signed in", err)
        self.assertNotIn("stopped after the checks", err)
        # Another agent's key is not this agent's.
        FakeAgy.spawned = []
        self.keychain.items[gk.agent_service("@someone-else:s.space")] = wrapped(DUMMY_KEY)
        rc, err = self.run_install(self.install_args())
        self.assertIn("not signed in", err)
        self.assertEqual(self.authenticates(FakeAgy.spawned[0]), [])

    def test_a_pasted_token_install_checks_the_key_of_the_agent_it_names_before_the_token_is_written(self):
        self.keychain.items[gk.agent_service(AGENT)] = wrapped(DUMMY_KEY)
        seen = {}
        args = self.install_args(token_env_file=None, token_stdin=True, agent=AGENT)
        with mock.patch.object(cli.cb, "install_token_stdin",
                               lambda a, shape, pre, inst, cmd, **k: (seen.update(pre=pre()), 0)[1]):
            self.assertEqual(cli.install(args), 0)
        self.assertEqual(seen["pre"], "", "the preflight counts the key saved for --agent")
        self.assertTrue(args.signed_in, "and the install that follows does not start agy again")
        self.assertEqual(self.authenticates(FakeAgy.spawned[0])[0]["_meta"]["apiKey"], DUMMY_KEY)
        # With no --agent key the preflight refuses before any token is written.
        FakeAgy.spawned = []
        args = self.install_args(token_env_file=None, token_stdin=True, agent="@no-key:s.space")
        with mock.patch.object(cli.cb, "install_token_stdin",
                               lambda a, shape, pre, inst, cmd, **k: (seen.update(pre=pre()), 1)[1]):
            cli.install(args)
        self.assertIn("not signed in", seen["pre"])


# A stand-in agy that leaks: it answers `authenticate` and writes the key it was given to its stderr, the
# way a crashing or verbose CLI might, with an ordinary line before and after.
LEAKY_AGY = r"""
import json, sys
print("starting up", file=sys.stderr, flush=True)
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "initialize":
        result = {"protocolVersion": 1, "authMethods": [{"id": "gemini-api-key"}]}
    elif m.get("method") == "authenticate":
        key = m["params"]["_meta"]["apiKey"]
        print("auth debug: apiKey=%s (len %d)" % (key, len(key)), file=sys.stderr, flush=True)
        sys.stderr.write("partial " + key)          # no newline yet: the rest comes later
        sys.stderr.flush()
        print(" end; again " + key + key, file=sys.stderr, flush=True)
        result = {}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
print("bye", file=sys.stderr, flush=True)
"""
SENTINEL = "AIzaSENTINEL-not-a-real-key-ZZZZ9999"


class KeyedChildStderr(unittest.TestCase):
    """The real client against a real child process: what the child writes to stderr is what gets stored."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agy-stderr-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.script = self.tmp / "leaky_agy.py"
        self.script.write_text(LEAKY_AGY)
        self.log = self.tmp / "agy.stderr.log"

    def run_keyed(self, factory):
        client = factory([sys.executable, str(self.script)], cwd=str(self.tmp), env=isolated_child_env(),
                         stderr_path=str(self.log))
        try:
            init = client.initialize(10)
            keyed = gk.authenticate_with_app_key(client, init, 10, ["agy", "acp"], agent_id=AGENT,
                                                 key_source=lambda agent_id: SENTINEL)
        finally:
            client.close(5.0)
        return keyed, self.log.read_text()

    def test_the_key_the_agent_prints_never_reaches_its_log_file(self):
        keyed, stored = self.run_keyed(gc.AcpClient)
        self.assertTrue(keyed)
        self.assertNotIn(SENTINEL, stored)
        self.assertNotIn(SENTINEL[:20], stored, "not a piece of it either")
        self.assertEqual(stored.count("[redacted]"), 4, "every occurrence, the one split across two writes too")
        # The rest of the diagnostics is kept, in order, so the log is still useful.
        self.assertEqual([ln for ln in stored.splitlines()],
                         ["starting up", "auth debug: apiKey=[redacted] (len %d)" % len(SENTINEL),
                          "partial [redacted] end; again [redacted][redacted]", "bye"])

    def test_the_antigravity_client_asks_for_redaction_and_the_shared_default_is_unchanged(self):
        self.assertTrue(gc.AcpClient.redact_stderr)
        self.assertFalse(acp_client.AcpClient.redact_stderr, "other adapters keep the direct file handle")
        # The shared client without it writes the child's stderr as it comes: this is what the flag prevents.
        keyed, stored = self.run_keyed(acp_client.AcpClient)
        self.assertIn(SENTINEL, stored)

    def test_no_log_file_no_pipe_and_a_client_without_redact_is_still_authenticated(self):
        client = gc.AcpClient([sys.executable, str(self.script)], cwd=str(self.tmp), env=isolated_child_env())
        try:
            self.assertIsNone(client._stderr_pump, "nothing is stored, so nothing is filtered")
            client.redact("")
            self.assertEqual(client.secrets, [], "an empty secret is not a pattern")
        finally:
            client.close(5.0)
        agy = FakeAgy(["agy", "acp"])        # the fake has no `redact`: the helper must not need one
        self.assertTrue(gk.authenticate_with_app_key(agy, agy.initialize(1), 1, ["agy", "acp"], agent_id=AGENT,
                                                     key_source=lambda agent_id: SENTINEL))



NOISY_AGY = r"""
import json, os, sys
err = sys.stderr.buffer
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "initialize":
        result = {"protocolVersion": 1, "authMethods": [{"id": "gemini-api-key"}]}
    elif m.get("method") == "authenticate":
        key = m["params"]["_meta"]["apiKey"].encode()
        err.write(b"\xff\xfe not utf-8 \xc3\x28\n"); err.flush()
        err.write(b"bad lead \xe2" + key + b"\xff tail\n"); err.flush()
        err.write(b"split \x80" + key[:11]); err.flush()         # the key straddles two writes
        err.write(key[11:] + b"\xf0\x9f end\n"); err.flush()
        for n in range(4000):                                    # well past any pipe buffer
            err.write(b"filler %05d \xff " % n + b"x" * 80 + b"\n")
        err.write(b"after the flood " + key + b"\n"); err.flush()
        result = {}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
"""


class StderrThatIsNotUtf8(unittest.TestCase):
    """Bytes that are not UTF-8 on the agent's stderr: the client keeps reading, so the agent is never
    left blocked on a full pipe, and the key is still replaced before anything is stored."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agy-noisy-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.script = self.tmp / "noisy_agy.py"
        self.script.write_text(NOISY_AGY)
        self.log = self.tmp / "agy.stderr.log"

    def client(self):
        return gc.AcpClient([sys.executable, str(self.script)], cwd=str(self.tmp), env=isolated_child_env(),
                            stderr_path=str(self.log))

    def test_the_agent_answers_after_writing_more_than_a_pipe_of_undecodable_stderr(self):
        client = self.client()
        try:
            # The answer comes after ~400 KB of stderr: an agent blocked on the pipe would time out here.
            keyed = gk.authenticate_with_app_key(client, client.initialize(10), 20, ["agy", "acp"],
                                                 agent_id=AGENT, key_source=lambda agent_id: SENTINEL)
            self.assertTrue(client._stderr_pump.is_alive(), "still reading while the agent lives")
        finally:
            client.close(5.0)
        self.assertTrue(keyed)
        raw = self.log.read_bytes()
        stored = raw.decode("utf-8")                 # what is stored is valid UTF-8
        self.assertNotIn(SENTINEL, stored)
        self.assertNotIn(SENTINEL[:11], stored, "neither half of the key split across two writes")
        self.assertNotIn(SENTINEL[11:], stored)
        self.assertEqual(stored.count("[redacted]"), 3)
        lines = stored.splitlines()
        self.assertEqual(lines[1], "bad lead �[redacted]� tail")
        self.assertEqual(lines[2], "split �[redacted]� end")
        self.assertEqual(len([ln for ln in lines if ln.startswith("filler ")]), 4000, "nothing dropped")
        self.assertEqual(lines[-1], "after the flood [redacted]")

    def test_a_log_that_stops_taking_writes_does_not_stop_the_reading(self):
        client = self.client()
        try:
            init = client.initialize(10)
            client.err.close()                       # every later write to the log raises
            keyed = gk.authenticate_with_app_key(client, init, 20, ["agy", "acp"], agent_id=AGENT,
                                                 key_source=lambda agent_id: SENTINEL)
        finally:
            client.close(5.0)
        self.assertTrue(keyed, "the agent wrote its whole flood and answered")
        self.assertNotIn(SENTINEL.encode(), self.log.read_bytes())

    def test_a_text_stream_and_a_closed_pipe(self):
        class Lines:
            def __init__(self, items):
                self.items = list(items)

            def readline(self):
                item = self.items.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item

        def pumped(items):
            client = gc.AcpClient.__new__(gc.AcpClient)
            client.secrets, client.err = [SENTINEL], io.BytesIO()
            client.p = mock.Mock(stderr=Lines(items))
            client._pump_stderr()
            return client.err.getvalue().decode("utf-8")

        self.assertEqual(pumped(["key %s\n" % SENTINEL, ""]), "key [redacted]\n")
        self.assertEqual(pumped([b"one\n", ValueError("read of closed file"), b"never\n"]), "one\n")


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
