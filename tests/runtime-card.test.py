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
        return [c[1]["extra_content"][ac.CARD_KEY] for c in self.calls]


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
        self.assertEqual((self.fake.cards()[-1]["id"], self.fake.cards()[-1]["state"]), (rtc.NOTE_ID, "pending"))
        self.assertEqual(self.restarts, [], "nothing is restarted in the tap itself")

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

    def test_a_waiter_that_cannot_be_started_says_to_ask_again(self):
        self.claude_json = {"loggedIn": False}
        self.launch.fail = OSError("no nohup")
        rc, out = self.tap("Switch the core to Claude Code")
        self.assertEqual((rc, out["status"], out["waiting"]), (1, "needs_sign_in", ""))
        self.assertIn("ask again", out["why"])

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
        card = self.fake.cards()[-1]
        self.assertEqual((card["state"], card["header"]["status"]["label"]), ("pending", "Switching"))
        self.assertIn("Switching the core from Codex to Claude Code", out["say"])
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())
        self.assertTrue(self.ws.joinpath(*rtc.RUNNING).exists(), "the one switch under way")
        self.assertEqual(self.restarts, [], "the waiter restarts nothing itself")

    def test_no_sign_in_within_the_wait_says_so_and_changes_nothing(self):
        rc, out = self.wait()
        self.assertEqual((rc, out["status"], out["posted"]), (1, "gave_up", "posted"))
        self.assertIn("I stopped waiting for the Claude Code sign-in after 15 minutes. The core is still on Codex", out["say"])
        card = self.fake.cards()[-1]
        self.assertEqual((card["state"], card["header"]["status"]["label"]), ("error", "Not switched"))
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

    def test_a_core_already_on_that_runtime_ends_the_wait(self):
        self.current = "claude"
        rc, out = self.wait()
        self.assertEqual((rc, out["status"]), (0, "already"))
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
        for code in rtc.NOT_RESTARTED:
            with self.subTest(rc=code):
                self.restart_rc = code
                rc, out = self.switch()
                self.assertEqual((rc, out["outcome"], out["restart"]), (1, "error", "not-restarted"))
                self.assertIn("The core is still on Codex", out["say"])
                self.ws.joinpath(*rtc.RUNNING).write_text(json.dumps(
                    {"attempt": self.attempt, "runtime": "claude", "room": DM, "ts": 1e12}))

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
