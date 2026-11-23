#!/usr/bin/env python3
"""The core's account card (skills/account-switch/scripts/account_card.py): card, tap, switch, outcome.

- The card names the core's account masked: a Codex core's from codex_account, a Claude core's from
  `claude auth status --json`. A Codex core's card has a "Use" button per other account in the
  desktop's store and always "Add another account"; a Claude core's has only the latter.
- `post` saves the signed-in Codex account in the store in the desktop's format and key rule
  (16 hex of sha256(account id)), folder 0700 and file 0600.
- Only the owner's tap in the DM on a card this agent posted acts. "Add another account" points to
  Settings and starts nothing. "Use" starts the detached switch for a key that card offered and the
  store still holds; the account already in use, a second switch under way, or a Claude core start
  nothing.
- `switch` puts the saved sign-in in place, restarts the core, and posts the outcome; when the
  restart fails the previous sign-in is put back; a sign-in that is not the wanted one after the
  restart is reported as a failure.
- No token appears in any output or card.

The Action call, the core's row, the restart and the process launch are fakes; no network, no tmux.

Run: python3 tests/account-card.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "account-switch" / "scripts" / "account_card.py"
_spec = importlib.util.spec_from_file_location("account_card", SCRIPT)
acc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acc)
ac = acc.ac

import connections  # noqa: E402

DM, ROOM, OWNER = "!dm:s.space", "!team:s.space", "@owner:s.space"
CORE = {"instance": "s_dspace", "owner_mxid": OWNER, "agent_mxid": "@owner-mini.agent:s.space"}
SECRET_A, SECRET_B, SECRET_C = "tok-SECRET-alpha", "tok-SECRET-bravo", "tok-SECRET-charlie"


def jwt(claims: dict) -> str:
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def auth(email: str, plan: str, account_id: str, secret: str) -> str:
    claims = {"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": plan}}
    return json.dumps({"tokens": {"id_token": jwt(claims), "access_token": secret, "refresh_token": secret + "-r",
                                  "account_id": account_id}})


A = auth("alice@example.com", "plus", "acct-a", SECRET_A)
B = auth("bob@work.io", "pro", "acct-b", SECRET_B)
C = auth("carol@example.org", "team", "acct-c", SECRET_C)


def key_of(account_id: str) -> str:
    return hashlib.sha256(account_id.encode()).hexdigest()[:16]


def task(said, *, tier="owner", user=OWNER, kind="dm", room=DM, **extra):
    head = {"id": "task-1", "task_layout": "mid"}
    tail = {"source": "ag2space", "channel_id": room, "channel_kind": kind, "user_id": user, "access_tier": tier,
            "source_message_id": "$ask", "task_id": "task-1", **extra}
    return "\n".join([f"{k}: {v}" for k, v in head.items()] + [f"task: {said}"]
                     + [f"{k}: {v}" for k, v in tail.items()]) + "\n"


class Fake:
    def __init__(self):
        self.calls = []

    def execute(self, action, arguments, *, operation_id, room_id):
        self.calls.append((action, arguments, operation_id, room_id))
        return {"result": {"event_id": f"$card{len(self.calls)}"}}

    def cards(self):
        return [c[1]["extra_content"][ac.CARD_KEY] for c in self.calls]


class Launch:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return mock.Mock(pid=4242)


def codex_read_from(home_holder):
    """codex_account.read as a stand-in: the account named by the home's auth.json, no app-server."""
    def read(home):
        import codex_account
        return codex_account.from_auth_file(home)
    return read


class Base(unittest.TestCase):
    RUNTIME = "codex"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.ws, self.home, self.app = root / "ws", root / "codex-home", root / "app-support"
        for d in (self.ws, self.home, self.app):
            d.mkdir()
        self.store = self.app / "accounts" / "codex"
        self.fake, self.launch, self.restarts, self.outputs = Fake(), Launch(), [], []
        self.restart_rc, self.restart_writes = 0, None
        self.ready_ok, self.live_answer, self.ready_since = True, None, []
        self.claude_json = {"loggedIn": True, "email": "susan@example.com", "subscriptionType": "max"}
        for target, name, value in ((acc, "_owner_dm", lambda ws, core: DM),
                                    (acc, "runtime", lambda: self.RUNTIME),
                                    (acc, "codex_home", lambda: self.home),
                                    (connections, "read_rows", lambda ws=None: [CORE])):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {"SUTANDO_APP_SUPPORT": str(self.app)})
        env.start()
        self.addCleanup(env.stop)

    def signed_in(self, text):
        (self.home / "auth.json").write_text(text)

    def live(self):
        return (self.home / "auth.json").read_text()

    def keep(self, text):
        """A saved account, as the desktop writes it."""
        acc.save_current(self.store, text)

    def claude_run(self, argv, **kw):
        self.assertEqual(argv[:3], ["claude", "auth", "status"])
        return mock.Mock(stdout=json.dumps(self.claude_json), returncode=0)

    def restart(self):
        self.restarts.append(self.live())
        if self.restart_writes is not None:
            self.signed_in(self.restart_writes)
        return self.restart_rc

    def ready(self, ws, since):
        self.ready_since.append(since)
        return self.ready_ok

    def live_account(self, home):
        """Codex's answer for the restarted core: the home's sign-in unless a test says otherwise."""
        if self.live_answer is not None:
            return self.live_answer
        import codex_account
        return codex_account.from_auth_file(home) or {}

    def run_(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = acc.main(["--workspace", str(self.ws)] + argv, core=CORE, popen=self.launch, run=self.claude_run,
                          execute=lambda *a, **k: self.fake.execute(*a, **k), codex_read=codex_read_from(self),
                          restart=self.restart, sleep=lambda s: None, ready=self.ready, live=self.live_account)
        self.outputs.append(out.getvalue())
        return rc, json.loads(out.getvalue())

    def post(self):
        return self.run_(["post", "--room", DM])

    def tap(self, label, *, event="$card1", card_id=acc.CARD_ID, **kw):
        kw.setdefault("reply_to_event", event)
        kw.setdefault("card_click", json.dumps({"card_id": card_id, "action": label}))
        path = self.ws / "task.txt"
        path.write_text(task(label, **kw))
        return self.run_(["reply", "--task-file", str(path)])

    def leaked(self):
        seen = "".join(self.outputs) + json.dumps([c[1] for c in self.fake.calls])
        log = self.ws / "logs" / "account-card.log"
        seen += log.read_text() if log.exists() else ""
        return [s for s in (SECRET_A, SECRET_B, SECRET_C, "refresh_token", "id_token") if s in seen]

    def tearDown(self):
        self.assertEqual(self.leaked(), [], "a token reached an output, a card or the log")


class CodexCard(Base):
    def test_card_names_the_account_masked_and_offers_the_other_saved_ones(self):
        self.signed_in(A)
        self.keep(B)
        self.keep(C)
        rc, out = self.post()
        self.assertEqual((rc, out["status"]), (0, "posted"))
        card = self.fake.cards()[0]
        self.assertIsNone(ac.problem(card))
        self.assertEqual(card["id"], acc.CARD_ID)
        self.assertIn("a…@example.com (plus)", card["text"])
        self.assertNotIn("alice", json.dumps(card))
        labels = [b["label"] for b in card["buttons"]]
        self.assertEqual(labels, ["Use b…@work.io (pro)", "Use c…@example.org (team)", "Add another account"])
        self.assertEqual([b["command"] for b in card["buttons"]],
                         [f"card:account:core:{key_of('acct-b')}", f"card:account:core:{key_of('acct-c')}",
                          "card:account:core:add"])
        self.assertEqual(self.fake.calls[0][3], DM)

    def test_post_saves_the_signed_in_account_in_the_desktop_format_privately(self):
        self.signed_in(A)
        self.post()
        path = self.store / f"{key_of('acct-a')}.json"
        self.assertEqual(json.loads(path.read_text()), {"label": "alice@example.com (plus)", "auth": A})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o700)
        self.assertEqual([b["label"] for b in self.fake.cards()[0]["buttons"]], ["Add another account"])
        self.assertIn("No other account is saved", self.fake.cards()[0]["text"])

    def test_an_api_key_sign_in_is_named_and_not_saved(self):
        self.signed_in(json.dumps({"OPENAI_API_KEY": "sk-SECRET-key"}))
        self.post()
        self.assertIn("an OpenAI API key", self.fake.cards()[0]["text"])
        self.assertFalse(self.store.exists())
        self.assertNotIn("sk-SECRET-key", "".join(self.outputs))

    def test_the_card_goes_only_to_the_owner_dm(self):
        self.signed_in(A)
        rc, out = self.run_(["post", "--room", ROOM])
        self.assertEqual((rc, out["status"]), (1, "failed"))
        self.assertEqual(self.fake.calls, [])

    def test_status_lists_in_use_and_saved_masked(self):
        self.signed_in(A)
        self.keep(B)
        rc, out = self.run_(["status"])
        self.assertEqual(rc, 0)
        self.assertEqual(out["runtime"], "codex")
        self.assertEqual([s["label"] for s in out["saved"]], ["b…@work.io (pro)"])


class ClaudeCard(Base):
    RUNTIME = "claude"

    def test_claude_core_card_names_the_account_and_offers_only_settings(self):
        rc, out = self.post()
        self.assertEqual(rc, 0)
        card = self.fake.cards()[0]
        self.assertIn("Claude Code", card["text"])
        self.assertIn("s…@example.com (max)", card["text"])
        self.assertEqual([b["label"] for b in card["buttons"]], ["Add another account"])
        self.assertFalse(self.store.exists())

    def test_signed_out_and_unreadable(self):
        self.claude_json = {"loggedIn": False}
        self.post()
        self.assertIn("is not signed in", self.fake.cards()[0]["text"])
        self.claude_json = "nonsense"
        self.post()
        self.assertIn("could not read", self.fake.cards()[1]["text"])

    def test_a_use_tap_on_a_claude_core_starts_nothing(self):
        self.post()
        rc, out = self.tap("Use x", card_click=json.dumps({"card_id": acc.CARD_ID, "action": "Use x"}))
        self.assertNotEqual(out.get("status"), "started")
        self.assertEqual(self.launch.calls, [])


class Taps(Base):
    def setUp(self):
        super().setUp()
        self.signed_in(A)
        self.keep(B)
        self.post()
        self.use_b = "Use b…@work.io (pro)"

    def test_add_points_to_settings_and_starts_nothing(self):
        rc, out = self.tap("Add another account")
        self.assertEqual((rc, out["status"]), (0, "say"))
        self.assertIn("Settings → Connections", out["say"])
        self.assertIn("never in this chat", out["say"])
        self.assertEqual(self.launch.calls, [])
        self.assertEqual(self.fake.cards()[-1]["id"], acc.NOTE_ID)

    def test_the_owners_use_tap_starts_the_detached_switch(self):
        rc, out = self.tap(self.use_b)
        self.assertEqual((rc, out["status"]), (0, "started"))
        argv, kw = self.launch.calls[0]
        self.assertIn("switch", argv)
        self.assertEqual(argv[argv.index("--key") + 1], key_of("acct-b"))
        self.assertEqual(argv[argv.index("--room") + 1], DM)
        self.assertTrue(kw["start_new_session"])
        self.assertIn("Switching the core to b…@work.io (pro)", out["say"])
        self.assertEqual(self.fake.cards()[-1]["state"], "pending")
        self.assertEqual(self.live(), A, "nothing is swapped before the detached step")

    def test_someone_else_or_another_room_starts_nothing(self):
        for kw in ({"user": "@guest:s.space", "tier": "guest"}, {"kind": "group", "room": ROOM}):
            rc, out = self.tap(self.use_b, **kw)
            self.assertEqual(rc, 1, kw)
            self.assertEqual(out["status"], ac.FORGED, kw)
        self.assertEqual(self.launch.calls, [])

    def test_a_key_the_store_no_longer_holds_is_gone(self):
        (self.store / f"{key_of('acct-b')}.json").unlink()
        rc, out = self.tap(self.use_b)
        self.assertEqual((rc, out["say"]), (2, acc.GONE))
        self.assertEqual(self.launch.calls, [])

    def test_the_account_in_use_changes_nothing(self):
        self.signed_in(B)
        rc, out = self.tap(self.use_b)
        self.assertEqual(out["status"], "say")
        self.assertIn("already signed in", out["say"])
        self.assertEqual(self.launch.calls, [])

    def test_a_second_switch_while_one_runs_is_refused(self):
        self.tap(self.use_b)
        rc, out = self.tap(self.use_b)
        self.assertEqual(out["say"], acc.BUSY)
        self.assertEqual(len(self.launch.calls), 1)


class Switch(Base):
    def setUp(self):
        super().setUp()
        self.signed_in(A)
        self.keep(B)
        self.post()
        self.tap("Use b…@work.io (pro)")
        argv = self.launch.calls[0][0]
        self.attempt = argv[argv.index("--attempt") + 1]
        self.key = key_of("acct-b")

    def switch(self, attempt=None):
        return self.run_(["switch", "--room", DM, "--key", self.key, "--attempt", attempt or self.attempt])

    def test_switched_sign_in_verified_and_restart_requested(self):
        rc, out = self.switch()
        self.assertEqual((rc, out["outcome"]), (0, "done"))
        self.assertEqual(self.restarts, [B], "the saved sign-in is in place before the restart")
        self.assertEqual(self.live(), B)
        self.assertEqual(stat.S_IMODE((self.home / "auth.json").stat().st_mode), 0o600)
        self.assertTrue((self.store / f"{key_of('acct-a')}.json").exists(), "the previous account stays saved")
        self.assertEqual(out["say"], "I asked the core to restart with b…@work.io (pro)'s sign-in, and its heartbeat "
                                     "came back after the request. Saved sign-in verified: Codex confirms it is "
                                     "b…@work.io (pro). The running core's own account isn't confirmed.")
        self.assertEqual((out["verified"], out["restart"], out["core_identity"]),
                         ("saved-sign-in", "requested", "unconfirmed"))
        last = self.fake.cards()[-1]
        self.assertEqual((last["id"], last["state"]), (acc.NOTE_ID, "done"))
        self.assertFalse(self.ws.joinpath(*acc.RUNNING).exists())

    def test_a_restart_that_did_not_happen_puts_the_previous_sign_in_back(self):
        self.restart_rc = 4
        rc, out = self.switch()
        self.assertEqual((rc, out["outcome"]), (1, "error"))
        self.assertEqual(self.live(), A)
        self.assertIn("I'm still signed in as a…@example.com (plus)", out["say"])
        self.assertIn("put off", out["say"])

    def test_a_sign_in_overwritten_during_the_restart_is_reported(self):
        self.restart_writes = A
        rc, out = self.switch()
        self.assertEqual(out["outcome"], "error")
        self.assertIn("But its saved sign-in is a…@example.com (plus), not b…@work.io (pro)", out["say"])

    def reset_attempt(self):
        rec = {"attempt": self.attempt, "key": self.key, "room": DM, "ts": 1e12}
        self.ws.joinpath(*acc.RUNNING).write_text(json.dumps(rec))

    def test_an_unreadable_previous_sign_in_is_never_replaced(self):
        (self.home / "auth.json").unlink()
        (self.home / "auth.json").mkdir()
        rc, out = self.switch()
        self.assertEqual((rc, out["outcome"]), (1, "error"))
        self.assertIn("could not be read", out["say"])
        self.assertEqual(self.restarts, [])
        self.assertTrue((self.home / "auth.json").is_dir())

    def test_a_previous_sign_in_that_cannot_be_saved_is_never_replaced(self):
        for text in ("{not json", json.dumps({"OPENAI_API_KEY": "sk-SECRET"})):
            with self.subTest(text=text[:8]):
                self.signed_in(text)
                self.restarts.clear()
                rc, out = self.switch()
                self.assertEqual(out["outcome"], "error")
                self.assertIn("left it in place", out["say"])
                self.assertEqual((self.restarts, self.live()), ([], text))
                self.assertNotIn("sk-SECRET", json.dumps(out))
                self.reset_attempt()

    def test_a_definite_restart_failure_restores_an_absent_sign_in(self):
        (self.home / "auth.json").unlink()
        for rc_ in acc.NOT_RESTARTED:
            with self.subTest(rc=rc_):
                self.restart_rc = rc_
                rc, out = self.switch()
                self.assertEqual(out["outcome"], "error")
                self.assertFalse((self.home / "auth.json").exists(), "absence is put back too")
                self.reset_attempt()

    def test_an_uncertain_restart_failure_keeps_the_new_sign_in_and_says_so(self):
        self.restart_rc = 1
        rc, out = self.switch()
        self.assertEqual(out["outcome"], "error")
        self.assertEqual(self.live(), B, "the core may already run on it, so it is not swapped back")
        self.assertIn("can't tell which account", out["say"])

    def test_no_fresh_heartbeat_is_said_and_the_sign_in_still_verified(self):
        self.ready_ok = False
        rc, out = self.switch()
        self.assertEqual(out["outcome"], "info")
        self.assertIn("I haven't seen a heartbeat from the core session since", out["say"])
        self.assertIn("Saved sign-in verified", out["say"])
        self.assertEqual(len(self.ready_since), 1)

    def test_an_early_refusal_requests_no_restart(self):
        (self.home / "auth.json").write_text("{not json")
        rc, out = self.switch()
        self.assertEqual((out["outcome"], out["restart"]), ("error", "not-requested"))
        self.assertEqual(self.restarts, [])

    def test_a_restart_that_was_put_off_was_still_requested(self):
        self.restart_rc = 4
        rc, out = self.switch()
        self.assertEqual((out["outcome"], out["restart"]), ("error", "requested"))

    def test_codex_not_confirming_the_saved_sign_in_is_an_error(self):
        self.live_answer = {}
        rc, out = self.switch()
        self.assertEqual(out["outcome"], "error")
        self.assertIn("did not confirm", out["say"])

    def test_codex_naming_another_account_is_an_error(self):
        self.live_answer = {"kind": "chatgpt", "email": "alice@example.com"}
        rc, out = self.switch()
        self.assertEqual(out["outcome"], "error")
        self.assertIn("not b…@work.io (pro)", out["say"])

    def assertNoRestartClaim(self, ready, answer):
        self.ready_ok, self.live_answer = ready, answer
        rc, out = self.switch()
        self.assertNotIn("restarted", out["say"])
        self.assertNotIn("Switched", out["say"])
        self.assertIn("I asked the core to restart", out["say"])

    def test_a_fresh_beat_and_the_wanted_account_claims_no_restart(self):
        self.assertNoRestartClaim(True, None)

    def test_no_beat_and_the_wanted_account_claims_no_restart(self):
        self.assertNoRestartClaim(False, None)

    def test_a_fresh_beat_and_no_confirmation_claims_no_restart(self):
        self.assertNoRestartClaim(True, {})

    def test_a_fresh_beat_and_another_account_claims_no_restart(self):
        self.assertNoRestartClaim(True, {"kind": "chatgpt", "email": "alice@example.com"})

    def test_settlement_is_bound_to_the_whole_recorded_attempt(self):
        rec = json.loads(self.ws.joinpath(*acc.RUNNING).read_text())
        for field, value in (("key", key_of("acct-c")), ("room", ROOM)):
            with self.subTest(field=field):
                self.ws.joinpath(*acc.RUNNING).write_text(json.dumps({**rec, field: value}))
                rc, out = self.switch()
                self.assertEqual(rc, 2)
                self.assertEqual(self.restarts, [])
                self.assertTrue(self.ws.joinpath(*acc.RUNNING).exists(), "another attempt's record stays")

    def test_a_newer_attempt_recorded_mid_switch_is_not_cleared(self):
        newer = {"attempt": "9-cafebabe", "key": self.key, "room": DM, "ts": 2e12}

        def restart():
            self.ws.joinpath(*acc.RUNNING).write_text(json.dumps(newer))
            return 0
        self.restart = restart
        self.switch()
        self.assertEqual(json.loads(self.ws.joinpath(*acc.RUNNING).read_text()), newer)

    def test_a_switch_already_executing_refuses_a_second_and_blocks_admission(self):
        from file_lock import try_lock_fd, unlock_fd
        fd = os.open(str(self.ws.joinpath(*acc.EXEC_LOCK)), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self.assertTrue(try_lock_fd(fd))
            self.assertTrue(acc.executing(self.ws))
            rc, out = self.switch()
            self.assertEqual((rc, out["why"]), (2, "another switch is running"))
            self.assertEqual(self.restarts, [])
            self.ws.joinpath(*acc.RUNNING).unlink()
            self.assertFalse(acc.admit(self.ws, {"attempt": "x", "ts": 1.0}, 1.0))
            unlock_fd(fd)
        finally:
            os.close(fd)
        self.assertFalse(acc.executing(self.ws))
        self.assertTrue(acc.admit(self.ws, {"attempt": "x", "ts": 1.0}, 1.0))
        self.assertFalse(acc.admit(self.ws, {"attempt": "y", "ts": 2.0}, 2.0), "one attempt at a time")

    def test_another_attempt_or_a_bad_key_is_refused(self):
        rc, out = self.switch("0-deadbeef")
        self.assertEqual(rc, 2)
        self.assertEqual(self.restarts, [])
        rc, out = self.run_(["switch", "--room", DM, "--key", "../../x", "--attempt", self.attempt])
        self.assertEqual(rc, 2)
        self.assertEqual(self.live(), A)

    def test_restart_runs_the_graceful_restart_outside_the_in_session_guard(self):
        seen = {}

        def run(argv, **kw):
            seen.update(argv=argv, env=kw["env"])
            return mock.Mock(returncode=0)
        self.assertEqual(acc.restart_core(run), 0)
        self.assertEqual(seen["argv"], ["bash", str(acc.RESTART)])
        self.assertEqual(seen["env"]["SUTANDO_ALLOW_INSESSION_RESTART"], "1")
        self.assertTrue(acc.RESTART.is_file())


class Edges(Base):
    """The failure branches: each says what it could not do and changes nothing it should not."""

    def test_runtime_codex_home_and_store_resolve_from_config(self):
        import sutando_config
        import codex_account
        for value, want in (("codex", "codex"), ("claude", "claude")):
            with mock.patch.object(sutando_config, "resolve_core_runtime", return_value=value):
                self.assertEqual(_real_runtime(), want)
        with mock.patch.object(sutando_config, "resolve_core_runtime", side_effect=RuntimeError("x")):
            self.assertEqual(_real_runtime(), "claude")
        with mock.patch.object(codex_account, "home", return_value=self.home):
            self.assertEqual(_real_codex_home(), self.home)
        with mock.patch.dict(os.environ, {"SUTANDO_APP_SUPPORT": ""}):
            with mock.patch.object(sutando_config, "desktop_runtime_selection_path",
                                   return_value=self.app / "runtime.json"):
                self.assertEqual(acc.store_dir(), self.store)
            with mock.patch.object(sutando_config, "desktop_runtime_selection_path", side_effect=RuntimeError("x")):
                self.assertIsNone(acc.store_dir())

    def test_saved_skips_what_it_cannot_read(self):
        self.keep(B)
        (self.store / "notakey.json").write_text("{}")
        (self.store / ("0" * 16 + ".json")).write_text("{")
        (self.store / ("1" * 16 + ".json")).write_text(json.dumps({"label": 3}))
        self.assertEqual([x["key"] for x in acc.saved(self.store)], [key_of("acct-b")])
        self.assertIsNone(acc._live_auth(self.ws / "nowhere"))

    def test_claude_status_unreadable_answers(self):
        import sutando_config
        with mock.patch.object(sutando_config, "resolve_claude_sutando_config_dir", side_effect=RuntimeError("x")):
            for run in (mock.Mock(side_effect=OSError("no claude")), mock.Mock(return_value=mock.Mock(stdout="nope")),
                        mock.Mock(return_value=mock.Mock(stdout="[]"))):
                self.assertEqual(acc.claude_status(run)["signed_in"], None)

    def test_current_without_a_usable_home_or_with_an_api_key(self):
        import codex_account
        with mock.patch.object(acc, "codex_home", side_effect=codex_account.NoHome("refused")):
            self.assertEqual(acc.current("codex")["signed_in"], None)
            rc, out = self.post()
            self.assertIn("could not read", out.get("why", "") + json.dumps(self.fake.calls))
        self.signed_in(json.dumps({"OPENAI_API_KEY": "sk-SECRET"}))
        with mock.patch.object(codex_account, "read", return_value={"kind": "apiKey"}):
            self.assertEqual(acc.current("codex")["shown"], "an OpenAI API key")
        (self.home / "auth.json").unlink()
        self.assertFalse(acc.current("codex", codex_read=lambda h: {})["signed_in"])

    def test_the_core_lane_helpers_ask_pool_provision(self):
        pp = mock.Mock()
        pp.core_lane.return_value = CORE
        pp.core_actions.return_value = (mock.Mock(execute="EXEC"),)
        with mock.patch.dict(sys.modules, {"pool_provision": pp}):
            self.assertEqual(acc._core(self.ws), CORE)
            self.assertEqual(acc._execute(self.ws, "s_dspace"), "EXEC")
        with mock.patch.object(connections, "owner_dm", return_value=DM):
            self.assertEqual(_real_owner_dm(self.ws, CORE), DM)

    def test_post_failures_are_named(self):
        body, card = acc.notice_card("info", "x", "words")
        self.assertEqual(acc._post(self.ws, ROOM, body, card, "op", core=CORE, execute=self.fake.execute)["status"],
                         "failed")
        self.assertIn("RuntimeError", acc._post(self.ws, DM, body, card, "op", core=CORE,
                                                 execute=mock.Mock(side_effect=RuntimeError("down")))["why"])
        with mock.patch.object(acc.issued_cards, "record", side_effect=OSError("ro")):
            out = acc._post(self.ws, DM, body, card, "op", core=CORE, execute=self.fake.execute)
        self.assertEqual(out["status"], "posted")
        self.assertIn("will not act", out["why"])

    def test_cmd_post_degrades_and_prints(self):
        self.signed_in(A)
        with mock.patch.object(acc, "save_current", side_effect=OSError("ro")), \
                mock.patch.object(acc, "remember_keys", side_effect=OSError("ro")):
            rc, out = self.post()
        self.assertEqual(out["status"], "posted")
        self.assertIn("will not act", out["why"])
        with mock.patch.object(acc, "menu_card", side_effect=acc.ac.CardError("too long")):
            rc, out = self.post()
        self.assertEqual((rc, out["status"]), (1, "failed"))
        rc, out = self.run_(["post", "--room", DM, "--print"])
        self.assertEqual(out["handler"], acc.HANDLER)

    def test_reply_failures_change_nothing(self):
        rc, out = self.run_(["reply", "--task-file", str(self.ws / "gone.txt")])
        self.assertEqual((rc, out["status"]), (2, "refused"))
        self.signed_in(A)
        self.keep(B)
        self.post()
        use = "Use b…@work.io (pro)"
        with mock.patch.object(acc, "admit", side_effect=OSError("ro")):
            rc, out = self.tap(use)
        self.assertEqual((rc, out["status"]), (1, "failed"))
        self.launch = mock.Mock(side_effect=OSError("no fork"))
        rc, out = self.tap(use)
        self.assertEqual((rc, out["status"]), (1, "failed"))
        self.assertFalse(self.ws.joinpath(*acc.RUNNING).exists(), "a failed start clears its own record")

    def test_reply_without_a_core_row_and_to_another_target(self):
        self.signed_in(A)
        self.post()
        path = self.ws / "task.txt"
        path.write_text(task("Use x", reply_to_event="$card1",
                             card_click=json.dumps({"card_id": acc.CARD_ID, "action": "Use x"})))
        verdict = mock.Mock(command=mock.Mock(target="worker", verb="add"))
        with mock.patch.object(acc.ac, "click_verdict", return_value=verdict), \
                mock.patch.object(acc.ac, "click_answer", return_value=None), \
                mock.patch.object(acc, "_core", side_effect=RuntimeError("no row")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = acc.main(["--workspace", str(self.ws), "reply", "--task-file", str(path)])
        self.assertEqual((rc, json.loads(out.getvalue())["say"]), (2, acc.GONE))

    def test_restart_core_timeout_and_missing_bash(self):
        self.assertEqual(acc.restart_core(mock.Mock(side_effect=acc.subprocess.TimeoutExpired("bash", 1))), 124)
        self.assertEqual(acc.restart_core(mock.Mock(side_effect=OSError("no bash"))), 127)

    def beat(self, at, session="sutando-core", socket="/tmp/sutando-tmux.sock"):
        cores = self.ws / "state" / "cores"
        cores.mkdir(parents=True, exist_ok=True)
        (cores / "h.alive").write_text(json.dumps({"pid": 1, "heartbeat_pid": 2, "session": session,
                                                    "socket": socket, "last_beat_at": at}))

    def fresh(self, since):
        import util_paths
        with mock.patch.object(util_paths, "_host_label", return_value="h"):
            return acc.fresh_beat(self.ws, since)

    def test_a_beat_after_the_request_from_the_core_session_is_fresh(self):
        self.beat(1001.0)
        self.assertTrue(self.fresh(1000.0))

    def test_a_beat_before_the_request_is_not_fresh(self):
        self.beat(999.0)
        self.assertFalse(self.fresh(1000.0))

    def test_a_beat_from_another_session_is_not_the_cores(self):
        self.beat(1001.0, session="sutando-core-watcher")
        self.assertFalse(self.fresh(1000.0))

    def test_a_beat_on_another_socket_is_not_the_cores(self):
        self.beat(1001.0, socket="/tmp/other.sock")
        self.assertFalse(self.fresh(1000.0))

    def test_the_core_session_and_socket_follow_the_launch_env(self):
        env = {"SUTANDO_TMUX_SESSION": "core-b", "SUTANDO_TMUX_SOCKET": "/tmp/b.sock"}
        with mock.patch.dict(os.environ, env):
            self.beat(1001.0)
            self.assertFalse(self.fresh(1000.0))
            self.beat(1001.0, session="core-b", socket="/tmp/b.sock")
            self.assertTrue(self.fresh(1000.0))

    def test_unreadable_heartbeats_are_not_fresh(self):
        self.assertFalse(self.fresh(0.0))
        cores = self.ws / "state" / "cores"
        cores.mkdir(parents=True, exist_ok=True)
        for name, text in (("list", "[]"), ("broken", "{"), ("bad-time", json.dumps(
                {"session": "sutando-core", "socket": "/tmp/sutando-tmux.sock", "last_beat_at": "x"}))):
            with self.subTest(case=name):
                (cores / "h.alive").write_text(text)
                self.assertFalse(self.fresh(0.0))
        import util_paths
        with mock.patch.object(util_paths, "_host_label", side_effect=RuntimeError("x")):
            self.assertFalse(acc.fresh_beat(self.ws, 0.0))

    def test_core_ready_waits_for_a_fresh_beat_within_the_limit(self):
        import util_paths
        t = [1000.0]
        polls = []

        def sleep(_):
            t[0] += acc.READY_POLL_S
            polls.append(1)
            if len(polls) == 2:
                self.beat(1500.0)
        with mock.patch.object(util_paths, "_host_label", return_value="h"):
            self.assertTrue(acc.core_ready(self.ws, 1200.0, sleep=sleep, clock=lambda: t[0]))
            self.assertFalse(acc.core_ready(self.ws, 2000.0, sleep=sleep, clock=lambda: t[0]))

    def test_live_account_asks_codex_and_email_reads_only_the_claim(self):
        import codex_account
        with mock.patch.object(codex_account, "from_app_server", return_value=None):
            self.assertEqual(acc.live_account(self.home), {})
        self.assertEqual(acc._email(B), "bob@work.io")
        for bad in ("{", "[]", json.dumps({"tokens": {"id_token": {"raw_jwt": jwt({"email": "p@q.r"})}}})):
            self.assertIn(acc._email(bad), ("", "p@q.r"))

    def test_do_switch_refusals_before_anything_is_written(self):
        import codex_account
        with mock.patch.object(acc, "codex_home", side_effect=codex_account.NoHome("refused")):
            state, words = acc.do_switch(self.ws, key_of("acct-b"))
        self.assertIn("not usable", words)
        self.signed_in(A)
        self.keep(B)
        with mock.patch.object(acc, "save_current", return_value=False), \
                mock.patch.object(acc, "_saved_auth", side_effect=lambda st, k: B if k == key_of("acct-b") else None):
            state, words = acc.do_switch(self.ws, key_of("acct-b"), restart=lambda: 0)
        self.assertIn("could not be written", words)
        self.assertEqual(self.live(), A)

    def test_a_restore_that_fails_says_which_sign_in_the_core_will_use(self):
        self.signed_in(A)
        self.keep(B)
        with mock.patch.object(acc, "_restore", side_effect=OSError("ro")):
            state, words = acc.do_switch(self.ws, key_of("acct-b"), restart=lambda: 4)
        self.assertIn("could not be put back", words)

    def test_the_outcome_is_posted_again_until_it_lands(self):
        self.signed_in(A)
        self.keep(B)
        self.post()
        self.tap("Use b…@work.io (pro)")
        argv = self.launch.calls[0][0]
        attempt = argv[argv.index("--attempt") + 1]
        posts = iter([{"status": "failed"}, {"status": "posted", "event_id": "$x"}])
        slept = []
        with mock.patch.object(acc, "_post", side_effect=lambda *a, **k: next(posts)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                acc.main(["--workspace", str(self.ws), "switch", "--room", DM, "--key", key_of("acct-b"),
                          "--attempt", attempt], restart=lambda: 0, sleep=slept.append, ready=self.ready,
                         live=self.live_account)
        self.assertEqual(slept, [acc.SETTLE_PAUSE_S])

    def test_main_resolves_the_workspace(self):
        import workspace_default
        with mock.patch.object(workspace_default, "resolve_workspace", return_value=self.ws):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                acc.main(["status"], codex_read=codex_read_from(self))
        self.assertIn("words", json.loads(out.getvalue()))


_real_runtime, _real_codex_home, _real_owner_dm = acc.runtime, acc.codex_home, acc._owner_dm


class Identity(unittest.TestCase):
    def test_key_and_label_follow_the_desktop_rule(self):
        self.assertEqual(acc.identity(A), (key_of("acct-a"), "alice@example.com (plus)"))
        no_id = json.dumps({"tokens": {"id_token": jwt({"sub": "user-1", "email": "x@y.z"})}})
        self.assertEqual(acc.identity(no_id), (key_of("user-1"), "x@y.z"))
        for bad in ("", "{", json.dumps({"OPENAI_API_KEY": "sk"}), json.dumps({"tokens": {}})):
            self.assertIsNone(acc.identity(bad))

    def test_masking(self):
        self.assertEqual(acc.masked_label("alice@example.com (plus)"), "a…@example.com (plus)")
        self.assertEqual(acc.masked_label("ChatGPT account (pro)"), "ChatGPT account (pro)")


if __name__ == "__main__":
    unittest.main()
