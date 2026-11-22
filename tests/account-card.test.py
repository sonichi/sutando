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

    def run_(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = acc.main(["--workspace", str(self.ws)] + argv, core=CORE, popen=self.launch, run=self.claude_run,
                          execute=lambda *a, **k: self.fake.execute(*a, **k), codex_read=codex_read_from(self),
                          restart=self.restart, sleep=lambda s: None)
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

    def test_switched_restarted_and_confirmed(self):
        rc, out = self.switch()
        self.assertEqual((rc, out["outcome"]), (0, "done"))
        self.assertEqual(self.restarts, [B], "the saved sign-in is in place before the restart")
        self.assertEqual(self.live(), B)
        self.assertEqual(stat.S_IMODE((self.home / "auth.json").stat().st_mode), 0o600)
        self.assertTrue((self.store / f"{key_of('acct-a')}.json").exists(), "the previous account stays saved")
        self.assertEqual(out["say"], "Switched. The core restarted and is signed in as b…@work.io (pro).")
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
        self.assertIn("its sign-in is a…@example.com (plus), not b…@work.io (pro)", out["say"])

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
