#!/usr/bin/env python3
"""A local agent restarts its CLI when its own sign-in changes (an account switch from the card or the
desktop's Settings), once the turn in progress is over, and only then: the same account, or a sign-in
file that cannot be read, restarts nothing. No tmux, no codex: the pane, the record and the server are
stood in."""
import base64
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ag2_sparrow import signin_watch  # noqa: E402
from ag2_sparrow.adapters.claude_code import launcher  # noqa: E402
from ag2_sparrow.adapters.codex import codex_connector as cc  # noqa: E402


def id_token(email):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return "h.%s.s" % payload


def write(path, data, bump=[0]):
    """Write `data` (JSON, or raw text) and move the mtime on, so a same-second rewrite is still seen."""
    Path(path).write_text(data if isinstance(data, str) else json.dumps(data))
    bump[0] += 1
    t = time.time() + bump[0]
    os.utime(path, (t, t))


def codex_auth(account, email):
    return {"OPENAI_API_KEY": None, "tokens": {"account_id": account, "id_token": id_token(email),
                                               "access_token": "SECRET-A", "refresh_token": "SECRET-R"}}


def claude_conf(uuid, email):
    return {"projects": {}, "oauthAccount": {"accountUuid": uuid, "emailAddress": email}}


class Readers(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.d = Path(d.name)

    def test_codex_reads_the_account_and_email_and_never_a_token(self):
        write(self.d / "auth.json", codex_auth("acct-1", "a@x.org"))
        got = signin_watch.codex_identity(str(self.d))
        self.assertEqual(got, ("acct-1", "a@x.org", ""))
        self.assertNotIn("SECRET", repr(got) + signin_watch.fingerprint(got))
        write(self.d / "auth.json", {"OPENAI_API_KEY": "sk-SECRET"})
        self.assertEqual(signin_watch.codex_identity(str(self.d)), ("api-key", "", ""))
        os.unlink(self.d / "auth.json")
        self.assertEqual(signin_watch.codex_identity(str(self.d)), signin_watch.SIGNED_OUT + ("",))
        write(self.d / "auth.json", "{not json")
        self.assertIsNone(signin_watch.codex_identity(str(self.d)))

    def test_the_switch_marker_is_part_of_the_identity_without_its_content(self):
        write(self.d / "auth.json", codex_auth("acct-1", "a@x.org"))
        before = signin_watch.codex_identity(str(self.d))
        write(self.d / signin_watch.MARKER, '{"email": "b@x.org", "at": 1}')
        after = signin_watch.codex_identity(str(self.d))
        self.assertEqual(after[:2], before[:2])
        self.assertNotEqual(signin_watch.fingerprint(after), signin_watch.fingerprint(before))
        self.assertNotIn("b@x.org", repr(after))
        os.unlink(self.d / signin_watch.MARKER)
        os.mkdir(self.d / signin_watch.MARKER)                # refused: the whole identity is unreadable
        self.assertIsNone(signin_watch.codex_identity(str(self.d)))

    def test_claude_reads_oauth_account_only(self):
        f = self.d / ".claude.json"
        write(f, claude_conf("u-1", "a@x.org"))
        self.assertEqual(signin_watch.claude_identity(str(f), str(self.d)), ("u-1", "a@x.org", ""))
        write(f, {"projects": {}})
        self.assertEqual(signin_watch.claude_identity(str(f), str(self.d)), signin_watch.SIGNED_OUT + ("",))
        write(f, '{"oauthAccount": {"accountUu')       # caught mid-write
        self.assertIsNone(signin_watch.claude_identity(str(f), str(self.d)))


# ── Codex ────────────────────────────────────────────────────────────────────

class Server:
    """The app-server, as far as the sign-in check talks to it: `account/read` names its account."""

    def __init__(self, email):
        self.email, self.events, self.p = email, None, types.SimpleNamespace(pid=1)

    def request(self, method, params=None, timeout=120):
        assert method == "account/read", method
        return {"account": {"type": "chatgpt", "email": self.email}}

    def notify(self, method, params=None):
        pass

    def close(self):
        pass


class CodexRestart(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = Path(d.name)
        write(self.home / "auth.json", codex_auth("acct-1", "a@x.org"))
        self.servers = []

        def factory():
            # A new server signs in with whatever auth.json holds when it starts.
            now = signin_watch.codex_identity(str(self.home)) or ("", "")
            self.servers.append(Server(now[1]))
            return self.servers[-1]

        args = cc.build_parser().parse_args(["--work", str(self.home / "w")])
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            self.c = cc.Connector(factory, args, note="n")
        self.c.handshake = lambda: None
        self.restarts = mock.patch.object(self.c, "restart_server", wraps=self.c.restart_server).start()
        self.addCleanup(mock.patch.stopall)

    def test_an_account_switch_restarts_the_server_exactly_once(self):
        self.assertFalse(self.c.signin_check())
        write(self.home / "auth.json", codex_auth("acct-2", "b@x.org"))      # the desktop switched account
        self.assertTrue(self.c.signin_check())
        self.assertEqual(self.servers[-1].email, "b@x.org", "the new server is on the new account")
        for _ in range(3):
            self.assertFalse(self.c.signin_check())
        self.assertEqual(self.restarts.call_count, 1)

    def test_the_check_runs_between_turns_in_serve_so_the_next_task_meets_the_new_account(self):
        order = []
        self.c.signin_check = lambda: order.append("check") or False
        self.c.pending_tasks = lambda seen: order.append("tasks") or []
        self.c.args.idle_exit, self.c.args.poll = 0.0001, 0.0001
        self.c.args.state, self.c.args.bridge_state = str(self.home / "state"), None
        os.makedirs(self.c.args.state)
        self.c.rpc.events = __import__("queue").Queue()
        with mock.patch.object(cc.connector_cards, "absorb"), mock.patch.object(type(self.c), "accounts",
                                                                                  mock.PropertyMock()):
            self.c.serve()
        self.assertEqual(order[:2], ["check", "tasks"])

    def test_a_switch_marker_restarts_once_unless_the_server_is_already_on_it(self):
        # The desktop swapped auth.json and wrote its marker: the old server still answers the old email.
        write(self.home / "auth.json", codex_auth("acct-2", "b@x.org"))
        write(self.home / signin_watch.MARKER, "b@x.org 1")
        self.assertTrue(self.c.signin_check())
        self.assertFalse(self.c.signin_check())
        # A marker rewritten while the server already runs on that account: kept.
        write(self.home / signin_watch.MARKER, "b@x.org 2")
        self.assertFalse(self.c.signin_check())
        self.assertFalse(self.c.signin_check())
        self.assertEqual(self.restarts.call_count, 1)

    def test_the_same_account_restarts_nothing(self):
        write(self.home / "auth.json", codex_auth("acct-1", "a@x.org"))       # rewritten (a token refresh)
        for _ in range(3):
            self.assertFalse(self.c.signin_check())
        self.restarts.assert_not_called()

    def test_an_unreadable_file_restarts_nothing(self):
        write(self.home / "auth.json", "{torn")
        self.assertFalse(self.c.signin_check())
        os.unlink(self.home / "auth.json")
        os.mkdir(self.home / "auth.json")                    # not a regular file: refused, not read
        self.assertFalse(self.c.signin_check())
        self.restarts.assert_not_called()

    def test_a_server_already_on_the_new_account_is_kept(self):
        # The card's own sign-in: the server wrote auth.json itself and already runs on it.
        write(self.home / "auth.json", codex_auth("acct-2", "b@x.org"))
        self.servers[-1].email = "b@x.org"
        self.assertFalse(self.c.signin_check())
        self.assertFalse(self.c.signin_check())
        self.restarts.assert_not_called()

    def test_the_shared_daemon_is_never_restarted(self):
        self.c.args.server = "proxy"
        write(self.home / "auth.json", codex_auth("acct-2", "b@x.org"))
        self.assertFalse(self.c.signin_check())
        self.restarts.assert_not_called()


# ── Claude Code ──────────────────────────────────────────────────────────────

GEN = {"nonce": "a" * 32, "pane_pid": 7, "session_id": "$1", "session_created": 5, "pane_id": "%1"}
CONSUMER = {"pid": 11, "start": "s"}
LIMIT = "You've hit your limit · resets 3pm\n❯ 1. Wait for limit to reset\n  2. Switch to usage credits"


class ClaudeRestart(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.conf = Path(d.name) / ".claude.json"
        write(self.conf, claude_conf("u-1", "a@x.org"))
        lc = launcher.Launcher.__new__(launcher.Launcher)
        lc.logs, lc.now, lc.stops, lc.keys, lc.pane = [], 1000.0, [], [], "❯ "
        lc.log, lc.sleep, lc.clock = lc.logs.append, lambda s: None, lambda: lc.now
        lc.tm = types.SimpleNamespace(capture=lambda pane_id, styled=False: lc.pane, sock="/s", tmux="tmux")
        self.marker = Path(d.name) / signin_watch.MARKER
        lc.signin = signin_watch.SigninWatch([str(self.conf), str(self.marker)],
                                             lambda: signin_watch.claude_identity(str(self.conf), d.name))
        lc.signin_next, lc.signin_waiting, lc.observation = 0.0, False, {"task": None}
        lc.rec = {"generation": GEN, "title_token": "tok", "consumer": CONSUMER}
        lc.rec["signin"] = lc.signin_mark(CONSUMER)        # what create() records

        def with_record(mutate):
            lc.rec = mutate(lc.rec) or lc.rec
            return lc.rec
        lc._with_record = with_record
        lc.stop_session = lambda rec, why: lc.stops.append(why) or "stopped"
        lc.send_keys = lambda gen, title, *names: lc.keys.append(names) or 0
        self.lc = lc

    def check(self):
        self.lc.now += launcher.SIGNIN_CHECK_S
        restarted, self.lc.rec = self.lc.signin_check(self.lc.rec)
        return restarted

    def test_an_account_switch_restarts_once_after_the_turn(self):
        lc = self.lc
        lc.observation = {"task": "task-1"}                  # a turn is running
        write(self.conf, claude_conf("u-2", "b@x.org"))
        self.assertFalse(self.check())
        self.assertFalse(self.check())
        self.assertEqual(lc.stops, [], "never in the middle of a turn")
        self.assertEqual(sum("once the current turn ends" in l for l in lc.logs), 1)
        lc.observation = {"task": None}                      # the turn ended
        self.assertTrue(self.check())
        self.assertEqual(len(lc.stops), 1)
        self.assertIsNone(lc.rec["generation"], "the next tick creates the new session")
        # The new session (create() marks it) runs on the new account: nothing more.
        lc.rec = dict(lc.rec, generation=GEN, consumer={"pid": 12, "start": "t"})
        lc.rec["signin"] = lc.signin_mark(lc.rec["consumer"])
        for _ in range(3):
            self.assertFalse(self.check())
        self.assertEqual(len(lc.stops), 1)

    def test_a_keychain_only_switch_restarts_once_through_the_marker(self):
        # The desktop changed only the Keychain item: .claude.json keeps the old email, the marker moves.
        lc = self.lc
        lc.observation = {"task": "task-1"}
        write(self.marker, '{"email": "b@x.org", "at": 1}')
        self.assertFalse(self.check())
        lc.observation = {"task": None}
        self.assertTrue(self.check())
        self.assertEqual(len(lc.stops), 1)
        lc.rec = dict(lc.rec, generation=GEN, consumer={"pid": 12, "start": "t"})
        lc.rec["signin"] = lc.signin_mark(lc.rec["consumer"])
        for _ in range(3):
            self.assertFalse(self.check())
        self.assertEqual(len(lc.stops), 1)

    def test_a_marker_seen_after_the_desktop_restarted_claude_restarts_nothing(self):
        lc = self.lc
        write(self.marker, '{"email": "b@x.org", "at": 1}')
        lc.rec = dict(lc.rec, consumer={"pid": 13, "start": "u"})      # already restarted on the new account
        self.assertFalse(self.check())
        self.assertFalse(self.check())
        self.assertEqual(lc.stops, [])

    def test_a_usage_limit_menu_is_dismissed_and_does_not_wait_for_the_turn(self):
        lc = self.lc
        lc.observation, lc.pane = {"task": "task-1"}, LIMIT  # the turn is stuck behind the limit
        write(self.conf, claude_conf("u-2", "b@x.org"))
        self.assertTrue(self.check())
        self.assertEqual((lc.keys, len(lc.stops)), ([("Escape",)], 1))

    def test_the_same_account_restarts_nothing(self):
        write(self.conf, dict(claude_conf("u-1", "a@x.org"), numStartups=9))   # the CLI rewrote its config
        for _ in range(3):
            self.assertFalse(self.check())
        self.assertEqual(self.lc.stops, [])

    def test_an_unreadable_file_restarts_nothing(self):
        write(self.conf, '{"oauthAccount": {"accountUuid": "u-2", "emailAdd')
        self.assertFalse(self.check())
        os.unlink(self.conf)
        os.mkdir(self.conf)                                  # not a regular file: refused, not read
        self.assertFalse(self.check())
        self.assertEqual(self.lc.stops, [])

    def test_a_consumer_already_restarted_elsewhere_is_marked_not_restarted(self):
        # The desktop restarted claude on the new account: a consumer this mark was not made for.
        lc = self.lc
        write(self.conf, claude_conf("u-2", "b@x.org"))
        lc.rec = dict(lc.rec, consumer={"pid": 13, "start": "u"})
        self.assertFalse(self.check())
        self.assertFalse(self.check())
        self.assertEqual(lc.stops, [])
        self.assertEqual(lc.rec["signin"]["consumer"], {"pid": 13, "start": "u"})

    def test_a_failed_stop_is_retried_and_counts_once(self):
        lc = self.lc
        outcomes = iter(["busy: another pane-key sender holds the guard", "stopped"])
        lc.stop_session = lambda rec, why: lc.stops.append(why) or next(outcomes)
        write(self.conf, claude_conf("u-2", "b@x.org"))
        self.assertFalse(self.check())
        self.assertIsNotNone(lc.rec["generation"])
        self.assertTrue(self.check())
        self.assertEqual(len(lc.stops), 2)


if __name__ == "__main__":
    unittest.main()
