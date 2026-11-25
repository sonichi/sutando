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
import subprocess
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


def worker_on_record(record: Path, alive: bool) -> None:
    """The record's attempt, with a worker that still runs (this process) or one that has exited."""
    import subprocess as sp
    import holder_lock
    if alive:
        pid = os.getpid()
    else:
        proc = sp.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        pid = proc.pid
    attempt = json.loads(record.read_text())["attempt"]
    record.with_name(record.stem + ".worker.json").write_text(json.dumps(
        {"attempt": attempt, "pid": pid, "start": holder_lock.start_time(os.getpid())}))


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
        # The gateway step has its own tests, which put the real one back (gateway_ctl).
        self.no_gateway = mock.patch.object(rtc, "restore_desktop_gateway", lambda run=None: "")
        self.no_gateway.start()
        self.addCleanup(mock.patch.stopall)

    # fakes ------------------------------------------------------------------------------------------
    def claude_run(self, argv, **kw):
        self.assertEqual(argv[:3], ["claude", "auth", "status"])
        out = self.claude_json if isinstance(self.claude_json, str) else json.dumps(self.claude_json)
        return mock.Mock(stdout=out, returncode=0)

    def codex_read(self, home):
        """Codex confirms the account the home's auth.json names."""
        import codex_account
        got = codex_account.from_auth_file(home)
        return dict(got, verified=True) if got else got

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
        self.assertIn("I'm using Codex.", card["text"])
        self.assertEqual([(b["label"], b["style"]) for b in card["buttons"]],
                         [("Codex", "primary"), ("Claude Code", "secondary")])
        self.assertEqual([b["command"] for b in card["buttons"]], ["card:runtime:core:codex", "card:runtime:core:claude"])
        self.assertIn("conversation does not", card["note"])
        self.assertEqual(self.fake.calls[0][3], DM)

    def test_a_claude_core_marks_claude_code(self):
        self.current = "claude"
        self.post()
        card = self.fake.cards()[0]
        self.assertIn("I'm using Claude Code.", card["text"])
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
        self.assertEqual(self.fake.edits(), [("$card1", "Runtime: " + out["say"])])
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
        argv = self.launch.calls[0][0]
        self.assertEqual(argv[argv.index("--card") + 1], "$card2", "its outcome goes to the card that says Switching")

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

    def app_server(self, account):
        """codex_account.read itself, with only the app-server child replaced: it answers `account`."""
        import codex_account
        import codex_rpc

        class Child:
            def __init__(self, argv, **kw):
                self.p = mock.Mock()

            def call(self, method, params, timeout=None):
                return {"id": 1, "result": {"account": account} if method == "account/read" else {}}

            def send(self, msg):
                pass

            def close(self):
                pass
        patch = mock.patch.object(codex_rpc, "Rpc", Child)
        patch.start()
        self.addCleanup(patch.stop)
        self.codex_read = codex_account.read

    def test_a_saved_codex_token_codex_does_not_confirm_is_not_a_sign_in(self):
        # auth.json still decodes, but Codex itself names no account: the file's claims are not a sign-in.
        self.current = self.selected = "claude"
        (self.home / "auth.json").write_text(CODEX_AUTH)
        self.app_server(None)
        rc, out = self.tap("Switch the core to Codex")
        self.assertEqual((rc, out["status"]), (1, "needs_sign_in"))
        self.assertIn("could not confirm Codex is signed in", out["say"])
        self.assertEqual(self.fake.cards()[-1]["header"]["status"]["label"], "Sign in first")
        self.assertEqual([c[0][c[0].index("--workspace") + 2] for c in self.launch.calls], ["await-sign-in"])
        self.assertFalse(self.ws.joinpath(*rtc.RUNNING).exists())
        self.assertEqual(self.restarts, [])

    def test_a_codex_sign_in_codex_confirms_is_switched_to(self):
        self.current = self.selected = "claude"
        (self.home / "auth.json").write_text(CODEX_AUTH)
        self.app_server({"type": "chatgpt", "email": "alice@example.com", "planType": "plus"})
        rc, out = self.tap("Switch the core to Codex")
        self.assertEqual((rc, out["status"]), (0, "started"))
        self.assertEqual([c[0][c[0].index("--workspace") + 2] for c in self.launch.calls], ["switch"])

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

    def test_a_switch_whose_worker_died_no_longer_blocks_a_tap(self):
        self.tap("Switch the core to Claude Code")
        record = self.ws.joinpath(*rtc.RUNNING)
        worker_on_record(record, alive=True)
        self.assertEqual(self.tap("Switch the core to Claude Code")[1]["say"], rtc.BUSY)
        worker_on_record(record, alive=False)
        before = json.loads(record.read_text())["attempt"]
        rc, out = self.tap("Switch the core to Claude Code")
        self.assertEqual((rc, out["status"]), (0, "started"))
        self.assertTrue(out["say"].startswith(rtc.STOPPED + " The core is on Codex. Switching the core from Codex"),
                        out["say"])
        self.assertEqual(self.fake.edits()[-1], ("$card1", "Runtime: " + out["say"]))
        self.assertNotEqual(json.loads(record.read_text())["attempt"], before, "the dead attempt's record is replaced")


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
        self.assertEqual(self.fake.edits(), [("$card2", "Runtime: " + out["say"])])
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

    def stopped_after_claiming(self, phase, **noted):
        """A waiter that claimed `phase` and was killed before doing it: what it leaves on record."""
        mine = {"attempt": self.attempt, "runtime": "claude", "room": DM}
        self.assertTrue(rtc.claim_wait(self.ws, mine, phase, **noted))
        rec = json.loads(self.ws.joinpath(*rtc.WAITING).read_text())
        self.assertEqual((rec["phase"], rec["attempt"], rec["card"]), (phase, self.attempt, "$card2"))
        return rec

    def admitted(self, attempt):
        self.ws.joinpath(*rtc.RUNNING).write_text(json.dumps(
            {"attempt": attempt, "runtime": "claude", "room": DM, "ts": self.clock.t}))

    def test_the_switchs_own_id_is_put_on_record_with_the_claim_before_anything_is_admitted(self):
        seen = {}
        real = rtc.begin_switch

        def begin(ws, room, rt, current, t, **kw):
            seen.update(json.loads(self.ws.joinpath(*rtc.WAITING).read_text()), given=kw["attempt"])
            return real(ws, room, rt, current, t, **kw)
        self.claude_json = {"loggedIn": True}
        with mock.patch.object(rtc, "begin_switch", begin):
            rc, out = self.wait()
        self.assertEqual((seen["phase"], seen["switch"]), ("switching", seen["given"]))
        self.assertEqual((out["status"], out["attempt"]), ("started", seen["given"]), "admitted under that id")

    def test_a_waiter_stopped_between_claiming_the_sign_in_and_admitting_the_switch_is_finished_by_the_next(self):
        self.stopped_after_claiming("switching", switch="7-abc")
        # The core's start hook: the wait is still on record, so a waiter is started for it again.
        self.assertEqual(self.run_(["resume"])[1]["status"], "resumed")
        self.launch.calls.clear()
        self.claude_json = {"loggedIn": False}      # it does not ask again: the sign-in was already seen in time
        self.clock.sleep(rtc.SIGN_IN_WAIT_S * 2)    # nor does a deadline that passed meanwhile undo it
        rc, out = self.wait()
        self.assertEqual((rc, out["status"], out["after_sign_in"], out["attempt"]), (0, "started", True, "7-abc"))
        self.assertIn("switch", self.launch.calls[0][0])
        self.assertEqual(self.fake.edits()[-1][0], "$card2")
        self.assertEqual(json.loads(self.ws.joinpath(*rtc.RUNNING).read_text())["attempt"], "7-abc")
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists(), "removed only once the worker was launched")

    def test_a_waiter_stopped_after_admission_and_before_the_worker_was_launched_gets_it_launched(self):
        self.stopped_after_claiming("switching", switch="7-abc")
        self.admitted("7-abc")                      # admitted; killed before the spawn: no worker holds the lock
        said = len(self.fake.calls)
        rc, out = self.wait()
        self.assertEqual((rc, out["status"], out["attempt"], out["relaunched"]), (0, "started", "7-abc", True))
        (argv, kw), = self.launch.calls
        self.assertEqual((argv[argv.index("--workspace") + 2], argv[argv.index("--attempt") + 1],
                          argv[argv.index("--card") + 1]), ("switch", "7-abc", "$card2"))
        # "Switching" is asked for once more under the attempt's own operation id, which the room answers
        # with the card it already has: an edit of the same card, never a new message.
        again = self.fake.calls[said:]
        self.assertEqual([(c[0], c[1]["event_id"], c[2]) for c in again],
                         [("room.message.edit", "$card2", "runtime-card:7-abc:switching-edit")])
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())
        # A worker that cannot be launched leaves the claim on record for the next waiter.
        self.stopped_after_claiming_again = rtc.set_wait(self.ws, {
            "attempt": self.attempt, "runtime": "claude", "room": DM, "ts": self.clock.t, "card": "$card2"})
        self.stopped_after_claiming("switching", switch="7-abc")
        self.launch.fail = OSError("no fork")
        rc, out = self.wait()
        self.assertEqual((rc, out["status"], out["kept"]), (1, "failed", "switching"))
        self.assertEqual(json.loads(self.ws.joinpath(*rtc.WAITING).read_text())["phase"], "switching")

    def test_a_waiter_stopped_after_its_worker_was_launched_starts_nothing_more(self):
        from file_lock import try_lock_fd, unlock_fd
        self.stopped_after_claiming("switching", switch="7-abc")
        self.admitted("7-abc")
        lock = self.ws.joinpath(*rtc.EXEC_LOCK)
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
        self.assertTrue(try_lock_fd(fd))            # its worker is running: it holds the execution lock
        try:
            said = len(self.fake.calls)
            rc, out = self.wait()
        finally:
            unlock_fd(fd)
            os.close(fd)
        self.assertEqual((rc, out["status"]), (0, "handed_off"))
        self.assertEqual((self.launch.calls, len(self.fake.calls)), ([], said), "nothing started, nothing said twice")
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())

    def test_a_worker_that_already_finished_is_not_run_again_and_another_switchs_admission_is_not_ours(self):
        # Its worker ran and finished (nothing is admitted now, the core is on Claude Code): done, not redone.
        self.stopped_after_claiming("switching", switch="7-abc")
        self.current = self.selected = "claude"
        rc, out = self.wait()
        self.assertEqual((out["status"], self.launch.calls, self.restarts), ("handed_off", [], []))
        # Another switch is admitted (not this one): this one is not taken for launched; it is told it is busy.
        self.current = self.selected = "codex"
        rtc.set_wait(self.ws, {"attempt": self.attempt, "runtime": "claude", "room": DM, "ts": self.clock.t,
                               "card": "$card2"})
        self.stopped_after_claiming("switching", switch="7-abc")
        self.admitted("9-other")
        rc, out = self.wait()
        self.assertEqual((rc, out["status"], out["say"], self.launch.calls), (1, "say", rtc.BUSY, []))
        self.assertEqual(json.loads(self.ws.joinpath(*rtc.RUNNING).read_text())["attempt"], "9-other")
        # Its output goes to a log, so the card it was to switch from is told, and only then is the wait gone.
        self.assertEqual((out["told"], self.fake.edits()[-1]), ("posted", ("$card2", "Runtime: " + rtc.BUSY)))
        self.assertEqual(self.fake.calls[-1][2], f"runtime-card:signin:{self.attempt}:not-switched-edit")
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())

    def offline_wait(self):
        ops = []

        def offline(action, arguments, *, operation_id, room_id):
            ops.append(operation_id)
            raise ac.CardError("offline")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rtc.main(["--workspace", str(self.ws), "await-sign-in", "--room", DM, "--runtime", "claude",
                           "--attempt", self.attempt], core=CORE, popen=self.launch, run=self.claude_run,
                          execute=offline, codex_read=self.codex_read, restart=self.restart,
                          sleep=self.clock.sleep, clock=self.clock)
        return rc, json.loads(out.getvalue()), ops

    def test_a_hand_off_that_did_not_start_is_an_outcome_on_record_and_only_its_delivery_is_retried(self):
        # Another switch is admitted, and the room is unreachable: the refusal is decided, not delivered.
        self.stopped_after_claiming("switching", switch="7-abc")
        self.admitted("9-other")
        rc, out, ops = self.offline_wait()
        self.assertEqual((rc, out["status"], out["told"], out["kept"]), (1, "say", "failed", "refused"))
        kept = json.loads(self.ws.joinpath(*rtc.WAITING).read_text())
        self.assertEqual((kept["phase"], kept["words"]), ("refused", rtc.BUSY), "the exact words, before sending")
        # By the next waiter the other switch is over and a sign-in is there: it still only tells the card.
        self.ws.joinpath(*rtc.RUNNING).unlink()
        self.claude_json = {"loggedIn": True}
        rc, out = self.wait()
        self.assertEqual((rc, out["status"], out["told"]), (1, "refused", "posted"))
        self.assertEqual((self.launch.calls, self.restarts), ([], []), "the switch is not started after all")
        self.assertEqual(self.fake.edits()[-1], ("$card2", "Runtime: " + rtc.BUSY))
        self.assertEqual(self.fake.calls[-1][2], f"runtime-card:signin:{self.attempt}:not-switched-edit")
        self.assertIn(self.fake.calls[-1][2], ops, "the same operation id as the tries that failed")
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())

    def test_a_refusal_the_card_service_applied_without_a_receipt_is_told_again_never_switched(self):
        self.stopped_after_claiming("switching", switch="7-abc")
        self.admitted("9-other")
        applied = []

        def applied_then_lost(action, arguments, *, operation_id, room_id):
            applied.append((operation_id, arguments.get("body")))     # the service did it; the answer never came
            raise ac.CardError("timed out")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rtc.main(["--workspace", str(self.ws), "await-sign-in", "--room", DM, "--runtime", "claude",
                      "--attempt", self.attempt], core=CORE, popen=self.launch, run=self.claude_run,
                     execute=applied_then_lost, codex_read=self.codex_read, restart=self.restart,
                     sleep=self.clock.sleep, clock=self.clock)
        self.assertEqual(json.loads(out.getvalue())["kept"], "refused")
        self.ws.joinpath(*rtc.RUNNING).unlink()
        rc, again = self.wait()
        self.assertEqual((again["status"], self.launch.calls), ("refused", []))
        edits = {(op, body) for op, body in applied if op.endswith("-edit")}
        self.assertEqual(edits, {(self.fake.calls[-1][2], self.fake.calls[-1][1]["body"])},
                         "one operation id, one text, on every try")

    def test_a_worker_that_cannot_start_and_a_correction_that_cannot_be_sent_leave_that_outcome_on_record(self):
        self.stopped_after_claiming("switching", switch="7-abc")
        self.launch.fail = OSError("no fork")

        def correction_lost(action, arguments, *, operation_id, room_id):
            if ":not-switched" in operation_id:
                raise ac.CardError("offline")           # "Switching" got through; its correction does not
            return self.fake.execute(action, arguments, operation_id=operation_id, room_id=room_id)
        rc, out = self.wait_with(correction_lost)
        self.assertEqual((rc, out["status"], out["told"], out["kept"]), (1, "failed", "failed", "refused"))
        self.assertFalse(self.ws.joinpath(*rtc.RUNNING).exists(), "the attempt is released")
        self.assertEqual(json.loads(self.ws.joinpath(*rtc.WAITING).read_text())["words"], rtc.FAILED_TO_START)
        self.launch.fail = None
        rc, out = self.wait()
        self.assertEqual((out["status"], self.launch.calls), ("refused", []))
        self.assertEqual(self.fake.edits()[-1], ("$card2", "Runtime: " + rtc.FAILED_TO_START))
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())

    def test_with_the_room_unreachable_the_first_hand_off_starts_nothing_and_stays_pending(self):
        self.claude_json = {"loggedIn": True}
        rc, out, ops = self.offline_wait()
        self.assertEqual((rc, out["status"], out["kept"], self.launch.calls), (1, "failed", "switching", []))
        kept = json.loads(self.ws.joinpath(*rtc.WAITING).read_text())
        self.assertEqual((kept["phase"], kept["route"]), ("switching", "post"))
        self.assertEqual(json.loads(self.ws.joinpath(*rtc.RUNNING).read_text())["attempt"], kept["switch"])

    def test_a_switch_whose_worker_cannot_be_started_does_not_leave_the_card_saying_switching(self):
        self.claude_json = {"loggedIn": True}
        self.launch.fail = OSError("no fork")
        rc, out = self.tap("Switch the core to Claude Code")
        self.assertEqual((out["status"], out["say"], out["told"]), ("failed", rtc.FAILED_TO_START, "posted"))
        words = [w for _, w in self.fake.edits()]
        self.assertIn("Switching the core from Codex to Claude Code", words[-2])
        self.assertEqual(words[-1], "Runtime: " + rtc.FAILED_TO_START)
        self.assertFalse(self.ws.joinpath(*rtc.RUNNING).exists(), "released: she can tap again")


    def test_last_words_that_could_not_be_delivered_keep_the_phase_and_are_said_by_the_next_waiter(self):
        for phase, status in (("expired", "gave_up"), ("already", "already")):
            with self.subTest(phase=phase):
                rtc.set_wait(self.ws, {"attempt": self.attempt, "runtime": "claude", "room": DM, "ts": self.clock.t,
                                       "card": "$card2"})
                self.stopped_after_claiming(phase)
                ops = []

                def offline(action, arguments, *, operation_id, room_id):
                    ops.append(operation_id)
                    raise ac.CardError("offline")
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = rtc.main(["--workspace", str(self.ws), "await-sign-in", "--room", DM, "--runtime", "claude",
                                   "--attempt", self.attempt], core=CORE, popen=self.launch, run=self.claude_run,
                                  execute=offline, codex_read=self.codex_read, restart=self.restart,
                                  sleep=self.clock.sleep, clock=self.clock)
                first = json.loads(out.getvalue())
                self.assertEqual((rc, first["status"], first["kept"]), (1, status, phase))
                self.assertEqual(json.loads(self.ws.joinpath(*rtc.WAITING).read_text())["phase"], phase, "not delivered: kept")
                self.assertGreaterEqual(len(ops), rtc.POST_TRIES, "it tried more than once before leaving it")
                rc, again = self.wait()             # the next waiter, with the room reachable
                self.assertEqual(again["status"], status)
                self.assertEqual(self.fake.edits()[-1][0], "$card2")
                self.assertIn(self.fake.calls[-1][2], ops, "the same operation id as the failed delivery")
                self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())


    def test_a_waiter_killed_right_after_its_correction_reached_the_room_never_starts_the_switch(self):
        class Killed(BaseException):
            """The waiter's process ending at that instant: nothing after the call runs."""

        self.stopped_after_claiming("switching", switch="7-abc")
        self.launch.fail = OSError("no fork")
        accepted = []

        def accepted_then_killed(action, arguments, *, operation_id, room_id):
            if operation_id.endswith(":not-switched-edit"):
                accepted.append(arguments["body"])      # the room took the corrective edit
                raise Killed()
            return self.fake.execute(action, arguments, operation_id=operation_id, room_id=room_id)
        with self.assertRaises(Killed), contextlib.redirect_stdout(io.StringIO()):
            rtc.main(["--workspace", str(self.ws), "await-sign-in", "--room", DM, "--runtime", "claude",
                      "--attempt", self.attempt], core=CORE, popen=self.launch, run=self.claude_run,
                     execute=accepted_then_killed, codex_read=self.codex_read, restart=self.restart,
                     sleep=self.clock.sleep, clock=self.clock)
        self.assertEqual(accepted, ["Runtime: " + rtc.FAILED_TO_START])
        kept = json.loads(self.ws.joinpath(*rtc.WAITING).read_text())
        self.assertEqual((kept["phase"], kept["words"]), ("refused", rtc.FAILED_TO_START), "on record before the first room write")
        self.assertNotIn("runtime-card:7-abc:not-started-edit", [c[2] for c in self.fake.calls],
                         "begin_switch itself said nothing about the failure")
        # Resumed with a worker that would now start: it only repeats the telling.
        self.launch.fail = None
        rc, out = self.wait()
        self.assertEqual((out["status"], self.launch.calls, self.restarts), ("refused", [], []))
        self.assertEqual(self.fake.edits()[-1], ("$card2", "Runtime: " + rtc.FAILED_TO_START))
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())

    def test_a_switching_card_posted_as_a_fallback_is_the_one_corrected_when_the_worker_cannot_start(self):
        self.stopped_after_claiming("switching", switch="7-abc")
        self.launch.fail = OSError("no fork")
        refuse_edit_of = {"$card2"}

        def execute(action, arguments, *, operation_id, room_id):
            if action == "room.message.edit" and arguments["event_id"] in refuse_edit_of:
                raise ac.CardError("not editable")
            return self.fake.execute(action, arguments, operation_id=operation_id, room_id=room_id)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rtc.main(["--workspace", str(self.ws), "await-sign-in", "--room", DM, "--runtime", "claude",
                      "--attempt", self.attempt], core=CORE, popen=self.launch, run=self.claude_run,
                     execute=execute, codex_read=self.codex_read, restart=self.restart,
                     sleep=self.clock.sleep, clock=self.clock)
        got = json.loads(out.getvalue())
        # "Sign in first" ($card2) could not be edited, so "Switching" went out as a new card.
        switching = [c for c in self.fake.calls if c[0] == "room.message.send"][-1]
        self.assertIn("Switching the core from Codex to Claude Code", switching[1]["extra_content"][ac.CARD_KEY]["text"])
        new_card = got["event_id"]
        self.assertNotEqual(new_card, "$card2")
        # The correction edits that new card, the one promising a restart, and the wait is done.
        self.assertEqual((got["status"], got["told"]), ("failed", "posted"))
        self.assertEqual(self.fake.edits()[-1], (new_card, "Runtime: " + rtc.FAILED_TO_START))
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())

    def test_the_fallback_cards_id_is_on_record_with_the_refusal_and_a_resumed_waiter_edits_that_card(self):
        self.stopped_after_claiming("switching", switch="7-abc")
        self.launch.fail = OSError("no fork")

        def only_switching_gets_out(action, arguments, *, operation_id, room_id):
            # No edit is accepted, and of the posts only the fallback "Switching" card: the refusal that
            # follows can be neither edited in nor posted.
            if action == "room.message.edit" or not operation_id.endswith(":switching"):
                raise ac.CardError("unreachable")
            return self.fake.execute(action, arguments, operation_id=operation_id, room_id=room_id)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rtc.main(["--workspace", str(self.ws), "await-sign-in", "--room", DM, "--runtime", "claude",
                           "--attempt", self.attempt], core=CORE, popen=self.launch, run=self.claude_run,
                          execute=only_switching_gets_out, codex_read=self.codex_read, restart=self.restart,
                          sleep=self.clock.sleep, clock=self.clock)
        got = json.loads(out.getvalue())
        new_card = got["event_id"]
        self.assertNotEqual(new_card, "$card2")
        self.assertEqual((rc, got["status"], got["told"], got["kept"]), (1, "failed", "failed", "refused"))
        # What the waiter itself saved: the refusal, its words, and the fallback card as the target.
        kept = json.loads(self.ws.joinpath(*rtc.WAITING).read_text())
        self.assertEqual((kept["phase"], kept["words"], kept["card"]), ("refused", rtc.FAILED_TO_START, new_card))
        # Resumed from that record as it stands, with the room back and a worker that would start.
        self.launch.fail = None
        rc, again = self.wait()
        self.assertEqual((again["status"], again["told"], self.launch.calls), ("refused", "posted", []))
        self.assertEqual(self.fake.edits()[-1], (new_card, "Runtime: " + rtc.FAILED_TO_START))
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())

    def edits_of_refused(self, *refused):
        def execute(action, arguments, *, operation_id, room_id):
            if action == "room.message.edit" and arguments["event_id"] in refused:
                raise ac.CardError("not editable")
            return self.fake.execute(action, arguments, operation_id=operation_id, room_id=room_id)
        return execute

    def tap_with(self, execute):
        path = self.ws / "task.txt"
        path.write_text(task("Switch the core to Claude Code", reply_to_event="$card1",
                             card_click=json.dumps({"card_id": rtc.CARD_ID, "action": "Switch the core to Claude Code"})))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rtc.main(["--workspace", str(self.ws), "reply", "--task-file", str(path)], core=CORE,
                          popen=self.launch, run=self.claude_run, codex_read=self.codex_read, execute=execute,
                          restart=self.restart, sleep=self.clock.sleep, clock=self.clock)
        return rc, json.loads(out.getvalue())

    def test_an_ordinary_tap_whose_switching_went_out_as_a_new_card_hands_that_card_to_the_worker(self):
        self.claude_json = {"loggedIn": True}
        rc, out = self.tap_with(self.edits_of_refused("$card1"))
        self.assertEqual(out["status"], "started")
        new_card = out["event_id"]
        self.assertNotEqual(new_card, "$card1")
        argv = self.launch.calls[-1][0]
        self.assertEqual(argv[argv.index("--card") + 1], new_card, "the outcome will edit the card that says Switching")

    def test_an_ordinary_tap_whose_worker_cannot_start_corrects_the_new_switching_card(self):
        self.claude_json = {"loggedIn": True}
        self.launch.fail = OSError("no fork")
        rc, out = self.tap_with(self.edits_of_refused("$card1"))
        new_card = out["event_id"]
        self.assertEqual((out["status"], out["told"]), ("failed", "posted"))
        self.assertNotEqual(new_card, "$card1")
        self.assertEqual(self.fake.edits()[-1], (new_card, "Runtime: " + rtc.FAILED_TO_START))
        self.assertEqual(len(self.fake.cards()), 3, "no separate notice beside the Switching card")

    def deduping(self, refuse_edits_of=()):
        """The room as it answers: one event per operation id, however often it is asked."""
        seen = {}

        def execute(action, arguments, *, operation_id, room_id):
            if action == "room.message.edit" and arguments["event_id"] in refuse_edits_of:
                raise ac.CardError("not editable")
            if operation_id not in seen:
                seen[operation_id] = self.fake.execute(action, arguments, operation_id=operation_id, room_id=room_id)
            return seen[operation_id]
        return execute

    def wait_with(self, execute, popen=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rtc.main(["--workspace", str(self.ws), "await-sign-in", "--room", DM, "--runtime", "claude",
                           "--attempt", self.attempt], core=CORE, popen=popen or self.launch, run=self.claude_run,
                          execute=execute, codex_read=self.codex_read, restart=self.restart,
                          sleep=self.clock.sleep, clock=self.clock)
        return rc, json.loads(out.getvalue())

    def test_a_waiter_stopped_after_the_fallback_switching_card_and_before_the_worker_hands_that_card_on(self):
        class Killed(BaseException):
            """The waiter's process ending at the spawn: the worker was never started."""

        def killed_at_spawn(argv, **kw):
            raise Killed()
        room = self.deduping(refuse_edits_of=("$card2",))
        self.claude_json = {"loggedIn": True}
        with self.assertRaises(Killed), contextlib.redirect_stdout(io.StringIO()):
            self.wait_with(room, popen=killed_at_spawn)
        switching = [c for c in self.fake.calls if c[0] == "room.message.send" and c[2].endswith(":switching")]
        self.assertEqual(len(switching), 1, "Sign in first could not be edited: Switching went out as a new card")
        kept = json.loads(self.ws.joinpath(*rtc.WAITING).read_text())
        new_card = kept["card"]
        self.assertEqual(kept["phase"], "switching")
        self.assertNotEqual(new_card, "$card2", "the card that says Switching is on record before the spawn")
        self.assertEqual(json.loads(self.ws.joinpath(*rtc.RUNNING).read_text())["attempt"], kept["switch"])
        # Resumed: the worker gets that card, and no second Switching card is posted.
        rc, out = self.wait_with(room)
        self.assertEqual((out["status"], out["relaunched"]), ("started", True))
        argv = self.launch.calls[-1][0]
        self.assertEqual((argv[argv.index("--card") + 1], argv[argv.index("--attempt") + 1]), (new_card, kept["switch"]))
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "room.message.send" and c[2].endswith(":switching")]), 1)

    def test_stopped_before_the_fallback_card_was_saved_the_same_post_is_asked_for_again_and_recovered(self):
        room = self.deduping(refuse_edits_of=("$card2",))
        # Admitted and "Switching" posted as a new card, and the waiter died before saving which card.
        self.stopped_after_claiming("switching", switch="7-abc")
        self.admitted("7-abc")
        _, posted = rtc.say_switching(self.ws, DM, "claude", "codex", "7-abc", "$card2", core=CORE, execute=room)
        new_card = posted["event_id"]
        self.assertEqual(json.loads(self.ws.joinpath(*rtc.WAITING).read_text())["card"], "$card2", "not saved")
        rc, out = self.wait_with(room)
        argv = self.launch.calls[-1][0]
        self.assertEqual((out["status"], argv[argv.index("--card") + 1]), ("started", new_card))
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "room.message.send" and c[2].endswith(":switching")]), 1,
                         "the same operation id: the room returns the card it already posted")

    def test_the_original_card_editable_again_after_the_fallback_post_does_not_hide_the_fallback_card(self):
        class Killed(BaseException):
            """The waiter's process ending right after the fallback post: its receipt was never saved."""

        refused = {"$card2"}
        seen = {}

        def room(action, arguments, *, operation_id, room_id):
            if action == "room.message.edit" and arguments["event_id"] in refused:
                raise ac.CardError("briefly unavailable")
            if operation_id not in seen:
                seen[operation_id] = self.fake.execute(action, arguments, operation_id=operation_id, room_id=room_id)
            if action == "room.message.send" and operation_id.endswith(":switching") and not died:
                died.append(1)
                raise Killed()                  # posted; the answer never reached the waiter
            return seen[operation_id]
        died = []
        self.claude_json = {"loggedIn": True}
        with self.assertRaises(Killed), contextlib.redirect_stdout(io.StringIO()):
            self.wait_with(room)
        kept = json.loads(self.ws.joinpath(*rtc.WAITING).read_text())
        self.assertEqual((kept["phase"], kept["route"], kept["card"]), ("switching", "post", "$card2"),
                         "the route is on record before the post; the new card's id never was")
        fallback = [v for k, v in seen.items() if k.endswith(":switching")][0]["result"]["event_id"]
        # By the resume the original card can be edited again. The recorded route wins: the same post is
        # asked for, its card recovered, and the worker is handed that card.
        refused.clear()
        rc, out = self.wait_with(room)
        argv = self.launch.calls[-1][0]
        self.assertEqual((out["status"], argv[argv.index("--card") + 1]), ("started", fallback))
        self.assertEqual([e for e, _ in self.fake.edits() if e == "$card2"], [], "the old card is not made to say Switching")
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "room.message.send" and c[2].endswith(":switching")]), 1)

    def test_a_route_that_cannot_be_recorded_means_no_fallback_card_is_posted(self):
        room = self.deduping(refuse_edits_of=("$card2",))
        self.claude_json = {"loggedIn": True}
        real = rtc.keep_wait_card

        def keep(ws, mine, event="", **noted):
            if noted.get("route"):
                raise OSError("disk full")              # the route cannot be put on record
            return real(ws, mine, event, **noted)
        with mock.patch.object(rtc, "keep_wait_card", keep):
            rc, out = self.wait_with(room)
        self.assertEqual([c for c in self.fake.calls if c[0] == "room.message.send" and c[2].endswith(":switching")], [],
                         "no card whose route is not on record")
        # The switch itself still goes ahead, with no card of its own: its worker posts the outcome.
        argv = self.launch.calls[-1][0]
        self.assertEqual((out["status"], "--card" in argv), ("started", False))
        self.assertNotIn("event_id", out)

    def test_a_saved_switching_card_is_used_as_it_is_on_resume_and_nothing_is_sent_again(self):
        self.stopped_after_claiming("switching", switch="7-abc")
        self.admitted("7-abc")
        mine = {"attempt": self.attempt, "runtime": "claude", "room": DM}
        rtc.keep_wait_card(self.ws, mine, "$card9", says="$card9", route="post")
        said = len(self.fake.calls)
        rc, out = self.wait()
        argv = self.launch.calls[-1][0]
        self.assertEqual((out["status"], argv[argv.index("--card") + 1]), ("started", "$card9"))
        self.assertEqual(len(self.fake.calls), said, "no edit and no post: the saved card is the target")

    def losing(self, answers):
        """A room where editing "Sign in first" is unavailable and the first `answers` replies to the
        "Switching" post are lost after it has landed; one event per operation id."""
        seen, lost = {}, []

        def room(action, arguments, *, operation_id, room_id):
            if action == "room.message.edit" and arguments["event_id"] == "$card2":
                raise ac.CardError("unavailable")
            if operation_id not in seen:
                seen[operation_id] = self.fake.execute(action, arguments, operation_id=operation_id, room_id=room_id)
            if action == "room.message.send" and operation_id.endswith(":switching") and len(lost) < answers:
                lost.append(1)
                raise ac.CardError("timed out")
            return seen[operation_id]
        return room, seen

    def switching_posts(self):
        return [c for c in self.fake.calls if c[0] == "room.message.send" and c[2].endswith(":switching")]

    def test_the_first_waiter_whose_fallback_post_lost_its_answers_keeps_asking_and_recovers_by_itself(self):
        room, seen = self.losing(answers=2)
        self.claude_json = {"loggedIn": True}
        began = self.clock.t
        rc, out = self.wait_with(room)                  # one waiter, no resume, no manual command
        fallback = [v for k, v in seen.items() if k.endswith(":switching")][0]["result"]["event_id"]
        argv = self.launch.calls[-1][0]
        self.assertEqual((rc, out["status"], argv[argv.index("--card") + 1]), (0, "started", fallback))
        self.assertEqual(len(self.launch.calls), 1, "the worker was not started until the card was known")
        self.assertEqual(len(self.switching_posts()), 1, "one card, however often it was asked for")
        self.assertEqual(self.clock.t - began, rtc.POST_PAUSE_S, "it waited before asking a third time")
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())

    def test_a_re_tap_during_the_waiters_backoff_is_told_busy_and_the_waiter_still_finishes_its_hand_off(self):
        room, seen = self.losing(answers=3)
        self.claude_json = {"loggedIn": True}
        real, taps = self.clock.sleep, []

        def sleep(s):
            real(s)
            if not taps:                                # the owner taps the same runtime again meanwhile
                taps.append(self.tap_with(room))
        self.clock.sleep = sleep
        rc, out = self.wait_with(room)
        self.clock.sleep = real
        (tap_rc, tapped), = taps
        self.assertEqual((tapped["status"], tapped["say"]), ("say", rtc.BUSY))
        fallback = [v for k, v in seen.items() if k.endswith(":switching")][0]["result"]["event_id"]
        argv = self.launch.calls[-1][0]
        self.assertEqual((rc, out["status"], argv[argv.index("--card") + 1]), (0, "started", fallback))
        self.assertEqual((len(self.launch.calls), len(self.switching_posts())), (1, 1), "one card recovered, one launch")
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists(), "retired by its own waiter, once done")

    def test_a_tap_whose_own_switch_starts_retires_an_earlier_wait_and_one_that_fails_to_start_does_not(self):
        self.claude_json = {"loggedIn": True}
        self.launch.fail = OSError("no fork")
        rc, out = self.tap("Switch the core to Claude Code")
        self.assertEqual(out["status"], "failed")
        self.assertTrue(self.ws.joinpath(*rtc.WAITING).exists(), "nothing replaced it")
        self.launch.fail = None
        rc, out = self.tap("Switch the core to Claude Code")
        self.assertEqual(out["status"], "started")
        self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())

    def test_a_fallback_never_confirmed_starts_no_worker_and_stays_pending_for_the_next_waiter(self):
        room, seen = self.losing(answers=rtc.CARD_TRIES + 1)
        self.claude_json = {"loggedIn": True}
        rc, out = self.wait_with(room)
        self.assertEqual((rc, out["status"], out["kept"], self.launch.calls), (1, "failed", "switching", []))
        kept = json.loads(self.ws.joinpath(*rtc.WAITING).read_text())
        self.assertEqual((kept["phase"], kept["route"], kept.get("says")), ("switching", "post", None))
        self.assertEqual(json.loads(self.ws.joinpath(*rtc.RUNNING).read_text())["attempt"], kept["switch"], "still admitted")
        # The next waiter (a core start, or the card asked for) gets the receipt: one card was ever posted.
        rc, out = self.wait_with(room)
        fallback = [v for k, v in seen.items() if k.endswith(":switching")][0]["result"]["event_id"]
        argv = self.launch.calls[-1][0]
        self.assertEqual((out["status"], argv[argv.index("--card") + 1], len(self.switching_posts())), ("started", fallback, 1))
        self.assertEqual([e for e, _ in self.fake.edits() if e == "$card2"], [])

    def test_a_waiter_stopped_before_its_last_words_leaves_them_for_the_next_to_say(self):
        for phase, status, said in (("expired", "gave_up", "I stopped waiting for the Claude Code sign-in"),
                                    ("already", "already", "The core is now running on Claude Code")):
            with self.subTest(phase=phase):
                rtc.set_wait(self.ws, {"attempt": self.attempt, "runtime": "claude", "room": DM, "ts": self.clock.t,
                                       "card": "$card2"})
                self.stopped_after_claiming(phase)
                self.claude_json = {"loggedIn": True}       # a sign-in now changes nothing: the step was decided
                rc, out = self.wait()
                self.assertEqual(out["status"], status)
                event, words = self.fake.edits()[-1]
                self.assertEqual(event, "$card2")
                self.assertIn(said, words)
                self.assertEqual((self.launch.calls, self.restarts), ([], []))
                self.assertFalse(self.ws.joinpath(*rtc.WAITING).exists())

    def test_a_newer_tap_replaces_a_claimed_wait_and_the_older_waiter_stops(self):
        self.stopped_after_claiming("switching")
        rc, newer = self.tap("Switch the core to Claude Code")
        self.launch.calls.clear()
        rc, out = self.wait()                       # the older attempt's resumed waiter
        self.assertEqual((out["status"], self.launch.calls), ("superseded", []))
        rec = json.loads(self.ws.joinpath(*rtc.WAITING).read_text())
        self.assertEqual((rec["attempt"], "phase" in rec), (newer["waiting"], False))
        # A wait whose removal fails stays on record; repeating its step later is safe.
        with mock.patch.object(rtc, "claim_wait", side_effect=OSError("disk")):
            rtc.finish_wait(self.ws, {"attempt": newer["waiting"], "runtime": "claude", "room": DM})
        self.assertTrue(self.ws.joinpath(*rtc.WAITING).exists())

    def test_no_sign_in_within_the_wait_says_so_and_changes_nothing(self):
        rc, out = self.wait()
        self.assertEqual((rc, out["status"], out["posted"]), (1, "gave_up", "posted"))
        self.assertIn("I stopped waiting for the Claude Code sign-in after 15 minutes. The core is still on Codex", out["say"])
        self.assertEqual(self.fake.edits(), [("$card2", "Runtime: " + out["say"])])
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
        self.assertEqual(self.fake.edits(), [("$card2", "Runtime: The core is now running on Claude Code. "
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
        self.assertEqual(out["say"], "Switched to Claude Code. The core restarted on it ✓ " + rtc.CARRIES)
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

    def failing_switch(self, back_ok, back_rc=None):
        """The restart onto Claude Code fails and leaves no core; the relaunch of Codex comes back or not."""
        self.record("codex", T0 - 600)
        before = self.ws.joinpath(*rtc.RECORD).read_text()
        seen = []

        def restart(rt):
            self.restarts.append(rt)
            self.clock.t += 30
            if rt == "claude":
                self.ws.joinpath(*rtc.RECORD).unlink()
                return 1
            seen.append(self.ws.joinpath(*rtc.RECORD).read_text())
            if back_ok:
                self.record("codex", self.clock.t - 5)
                self.current = self.selected = "codex"
            return back_rc if back_rc is not None else (0 if back_ok else 1)
        self.restart = restart
        rc, out = self.carded()
        self.assertEqual(seen, [before], "the previous runtime's record is put back before it is relaunched")
        self.assertEqual(self.restarts, ["claude", "codex"])
        self.assertEqual((rc, out["outcome"], out["fell_back"]), (1, "error", "codex"))
        self.assertEqual(self.fake.edits()[-1], ("$card1", "Runtime: " + out["say"]))
        self.assertNotIn("runtime card", out["say"])
        return out

    def test_a_failed_restart_relaunches_the_previous_runtime_and_says_so_on_the_card(self):
        out = self.failing_switch(back_ok=True)
        self.assertEqual(out["say"], "Couldn't switch to Claude Code: the restart did not finish cleanly (exit 1). "
                                     "I started Codex again, and the core is running on it.")
        self.assertTrue(out["fell_back_confirmed"])

    def test_a_relaunch_that_stopped_nothing_says_the_core_is_still_on_the_old_runtime(self):
        for code in rtc.NOT_RESTARTED:
            with self.subTest(rc=code):
                self.restarts.clear()
                self.ws.joinpath(*rtc.RUNNING).write_text(json.dumps(
                    {"attempt": self.attempt, "runtime": "claude", "room": DM, "ts": self.clock.t}))
                out = self.failing_switch(back_ok=False, back_rc=code)
                self.assertTrue(out["say"].endswith("The core is still on Codex."), out["say"])
                self.assertNotIn("may be down", out["say"])

    def test_a_relaunch_that_fails_too_names_what_to_do_outside_chat(self):
        out = self.failing_switch(back_ok=False)
        self.assertIn("Starting Codex again did not come back either, so the core may be down.", out["say"])
        self.assertIn("Quit and reopen the app", out["say"])
        self.assertFalse(out["fell_back_confirmed"])

    def test_a_restart_that_did_not_happen_says_still_on_the_old_runtime(self):
        # 3 and 5 end here at once; 4 (put off) has its own tests below.
        for code in (c for c in rtc.NOT_RESTARTED if c != rtc.DEFERRED):
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
        self.assertIn("Switched to Claude Code. The core restarted on it", said[-1])
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
        self.assertIn("Switched to Claude Code. The core restarted on it", said[-1])

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
                self.assertEqual(self.fake.edits()[-1], ("$card1", "Runtime: " + out["say"]))
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
        self.assertEqual(self.fake.edits()[-1], ("$card1", "Runtime: " + out["say"]))
        self.assertFalse(self.ws.joinpath(*rtc.RUNNING).exists(), "the attempt is released: she can tap again")
        self.assertGreater(rtc.RUNNING_STALE_S, 2 * rtc.RESTART_TIMEOUT_S + rtc.DEFER_WAIT_S)

    def test_a_lock_gone_before_its_id_could_be_read_identifies_no_result_however_recent(self):
        # The other restart ended before its lock was looked at, so no id was read. A published result
        # from this same second, a later one or an old one is somebody's, not provably its: none is taken.
        self.record("codex", T0 - 500)
        result = self.ws.joinpath(*rtc.RESTART_RESULT)
        result.parent.mkdir(parents=True, exist_ok=True)
        for at in (int(self.clock.t), int(self.clock.t) + 5, int(self.clock.t) - 5000):
            with self.subTest(at=at - int(self.clock.t)):
                self.restarts.clear()
                self.restart_rc = rtc.DEFERRED
                result.write_text(json.dumps({"rid": "grp-9-9", "runtime": "codex", "published": True, "at": at}))
                rc, out = self.carded()
                self.assertEqual((self.restarts, out["why"], out["restart"]), (["claude"], "inconclusive", "not-restarted"))
                self.ws.joinpath(*rtc.RUNNING).write_text(json.dumps(
                    {"attempt": self.attempt, "runtime": "claude", "room": DM, "ts": 1e12}))
        self.assertEqual(rtc.restart_left(self.ws, ""), "")
        self.assertEqual(rtc.restart_left(self.ws, "grp-9-9"), "codex")

    def gateway_ctl(self, rc=0, raises=None):
        """A desktop's control script beside the engine; `calls` gets each run of it."""
        if self.no_gateway is not None:
            self.no_gateway.stop()
            self.no_gateway = None
        ctl = self.ws / "sutando-ctl.sh"
        ctl.write_text("#!/bin/bash\n")
        patch = mock.patch.object(rtc, "DESKTOP_CTL", ctl)
        patch.start()
        self.addCleanup(patch.stop)
        calls = []

        def run(argv, **kw):
            if argv[:1] == ["claude"]:
                return self.claude_run(argv, **kw)
            calls.append((argv, kw))
            if raises:
                raise raises
            return mock.Mock(returncode=rc)
        self.claude_run_real, self.gateway_run = self.claude_run, run
        return ctl, calls

    def switch_with(self, run):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rtc.main(["--workspace", str(self.ws), "switch", "--room", DM, "--runtime", "claude",
                           "--attempt", self.attempt, "--card", "$card1"], core=CORE, run=run,
                          execute=lambda *a, **k: self.fake.execute(*a, **k), restart=self.restart,
                          sleep=self.clock.sleep, clock=self.clock)
        return rc, json.loads(out.getvalue())

    def test_a_desktop_hosted_core_gets_its_gateway_window_back_after_the_switchs_restart(self):
        ctl, calls = self.gateway_ctl()
        rc, out = self.switch_with(self.gateway_run)
        self.assertEqual((rc, out["outcome"], out["gateway"]), (0, "done", "restored"))
        (argv, kw), = calls
        self.assertEqual((argv, kw["cwd"]), (["bash", str(ctl), "restart-gateway"], str(ctl.parent)))
        self.assertNotIn("SUTANDO_CORE_RUNTIME", kw["env"])
        self.assertNotIn("gateway", self.fake.edits()[-1][1], "a gateway that came back is not news")

    def test_a_gateway_that_does_not_come_back_is_said_on_the_card(self):
        for rc_ctl, raises, why in ((3, None, "exit 3"), (0, subprocess.TimeoutExpired("ctl", 1), "timed out"),
                                    (0, OSError("gone"), "OSError")):
            with self.subTest(why=why):
                ctl, calls = self.gateway_ctl(rc=rc_ctl, raises=raises)
                self.current = self.selected = "codex"
                rc, out = self.switch_with(self.gateway_run)
                self.assertEqual((out["outcome"], out["gateway"]), ("info", why))
                self.assertIn("Switched to Claude Code. The core restarted on it. Tasks", out["say"])
                self.assertIn(f"Messages may not reach the core until the app is quit and reopened: its gateway did not come back ({why}).",
                              out["say"])
                self.assertEqual(self.fake.edits()[-1], ("$card1", "Runtime: " + out["say"]))
                self.ws.joinpath(*rtc.RUNNING).write_text(json.dumps(
                    {"attempt": self.attempt, "runtime": "claude", "room": DM, "ts": 1e12}))

    def test_a_desktop_whose_control_script_is_missing_is_not_a_recovered_gateway_and_the_card_says_so(self):
        ctl, calls = self.gateway_ctl()
        ctl.unlink()
        rc, out = self.switch_with(self.gateway_run)
        self.assertEqual((calls, out["outcome"], out["gateway"]), ([], "info", "the desktop's control script is missing"))
        self.assertIn("Switched to Claude Code. The core restarted on it. Tasks", out["say"])
        self.assertIn("its gateway did not come back (the desktop's control script is missing).", out["say"])
        self.assertEqual(self.fake.edits()[-1], ("$card1", "Runtime: " + out["say"]))

    def test_the_gateway_is_left_alone_with_no_desktop_or_no_restart_of_ours(self):
        ctl, calls = self.gateway_ctl()
        self.desktop = False
        rc, out = self.switch_with(self.gateway_run)                    # not hosted by a desktop
        self.assertEqual((calls, "gateway" in out), ([], False))
        self.desktop = True
        self.ws.joinpath(*rtc.RUNNING).write_text(json.dumps(
            {"attempt": self.attempt, "runtime": "claude", "room": DM, "ts": 1e12}))
        self.current = self.selected = "codex"
        self.restart_rc = 3                                             # the restart never happened
        rc, out = self.switch_with(self.gateway_run)
        self.assertEqual((calls, out["restart"]), ([], "not-restarted"))

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
        self.assertEqual(self.fake.edits()[-1], ("$card1", "Runtime: " + out["say"]))
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
