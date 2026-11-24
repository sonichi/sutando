#!/usr/bin/env python3
"""The core's runtime card (skills/runtime-switch/scripts/runtime_card.py): card, tap, switch, outcome.

- The card has "Codex" and "Claude Code"; the runtime in use is the primary button and named in the text.
- Only the owner's tap in the DM on a card this agent posted acts. A tap on the runtime in use, a second
  switch under way, or a stale choice starts nothing.
- Before anything stops, the target runtime's sign-in is probed (Claude Code: `claude auth status`;
  Codex: the core's Codex home). Not signed in or unreadable: "Sign in to <runtime> first", pointing
  to the desktop's Settings; nothing is launched.
- Signed in: "switching" is posted and `switch` starts detached (own session, without the old core's
  SUTANDO_CORE_RUNTIME).
- `switch` runs graceful-restart with `-- --runtime <rt>` and confirms from state/core-runtime.json
  (target runtime, session newer than the switch) plus the launcher's next pick, never a heartbeat:
  "Switched", a selection that still names the old runtime, or "could not confirm". graceful-restart
  exits 3/4/5 say the core is still on the old runtime.
- One card, updated in place: "switching" and the outcome edit the tapped card's event (the "Sign in
  first" card's, after a sign-in wait); a card that cannot be edited is answered with a card, as before.

The Action call, the core's row, the restart and the process launch are fakes; no network, no tmux.

Run: python3 tests/runtime-card.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import base64
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "runtime-switch" / "scripts" / "runtime_card.py"
_spec = importlib.util.spec_from_file_location("runtime_card", SCRIPT)
rtc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rtc)
ac = rtc.ac

import account_card  # noqa: E402  (on sys.path through runtime_card)
import connections  # noqa: E402

DM, ROOM, OWNER = "!dm:s.space", "!team:s.space", "@owner:s.space"
CORE = {"instance": "s_dspace", "owner_mxid": OWNER, "agent_mxid": "@owner-mini.agent:s.space"}
T0 = 1_800_000_000.0


def jwt(claims: dict) -> str:
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


CODEX_AUTH = json.dumps({"tokens": {"id_token": jwt({"email": "alice@example.com"}), "access_token": "tok-SECRET",
                                    "account_id": "acct-a"}})


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
        return [c[1]["extra_content"][ac.CARD_KEY] for c in self.calls if c[0] == "room.message.send"]

    def edits(self):
        """(edited event, words) for every edit, in order."""
        return [(c[1]["event_id"], c[1]["body"]) for c in self.calls if c[0] == "room.message.edit"]


class Launch:
    def __init__(self):
        self.calls = []

    fail = None

    def __call__(self, argv, **kw):
        if self.fail:
            raise self.fail
        self.calls.append((argv, kw))
        return mock.Mock(pid=4242)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Base(unittest.TestCase):
    RUNTIME = "codex"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.ws, self.home = root / "ws", root / "codex-home"
        self.ws.mkdir()
        self.home.mkdir()
        self.fake, self.launch, self.clock = Fake(), Launch(), Clock()
        self.current, self.selected, self.desktop = self.RUNTIME, self.RUNTIME, True
        self.claude_json = {"loggedIn": True, "email": "susan@example.com", "subscriptionType": "max"}
        self.restarts, self.restart_rc, self.launcher_confirms, self.launcher_selects = [], 0, True, True
        self.outputs = []
        for target, attr, value in ((rtc, "_owner_dm", lambda ws, core: DM),
                                    (rtc, "runtime", lambda: self.current),
                                    (rtc, "configured_runtime", lambda: self.selected),
                                    (rtc, "desktop_hosted", lambda: self.desktop),
                                    (account_card, "codex_home", lambda: self.home),
                                    (connections, "read_rows", lambda ws=None: [CORE])):
            patch = mock.patch.object(target, attr, value)
            patch.start()
            self.addCleanup(patch.stop)

    # fakes ------------------------------------------------------------------------------------------
    def claude_run(self, argv, **kw):
        self.assertEqual(argv[:3], ["claude", "auth", "status"])
        out = self.claude_json if isinstance(self.claude_json, str) else json.dumps(self.claude_json)
        return mock.Mock(stdout=out, returncode=0)

    def codex_read(self, home):
        import codex_account
        return codex_account.from_auth_file(home)

    def restart(self, rt):
        """graceful-restart + launcher: on success the launcher records the confirmed core and selection."""
        self.restarts.append(rt)
        self.clock.t += 30
        if self.restart_rc not in rtc.NOT_RESTARTED:
            if self.launcher_confirms:
                self.record(rt, self.clock.t - 5)
                self.current = rt
            if self.launcher_selects:
                self.selected = rt
        return self.restart_rc

    def record(self, rt, started):
        path = self.ws / "state" / "core-runtime.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"runtime": rt, "session": "sutando-core", "started_at": started}))

    def run_(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rtc.main(["--workspace", str(self.ws)] + argv, core=CORE, popen=self.launch, run=self.claude_run,
                          execute=lambda *a, **k: self.fake.execute(*a, **k), codex_read=self.codex_read,
                          restart=self.restart, sleep=self.clock.sleep, clock=self.clock)
        self.outputs.append(out.getvalue())
        return rc, json.loads(out.getvalue())

    def post(self):
        return self.run_(["post", "--room", DM])

    def run_with(self, **over):
        """A tap on Claude Code with one of the injected fakes replaced."""
        path = self.ws / "task.txt"
        path.write_text(task("Switch the core to Claude Code", reply_to_event="$card1",
                             card_click=json.dumps({"card_id": rtc.CARD_ID, "action": "Switch the core to Claude Code"})))
        out = io.StringIO()
        kw = dict(core=CORE, popen=self.launch, run=self.claude_run, codex_read=self.codex_read,
                  execute=lambda *a, **k: self.fake.execute(*a, **k), restart=self.restart,
                  sleep=self.clock.sleep, clock=self.clock)
        kw.update(over)
        with contextlib.redirect_stdout(out):
            rc = rtc.main(["--workspace", str(self.ws), "reply", "--task-file", str(path)], **kw)
        self.outputs.append(out.getvalue())
        return rc, json.loads(out.getvalue())

    def tap(self, label, *, event="$card1", card_id=rtc.CARD_ID, **kw):
        kw.setdefault("reply_to_event", event)
        kw.setdefault("card_click", json.dumps({"card_id": card_id, "action": label}))
        path = self.ws / "task.txt"
        path.write_text(task(label, **kw))
        return self.run_(["reply", "--task-file", str(path)])

    def tearDown(self):
        self.assertNotIn("tok-SECRET", "".join(self.outputs) + json.dumps([c[1] for c in self.fake.calls]))


class Card(Base):
    def test_the_card_marks_the_runtime_in_use(self):
        rc, out = self.post()
        self.assertEqual((rc, out["status"]), (0, "posted"))
        card = self.fake.cards()[0]
        self.assertIsNone(ac.problem(card))
        self.assertEqual(card["id"], rtc.CARD_ID)
        self.assertIn("running on Codex", card["text"])
        self.assertEqual([(b["label"], b["style"]) for b in card["buttons"]],
                         [("Codex", "primary"), ("Claude Code", "secondary")])
        self.assertEqual([b["command"] for b in card["buttons"]], ["card:runtime:core:codex", "card:runtime:core:claude"])
        self.assertIn("conversation does not", card["note"])
        self.assertEqual(self.fake.calls[0][3], DM)

    def test_a_claude_core_marks_claude_code(self):
        self.current = "claude"
        self.post()
        card = self.fake.cards()[0]
        self.assertIn("running on Claude Code", card["text"])
        self.assertEqual([b["style"] for b in card["buttons"]], ["secondary", "primary"])

    def test_the_card_goes_only_to_the_owner_dm(self):
        rc, out = self.run_(["post", "--room", ROOM])
        self.assertEqual((rc, out["status"]), (1, "failed"))
        self.assertEqual(self.fake.calls, [])

    def test_print_sends_nothing(self):
        rc, out = self.run_(["post", "--room", DM, "--print"])
        self.assertEqual((rc, out["handler"]), (0, rtc.HANDLER))
        self.assertEqual(self.fake.calls, [])

    def test_status(self):
        self.record("codex", T0)
        rc, out = self.run_(["status"])
        self.assertEqual((rc, out["runtime"], out["next_start"], out["confirmed_core"]["runtime"]),
                         (0, "codex", "codex", "codex"))


class Taps(Base):
    def setUp(self):
        super().setUp()
        self.post()

    def test_a_signed_in_target_starts_the_detached_switch(self):
        with mock.patch.dict(os.environ, {"SUTANDO_CORE_RUNTIME": "codex"}):
            rc, out = self.tap("Switch the core to Claude Code")
        self.assertEqual((rc, out["status"], out["runtime"]), (0, "started", "claude"))
        argv, kw = self.launch.calls[0]
        self.assertIn("switch", argv)
        self.assertEqual(argv[argv.index("--runtime") + 1], "claude")
        self.assertEqual(argv[argv.index("--room") + 1], DM)
        self.assertTrue(kw["start_new_session"])
        self.assertNotIn("SUTANDO_CORE_RUNTIME", kw["env"], "the old core's runtime must not ride along")
        self.assertIn("Switching the core from Codex to Claude Code", out["say"])
        # One card, updated in place: the tapped card's own event says it, no message is added, and the
        # detached switch is handed that event for its outcome.
        self.assertEqual(self.fake.edits(), [("$card1", "Core runtime: " + out["say"])])
        self.assertEqual((len(self.fake.cards()), out["event_id"]), (1, "$card1"))
        self.assertEqual(argv[argv.index("--card") + 1], "$card1")
        self.assertEqual(self.restarts, [], "nothing is restarted in the tap itself")

    def test_a_card_that_cannot_be_edited_is_answered_with_a_card_as_before(self):
        real = self.fake.execute
        self.fake.execute = lambda action, arguments, **kw: ({"is_error": True, "code": "M_FORBIDDEN"}
                                                             if action == "room.message.edit" else real(action, arguments, **kw))
        rc, out = self.tap("Switch the core to Claude Code")
        self.assertEqual((rc, out["status"], out["event_id"]), (0, "started", "$card2"))
        self.assertEqual((self.fake.cards()[-1]["id"], self.fake.cards()[-1]["state"]), (rtc.NOTE_ID, "pending"))
        self.assertEqual(self.fake.calls[-1][1]["reply_to"], "$card1")
        self.assertNotIn("--card", self.launch.calls[0][0], "its outcome is then posted as a card too")

    def test_a_signed_out_target_gets_the_sign_in_controls_in_the_card_and_restarts_nothing(self):
        for answer, why in (({"loggedIn": False}, "is not signed in"), ("nonsense", "could not confirm")):
            with self.subTest(why=why):
                self.claude_json = answer
                rc, out = self.tap("Switch the core to Claude Code")
                self.assertEqual((rc, out["status"]), (1, "needs_sign_in"))
                self.assertIn(why, out["say"])
                self.assertIn("Sign in to Claude Code below.", out["say"])
                self.assertIn("you don't need to ask again", out["say"])
                self.assertNotIn("Settings", out["say"], "the card acts in place: it sends nobody elsewhere")
                card = self.fake.cards()[-1]
                self.assertEqual((card["id"], card["state"], card["header"]["status"]["label"]), (rtc.NOTE_ID, "pending", "Sign in first"))
                # The marker the desktop app draws that runtime's sign-in controls from, on the core's computer.
                self.assertEqual(card[rtc.CONTROLS], {"runtime": "claude", "core": True})
                # Anywhere else the client shows the body: one line, where to open it.
                self.assertEqual(self.fake.calls[-1][1]["body"],
                                 "Open this on the computer that runs the core to sign in to Claude Code.")
        self.assertFalse(self.ws.joinpath(*rtc.RUNNING).exists(), "no switch is under way yet")
        self.assertEqual(self.restarts, [])

    def test_the_tap_is_kept_and_a_waiter_is_started_for_it(self):
        self.claude_json = {"loggedIn": False}
        rc, out = self.tap("Switch the core to Claude Code")
        waiting = json.loads(self.ws.joinpath(*rtc.WAITING).read_text())
        self.assertEqual((waiting["runtime"], waiting["room"], waiting["attempt"]), ("claude", DM, out["waiting"]))
        argv, kw = self.launch.calls[0]
        self.assertIn("await-sign-in", argv)
        self.assertEqual((argv[argv.index("--runtime") + 1], argv[argv.index("--attempt") + 1]), ("claude", out["waiting"]))
        self.assertTrue(kw["start_new_session"])
        # A second tap replaces the first: one wait is named, and the earlier waiter will find it is not its.
        rc, again = self.tap("Switch the core to Claude Code")
        self.assertNotEqual(again["waiting"], out["waiting"])
        self.assertEqual(json.loads(self.ws.joinpath(*rtc.WAITING).read_text())["attempt"], again["waiting"])
        self.assertEqual(len(self.launch.calls), 2)

    def test_a_waiter_that_cannot_be_started_promises_nothing_and_clears_only_its_own_attempt(self):
        self.claude_json = {"loggedIn": False}
        self.launch.fail = OSError("no nohup")
        rc, out = self.tap("Switch the core to Claude Code")
        self.assertEqual((rc, out["status"], out["waiting"]), (1, "needs_sign_in", ""))
        self.assertIn("ask again", out["why"])
        # The card in the room says what is true: sign in, then ask again. No automatic switch is promised.
        card = self.fake.cards()[-1]
        self.assertIn("then ask me to switch again", card["text"])
        self.assertNotIn("you don't need to ask again", card["text"])
        self.assertEqual(card[rtc.CONTROLS], {"runtime": "claude", "core": True}, "the sign-in is still in the card")
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists(), "its own attempt is cleared")
        # A newer tap's wait, written while this one was failing to start, is not the one cleared.
        newer = {"attempt": "9-newer", "runtime": "claude", "room": DM, "ts": self.clock.t}

        def fails_after_a_newer_tap(argv, **kw):
            rtc.set_wait(self.ws, newer)
            raise OSError("no nohup")
        self.launch.fail = None
        rc, out = self.run_with(popen=fails_after_a_newer_tap)
        self.assertEqual(out["waiting"], "")
        self.assertEqual(json.loads(self.ws.joinpath(*rtc.WAITING).read_text()), newer)

    def test_codex_target_sign_in_is_probed_on_the_codex_home(self):
        self.current = self.selected = "claude"
        rc, out = self.tap("Switch the core to Codex")
        self.assertEqual(out["status"], "needs_sign_in")
        self.assertIn("Sign in to Codex below", out["say"])
        self.assertEqual(self.fake.cards()[-1][rtc.CONTROLS], {"runtime": "codex", "core": True})
        (self.home / "auth.json").write_text(CODEX_AUTH)
        rc, out = self.tap("Switch the core to Codex")
        self.assertEqual((rc, out["status"]), (0, "started"))
        self.assertEqual([c[0][c[0].index("--workspace") + 2] for c in self.launch.calls], ["await-sign-in", "switch"])
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists(), "a tap that goes ahead ends the wait")

    def test_the_runtime_in_use_changes_nothing(self):
        rc, out = self.tap("Switch the core to Codex")
        self.assertEqual(out["status"], "say")
        self.assertIn("already running on Codex", out["say"])
        self.assertEqual(self.launch.calls, [])

    def test_someone_else_or_another_room_starts_nothing(self):
        for kw in ({"user": "@guest:s.space", "tier": "guest"}, {"kind": "group", "room": ROOM}):
            rc, out = self.tap("Switch the core to Claude Code", **kw)
            self.assertEqual((rc, out["status"]), (1, ac.FORGED), kw)
        self.assertEqual(self.launch.calls, [])

    def test_typed_button_text_without_a_tap_starts_nothing(self):
        rc, out = self.tap("Switch the core to Claude Code", card_click="")
        self.assertEqual(out["status"], ac.FORGED)
        self.assertEqual(self.launch.calls, [])

    def test_a_switch_that_cannot_be_recorded_or_started_changes_nothing_and_leaves_no_record(self):
        with mock.patch.object(rtc, "admit", side_effect=OSError("read-only")):
            rc, out = self.tap("Switch the core to Claude Code")
        self.assertEqual((rc, out["status"], out["why"]), (1, "failed", "OSError"))
        self.assertIn("Nothing changed", out["say"])
        self.assertEqual(self.launch.calls, [])
        self.launch.fail = OSError("no nohup")
        rc, out = self.tap("Switch the core to Claude Code")
        self.assertEqual((rc, out["status"]), (1, "failed"))
        self.assertIn("no nohup", out["why"])
        self.assertFalse(self.ws.joinpath(*rtc.RUNNING).exists(), "the attempt is released: a later tap can go ahead")
        self.launch.fail = None
        rc, out = self.tap("Switch the core to Claude Code")
        self.assertEqual((rc, out["status"]), (0, "started"))

    def test_a_second_switch_while_one_runs_is_refused(self):
        self.tap("Switch the core to Claude Code")
        rc, out = self.tap("Switch the core to Claude Code")
        self.assertEqual(out["say"], rtc.BUSY)
        self.assertEqual(len(self.launch.calls), 1)



class AwaitSignIn(Base):
    """The waiter a "Sign in first" card starts: it switches as the tap wanted once the sign-in is there."""

    def setUp(self):
        super().setUp()
        self.post()
        self.claude_json = {"loggedIn": False}
        rc, out = self.tap("Switch the core to Claude Code")
        self.attempt = out["waiting"]
        self.launch.calls.clear()

    def wait(self, attempt=None, runtime="claude"):
        return self.run_(["await-sign-in", "--room", DM, "--runtime", runtime, "--attempt", attempt or self.attempt])

    def test_signed_in_after_a_while_it_switches_without_being_asked_again(self):
        polls = []
        real = self.clock.sleep

        def sleep(s):
            polls.append(s)
            real(s)
            if len(polls) == 3:
                self.claude_json = {"loggedIn": True, "email": "susan@example.com"}
        self.clock.sleep = sleep
        rc, out = self.wait()
        self.assertEqual((rc, out["status"], out["runtime"], out["after_sign_in"]), (0, "started", "claude", True))
        self.assertEqual(polls, [rtc.SIGN_IN_POLL_S] * 3)
        argv = self.launch.calls[0][0]
        self.assertIn("switch", argv)
        self.assertEqual((argv[argv.index("--runtime") + 1], argv[argv.index("--room") + 1]), ("claude", DM))
        # The "Sign in first" card ($card2) is edited in place, and handed on for the outcome.
        self.assertEqual(self.fake.edits(), [("$card2", "Core runtime: " + out["say"])])
        self.assertEqual((len(self.fake.cards()), argv[argv.index("--card") + 1]), (2, "$card2"))
        self.assertIn("Switching the core from Codex to Claude Code", out["say"])
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())
        self.assertTrue(self.ws.joinpath(*rtc.RUNNING).exists(), "the one switch under way")
        self.assertEqual(self.restarts, [], "the waiter restarts nothing itself")

    def test_a_waiter_holds_its_lease_while_it_runs_and_a_second_one_for_the_attempt_stops(self):
        seen = []
        real = self.clock.sleep

        def sleep(s):
            if not seen:
                seen.append(rtc.waiter_alive(self.ws, self.attempt))
                rc, dup = self.wait()                                   # started again while the first runs
                seen.append((rc, dup["status"]))
                seen.append(self.run_(["resume"])[1]["status"])
            real(s)
        self.clock.sleep = sleep
        self.assertFalse(rtc.waiter_alive(self.ws, self.attempt), "nothing runs yet")
        rc, out = self.wait()
        self.assertEqual(seen, [True, (0, "duplicate"), "waiting"])
        self.assertEqual(out["status"], "gave_up", "the first went on to its own end")
        self.assertEqual(self.launch.calls, [], "neither the duplicate nor the resume started anything")
        self.assertFalse(rtc.waiter_alive(self.ws, self.attempt))
        self.assertEqual(list(self.ws.joinpath("state").glob("runtime-card-signin.*.alive")), [], "its lease is gone")
        self.assertEqual(self.wait(attempt="../x")[0], 2, "an attempt id is all a lease file is ever named by")

    def test_a_wait_whose_waiter_is_gone_is_given_a_new_one_by_resume_and_by_posting_the_card(self):
        # The waiter died with a core restart: the record is there, nobody holds its lease.
        rc, out = self.run_(["resume"])
        self.assertEqual((rc, out["status"], out["attempt"]), (0, "resumed", self.attempt))
        argv, kw = self.launch.calls[0]
        self.assertEqual((argv[argv.index("--workspace") + 2], argv[argv.index("--attempt") + 1],
                          argv[argv.index("--runtime") + 1], argv[argv.index("--room") + 1], kw["start_new_session"]),
                         ("await-sign-in", self.attempt, "claude", DM, True))
        # Asking for the card, or its status, brings it back too.
        self.launch.calls.clear()
        self.post()
        self.run_(["status"])
        self.assertEqual([c[0][c[0].index("--attempt") + 1] for c in self.launch.calls], [self.attempt] * 2)
        # Resumed past the deadline, the waiter says it stopped waiting on the card that promised the switch.
        self.clock.sleep(rtc.SIGN_IN_WAIT_S + 1)
        self.claude_json = {"loggedIn": True}
        rc, late = self.wait()
        self.assertEqual((late["status"], self.restarts), ("gave_up", []))
        self.assertEqual(self.fake.edits()[-1][0], "$card2")
        # Nothing waited for, a record that names no attempt, and a launch that fails.
        self.launch.calls.clear()
        self.assertEqual(self.run_(["resume"])[1], {"status": "none"})
        self.ws.joinpath(*rtc.WAITING).write_text(json.dumps({"attempt": "../../x", "runtime": "claude", "room": DM}))
        self.assertEqual((self.run_(["resume"])[1], self.launch.calls), ({"status": "none"}, []))
        self.ws.joinpath(*rtc.WAITING).write_text(json.dumps({"attempt": "5-ab", "runtime": "claude", "room": DM, "ts": 1}))
        self.launch.fail = OSError("no fork")
        self.assertEqual(self.run_(["resume"])[1], {"status": "failed", "attempt": "5-ab", "why": "OSError"})
        rc, out = self.run_(["status"])
        self.assertEqual((rc, out["runtime"]), (0, "codex"), "a resume that fails is never in the card's way")

    def test_no_sign_in_within_the_wait_says_so_and_changes_nothing(self):
        rc, out = self.wait()
        self.assertEqual((rc, out["status"], out["posted"]), (1, "gave_up", "posted"))
        self.assertIn("I stopped waiting for the Claude Code sign-in after 15 minutes. The core is still on Codex", out["say"])
        self.assertEqual(self.fake.edits(), [("$card2", "Core runtime: " + out["say"])])
        self.assertEqual(len(self.fake.cards()), 2, "no message is added")
        self.assertEqual((self.launch.calls, self.restarts), ([], []))
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())

    def test_a_newer_tap_or_another_attempt_makes_it_stop_without_a_word(self):
        cards = len(self.fake.cards())
        rc, out = self.wait(attempt="1-deadbeef")
        self.assertEqual((rc, out["status"]), (0, "superseded"))
        self.tap("Switch the core to Claude Code")            # a newer tap: the record names it now
        self.launch.calls.clear()
        cards = len(self.fake.cards())
        self.claude_json = {"loggedIn": True}
        rc, out = self.wait()
        self.assertEqual(out["status"], "superseded")
        self.assertEqual((len(self.fake.cards()), self.launch.calls), (cards, []))
        self.assertTrue(self.ws.joinpath(*rtc.WAITING).exists(), "the newer tap's wait is left alone")

    def test_a_wait_replaced_while_the_probe_ran_neither_switches_nor_speaks_nor_removes_its_replacement(self):
        newer = {"attempt": "9-newer", "runtime": "claude", "room": DM, "ts": self.clock.t}
        cards = len(self.fake.cards())

        def probe(argv, **kw):
            # A newer tap lands while this waiter is still asking whether Claude Code is signed in.
            rtc.set_wait(self.ws, newer)
            return mock.Mock(stdout=json.dumps({"loggedIn": True}), returncode=0)
        self.claude_run = probe
        rc, out = self.wait()
        self.assertEqual((rc, out["status"]), (0, "superseded"))
        self.assertEqual((self.launch.calls, self.restarts, len(self.fake.cards())), ([], [], cards))
        self.assertFalse(self.ws.joinpath(*rtc.RUNNING).exists(), "no switch was admitted")
        self.assertEqual(json.loads(self.ws.joinpath(*rtc.WAITING).read_text()), newer, "the newer tap's wait stands")

        # The same at the deadline: replaced while probing, it posts no expiry notice.
        self.attempt = "9-newer"

        def slow_probe(argv, **kw):
            self.clock.t += rtc.SIGN_IN_WAIT_S + 1
            rtc.set_wait(self.ws, dict(newer, attempt="9-newest"))
            return mock.Mock(stdout=json.dumps({"loggedIn": False}), returncode=0)
        self.claude_run = slow_probe
        rc, out = self.wait()
        self.assertEqual((out["status"], len(self.fake.cards())), ("superseded", cards))
        self.assertEqual(json.loads(self.ws.joinpath(*rtc.WAITING).read_text())["attempt"], "9-newest")

    def test_a_sign_in_seen_only_after_the_waits_own_deadline_switches_nothing(self):
        def late(argv, **kw):
            self.clock.t += rtc.SIGN_IN_WAIT_S + 5         # the probe itself outlasted the wait
            return mock.Mock(stdout=json.dumps({"loggedIn": True}), returncode=0)
        self.claude_run = late
        rc, out = self.wait()
        self.assertEqual((rc, out["status"]), (1, "gave_up"))
        self.assertEqual((self.launch.calls, self.restarts), ([], []))
        self.assertFalse(self.ws.joinpath(*rtc.RUNNING).exists())

    def test_the_deadline_is_the_waits_own_not_the_waiters(self):
        # A waiter started late (or again) does not get a fresh fifteen minutes: the wait's saved time counts.
        self.clock.t += rtc.SIGN_IN_WAIT_S - 2
        polls = []
        real = self.clock.sleep

        def sleep(sec):
            polls.append(sec)
            real(sec)
        self.clock.sleep = sleep
        rc, out = self.wait()
        self.assertEqual((out["status"], len(polls)), ("gave_up", 1))

    def test_the_sign_in_card_is_kept_with_the_wait(self):
        self.assertEqual(json.loads(self.ws.joinpath(*rtc.WAITING).read_text())["card"], "$card2")

    def test_a_core_already_on_that_runtime_ends_the_wait(self):
        self.current = "claude"
        rc, out = self.wait()
        self.assertEqual((rc, out["status"], out["edited"]), (0, "already", "posted"))
        # Switched some other way: the "Sign in first" card says so in place, and nothing is added.
        self.assertEqual(self.fake.edits(), [("$card2", "Core runtime: The core is now running on Claude Code. "
                                                        + rtc.CARRIES)])
        self.assertEqual(len(self.fake.cards()), 2)
        self.assertEqual(self.launch.calls, [])
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())

    def test_a_switch_already_under_way_is_not_started_twice(self):
        self.claude_json = {"loggedIn": True}
        # Another switch was admitted meanwhile (a tap that went ahead): the waiter starts no second one.
        self.assertTrue(rtc.admit(self.ws, {"attempt": "9-cafe", "runtime": "claude", "room": DM, "ts": self.clock.t},
                                  self.clock.t))
        rc, out = self.wait()
        self.assertEqual((rc, out["status"], out["say"]), (1, "say", rtc.BUSY))
        self.assertEqual(self.launch.calls, [])


class Switch(Base):
    def setUp(self):
        super().setUp()
        self.post()
        self.tap("Switch the core to Claude Code")
        argv = self.launch.calls[0][0]
        self.attempt = argv[argv.index("--attempt") + 1]

    def switch(self, attempt=None, runtime="claude"):
        return self.run_(["switch", "--room", DM, "--runtime", runtime, "--attempt", attempt or self.attempt])

    def test_switched_and_confirmed_from_record_and_selection(self):
        rc, out = self.switch()
        self.assertEqual((rc, out["outcome"]), (0, "done"))
        self.assertEqual(self.restarts, ["claude"])
        self.assertEqual(out["say"], "Switched. The core is now running on Claude Code. " + rtc.CARRIES)
        self.assertTrue(out["core_confirmed"] and out["selection_confirmed"])
        last = self.fake.cards()[-1]
        self.assertEqual((last["id"], last["state"]), (rtc.NOTE_ID, "done"))
        self.assertFalse(self.ws.joinpath(*rtc.RUNNING).exists())

    def test_a_stale_record_from_before_the_switch_is_not_confirmation(self):
        self.record("claude", T0 - 3600)          # an old Claude session
        self.launcher_confirms = False
        rc, out = self.switch()
        self.assertEqual((rc, out["outcome"]), (1, "error"))
        self.assertIn("could not confirm it is running on Claude Code", out["say"])

    def test_no_record_at_all_is_not_confirmation(self):
        self.launcher_confirms = self.launcher_selects = False
        rc, out = self.switch()
        self.assertEqual(out["outcome"], "error")
        self.assertIn("no core has been confirmed since", out["say"])

    def test_a_selection_still_naming_the_old_runtime_is_said(self):
        self.launcher_selects = False
        rc, out = self.switch()
        self.assertEqual((rc, out["outcome"]), (1, "info"))
        self.assertIn("the desktop still selects Codex", out["say"])

    def test_without_a_desktop_the_config_caveat_is_said(self):
        self.launcher_selects, self.desktop = False, False
        rc, out = self.switch()
        self.assertEqual(out["outcome"], "info")
        self.assertIn("config still names Codex", out["say"])

    def test_a_restart_that_did_not_happen_says_still_on_the_old_runtime(self):
        self.record("codex", T0 - 500)
        self.ws.joinpath(*rtc.RESTART_RESULT).write_text(json.dumps(
            {"rid": "grp-9-9", "runtime": "codex", "published": True, "at": T0 + 10 ** 6}))
        for code in rtc.NOT_RESTARTED:
            with self.subTest(rc=code):
                self.restart_rc = code
                rc, out = self.switch()
                self.assertEqual((rc, out["outcome"], out["restart"]), (1, "error", "not-restarted"))
                self.assertIn("The core is still on Codex", out["say"])
                self.ws.joinpath(*rtc.RUNNING).write_text(json.dumps(
                    {"attempt": self.attempt, "runtime": "claude", "room": DM, "ts": 1e12}))

    def carded(self):
        return self.run_(["switch", "--room", DM, "--runtime", "claude", "--attempt", self.attempt, "--card", "$card1"])

    def held(self, rid="grp-1-77"):
        """Another restart holds graceful-restart.sh's lock, under its id."""
        lock = self.ws.joinpath(*rtc.RESTART_LOCK)
        lock.mkdir(parents=True, exist_ok=True)
        lock.joinpath("rid").write_text(rid)
        return lock

    def ends(self, lock, runtime, published=True, rid="grp-1-77", record=None):
        """That restart's launcher: the record (when it can publish), its result under its id, then the lock goes."""
        if published:
            self.record(runtime, T0 - 60 if record is None else record)     # its session predates this card's request
        self.ws.joinpath(*rtc.RESTART_RESULT).write_text(json.dumps(
            {"rid": rid, "runtime": runtime, "published": published, "at": int(self.clock.t)}))
        lock.joinpath("rid").unlink(missing_ok=True)
        lock.rmdir()

    def test_a_restart_put_off_waits_for_the_other_restart_then_switches_on_the_same_card(self):
        lock = self.held()
        self.restart_rc = rtc.DEFERRED
        polls, real, first = [], self.clock.sleep, self.restart

        def restart(rt):
            code = first(rt)
            self.restart_rc = 0             # the next one is not in anybody's way
            return code
        self.restart = restart

        def sleep(s):
            polls.append(s)
            real(s)
            if len(polls) == 3:
                self.ends(lock, "codex")    # the other restart ended: the core is confirmed on Codex still
        self.clock.sleep = sleep
        rc, out = self.carded()
        self.assertEqual((rc, out["outcome"], out["event_id"]), (0, "done", "$card1"))
        self.assertEqual((self.restarts, polls[:3]), (["claude", "claude"], [rtc.DEFER_POLL_S] * 3))
        said = [words for event, words in self.fake.edits() if event == "$card1"]
        self.assertIn("Waiting for another restart to finish, then I'll switch to Claude Code. The core is still on Codex.",
                      said[-2])
        self.assertIn("Switched. The core is now running on Claude Code", said[-1])
        self.assertEqual(len(self.fake.cards()), 1, "one card throughout")
        self.assertFalse(self.ws.joinpath(*rtc.RUNNING).exists())

    def test_the_other_restart_already_made_the_switch_so_nothing_is_restarted_again(self):
        lock = self.held()
        self.restart_rc = rtc.DEFERRED
        real = self.clock.sleep

        def sleep(s):
            real(s)
            if lock.exists():
                # The restart that was in the way was itself a switch to Claude Code (the desktop's, say).
                self.current = self.selected = "claude"
                self.ends(lock, "claude")
        self.clock.sleep = sleep
        rc, out = self.carded()
        self.assertEqual((rc, out["outcome"], out["restart"], out["rc"]), (0, "done", "requested", 0))
        self.assertEqual(self.restarts, ["claude"], "the confirmed core is not restarted a second time")
        said = [words for event, words in self.fake.edits() if event == "$card1"]
        self.assertIn("Waiting for another restart to finish", said[-2])
        self.assertIn("Switched. The core is now running on Claude Code", said[-1])

    def test_what_the_other_restart_did_not_publish_is_not_known_and_nothing_is_restarted_blindly(self):
        def claude_came_up_unpublished(lock):
            self.ends(lock, "claude", published=False)      # Claude started; the record still says Codex

        def stale_target_record(lock):
            self.record("claude", T0 - 9000)                # an old record names the target; no result at all
            lock.joinpath("rid").unlink()
            lock.rmdir()

        def another_restarts_result(lock):
            self.ends(lock, "codex", rid="grp-0-11")        # a result, but not of the restart that held the lock

        def unreadable(lock):
            self.ws.joinpath(*rtc.RECORD).write_text("{not json")
            self.ws.joinpath(*rtc.RESTART_RESULT).write_text("{not json")
            lock.joinpath("rid").unlink()
            lock.rmdir()

        for leaves in (claude_came_up_unpublished, stale_target_record, another_restarts_result, unreadable):
            with self.subTest(leaves=leaves.__name__):
                self.restarts.clear()
                self.ws.joinpath(*rtc.RESTART_RESULT).unlink(missing_ok=True)
                self.record("codex", T0 - 500)              # a valid record from before, left unchanged
                self.restart_rc = rtc.DEFERRED
                lock, real = self.held(), self.clock.sleep

                def sleep(s, lock=lock, leaves=leaves, real=real):
                    real(s)
                    if lock.exists():
                        leaves(lock)
                self.clock.sleep = sleep
                rc, out = self.carded()
                self.clock.sleep = real
                self.assertEqual((rc, out["outcome"], out["restart"], out["why"]), (1, "error", "not-restarted", "inconclusive"))
                self.assertEqual(self.restarts, ["claude"], "not restarted blindly")
                self.assertIn("I could not read which runtime the core is on now, so I did not restart it again", out["say"])
                self.assertEqual(self.fake.edits()[-1], ("$card1", "Core runtime: " + out["say"]))
                self.assertFalse(self.ws.joinpath(*rtc.RUNNING).exists(), "released: she can ask again")
                self.ws.joinpath(*rtc.RUNNING).write_text(json.dumps(
                    {"attempt": self.attempt, "runtime": "claude", "room": DM, "ts": 1e12}))

    def test_a_restart_still_in_the_way_after_the_wait_is_not_asked_again_and_the_card_says_so(self):
        self.held()
        self.restart_rc = rtc.DEFERRED
        began = self.clock.t
        rc, out = self.carded()
        self.assertEqual((rc, out["outcome"], out["restart"], out["rc"]), (1, "error", "not-restarted", rtc.DEFERRED))
        self.assertEqual(self.restarts, ["claude"], "the lock never cleared: no second restart")
        self.assertGreaterEqual(self.clock.t - began, rtc.DEFER_WAIT_S)
        self.assertIn("Still busy: another restart was still in the way after I waited", out["say"])
        self.assertIn("The core is still on Codex. Ask me for the runtime card to try again.", out["say"])
        self.assertEqual(self.fake.edits()[-1], ("$card1", "Core runtime: " + out["say"]))
        self.assertFalse(self.ws.joinpath(*rtc.RUNNING).exists(), "the attempt is released: she can tap again")
        self.assertGreater(rtc.RUNNING_STALE_S, 2 * rtc.RESTART_TIMEOUT_S + rtc.DEFER_WAIT_S)

    def test_a_lock_gone_before_it_could_be_read_is_tied_by_a_result_left_since_this_switch_began(self):
        # Put off, and the other restart ended before its lock was looked at: no id was read. Its result,
        # left after this switch began, says Codex: asked once more; put off again: the same card says so.
        self.restart_rc = rtc.DEFERRED
        self.record("codex", T0 - 500)
        self.ws.joinpath(*rtc.RESTART_RESULT).parent.mkdir(parents=True, exist_ok=True)
        self.ws.joinpath(*rtc.RESTART_RESULT).write_text(json.dumps(
            {"rid": "grp-9-9", "runtime": "codex", "published": True, "at": int(self.clock.t) + 5}))
        rc, out = self.carded()
        self.assertEqual((self.restarts, out["rc"]), (["claude", "claude"], rtc.DEFERRED))
        self.assertIn("Still busy", out["say"])
        # The same result from before this switch began is not that restart's: nothing is asked again.
        self.restarts.clear()
        self.ws.joinpath(*rtc.RESTART_RESULT).write_text(json.dumps(
            {"rid": "grp-9-9", "runtime": "codex", "published": True, "at": int(self.clock.t) - 5000}))
        self.ws.joinpath(*rtc.RUNNING).write_text(json.dumps(
            {"attempt": self.attempt, "runtime": "claude", "room": DM, "ts": 1e12}))
        rc, out = self.carded()
        self.assertEqual((self.restarts, out["why"]), (["claude"], "inconclusive"))

    def test_an_unclean_restart_still_confirms_what_it_can(self):
        self.restart_rc, self.launcher_confirms, self.launcher_selects = 1, False, False
        rc, out = self.switch()
        self.assertEqual(out["outcome"], "error")
        self.assertIn("did not finish cleanly (exit 1)", out["say"])

    def test_only_the_recorded_attempt_runs(self):
        rc, out = self.switch(attempt="1-deadbeef")
        self.assertEqual((rc, out["status"]), (2, "refused"))
        rc, out = self.switch(runtime="codex")
        self.assertEqual((rc, out["status"]), (2, "refused"))
        self.assertEqual(self.restarts, [])

    def test_the_outcome_edits_the_card_the_tap_answered(self):
        rc, out = self.run_(["switch", "--room", DM, "--runtime", "claude", "--attempt", self.attempt, "--card", "$card1"])
        self.assertEqual((rc, out["outcome"], out["event_id"]), (0, "done", "$card1"))
        self.assertEqual(self.fake.edits()[-1], ("$card1", "Core runtime: " + out["say"]))
        self.assertEqual(self.fake.calls[-1][2], f"runtime-card:{self.attempt}:outcome-edit")
        self.assertEqual(len(self.fake.cards()), 1, "the outcome adds no message")

    def test_an_outcome_edit_still_refused_on_the_last_try_is_posted_as_a_card(self):
        real, tried = self.fake.execute, []

        def execute(action, arguments, **kw):
            tried.append(action)
            return {"is_error": True, "code": "M_FORBIDDEN"} if action == "room.message.edit" else real(action, arguments, **kw)
        self.fake.execute = execute
        rc, out = self.run_(["switch", "--room", DM, "--runtime", "claude", "--attempt", self.attempt, "--card", "$card1"])
        self.assertEqual((rc, out["status"], out["outcome"]), (0, "posted", "done"))
        self.assertEqual(tried, ["room.message.edit"] * rtc.POST_TRIES + ["room.message.send"])
        self.assertEqual((self.fake.cards()[-1]["id"], self.fake.cards()[-1]["state"]), (rtc.NOTE_ID, "done"))

    def test_outcome_post_is_retried(self):
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) < 2:
                raise ac.CardError("offline")
            return self.fake.execute(*a, **k)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rtc.main(["--workspace", str(self.ws), "switch", "--room", DM, "--runtime", "claude",
                           "--attempt", self.attempt], core=CORE, execute=flaky, restart=self.restart,
                          sleep=self.clock.sleep, clock=self.clock)
        self.assertEqual((rc, json.loads(out.getvalue())["status"]), (0, "posted"))
        self.assertEqual(len(calls), 2)


class Restart(unittest.TestCase):
    def test_graceful_restart_gets_the_runtime_and_the_in_session_override(self):
        seen = {}

        def run(argv, **kw):
            seen.update(argv=argv, env=kw["env"])
            return mock.Mock(returncode=0)
        with mock.patch.dict(os.environ, {"SUTANDO_CORE_RUNTIME": "codex"}):
            self.assertEqual(rtc.restart_core("claude", run=run), 0)
        self.assertEqual(seen["argv"][1:], [str(rtc.RESTART), "--", "--runtime", "claude"])
        self.assertEqual(seen["env"]["SUTANDO_ALLOW_INSESSION_RESTART"], "1")
        self.assertNotIn("SUTANDO_CORE_RUNTIME", seen["env"])

    def test_configured_runtime_ignores_the_callers_own_override(self):
        with tempfile.TemporaryDirectory() as app:
            Path(app, "runtime.json").write_text(json.dumps({"coreRuntime": "claude"}))
            with mock.patch.dict(os.environ, {"SUTANDO_APP_SUPPORT": app, "SUTANDO_CORE_RUNTIME": "codex"}):
                self.assertEqual(rtc.configured_runtime(), "claude")
                self.assertEqual(os.environ["SUTANDO_CORE_RUNTIME"], "codex", "restored afterwards")
                self.assertTrue(rtc.desktop_hosted())


if __name__ == "__main__":
    unittest.main(verbosity=1)
