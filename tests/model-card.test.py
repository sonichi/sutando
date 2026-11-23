#!/usr/bin/env python3
"""The core's model card (skills/model-switch/scripts/model_card.py): menu, tap, and the outcome it owes.

- The menu's buttons are the skill manifest's MODEL_SWITCH_CHOICES, in its order and under its titles;
  a button carries a slug that maps back to the exact id ("sonnet[1m]"), nothing is built in, and the
  model in use is read only from <workspace>/state/model-switch.json ("the default model" with no record).
- `reply` starts a switch only for the owner's tap on a card this agent posted: the detached line runs
  switch-model.sh and then `settle` with its exit code, and the answer is the "switching" card.
- `settle` always posts an outcome with the menu's card id: "Switched. I'm now using X." on exit 0, else
  "I could not switch to X. I'm still using Y. <reason>" for every exit code the script documents.
- A tap on the model already in use changes nothing.
- One tap is one attempt: its "switching" card is posted before its switch is started, its outcome is
  saved before it is posted, a failed post is tried again, an outcome still untold is posted first by
  the next `post` or `reply`, an outcome is told once, and no retry starts the switch again.
- An untold outcome with a newer attempt after it is marked superseded and never posted.
- A tap is resolved through the ids its own card was built with and acts only while the list still has
  that id; slugs are unique on a card; an id that is not a model name or a title too long is left out.

The Action call, the core's row and the process launch are fakes; no network, no tmux, no real switch.

Run: python3 tests/model-card.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "model-switch" / "scripts" / "model_card.py"
_spec = importlib.util.spec_from_file_location("model_card", SCRIPT)
mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc)
ac = mc.ac

import connections  # noqa: E402

DM, ROOM, OWNER = "!dm:s.space", "!team:s.space", "@owner:s.space"
CORE = {"instance": "s_dspace", "owner_mxid": OWNER, "agent_mxid": "@owner-mini.agent:s.space"}
SIMPLE = "opus=Opus;sonnet=Sonnet;fable=Fable;default=Default"
SHIPPED = "default=Default (auto-fallback);opus=Opus;sonnet[1m]=Sonnet (1M context);haiku=Haiku;fable[1m]=Fable (1M context)"
SAID = {entry.split("=")[0]: "Switch the core to " + entry.split("=")[1] for entry in (SIMPLE + ";" + SHIPPED).split(";")}


def task(said, *, tier="owner", user=OWNER, kind="dm", room=DM, **extra):
    head = {"id": "task-1", "task_layout": "mid"}
    tail = {"source": "ag2space", "channel_id": room, "channel_kind": kind, "user_id": user, "access_tier": tier,
            "source_message_id": "$ask", "task_id": "task-1", **extra}
    return "\n".join([f"{k}: {v}" for k, v in head.items()] + [f"task: {said}"]
                     + [f"{k}: {v}" for k, v in tail.items()]) + "\n"


class Fake:
    """The Action call: every send is kept and answered with the next event id."""

    def __init__(self):
        self.calls = []

    def execute(self, action, arguments, *, operation_id, room_id):
        self.calls.append((action, arguments, operation_id, room_id))
        return {"result": {"event_id": f"$card{len(self.calls)}"}}

    def cards(self):
        return [c[1]["extra_content"][ac.CARD_KEY] for c in self.calls]


class Launch:
    """The process launch: what would have been started, and nothing started."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return mock.Mock(pid=4242)


class Base(unittest.TestCase):
    CHOICES = SIMPLE
    RUNTIME = "claude"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.fake, self.launch, self.sleeps = Fake(), Launch(), []
        self.nap = self.sleeps.append
        self.manifest = self.ws / "manifest.json"
        self.choose(self.CHOICES)
        for target, name, value in ((mc, "_owner_dm", lambda ws, core: DM), (mc, "MANIFEST", self.manifest),
                                    (mc, "core_runtime", lambda: self.RUNTIME),
                                    (connections, "read_rows", lambda ws=None: [CORE])):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def choose(self, raw):
        self.manifest.write_text(json.dumps({"name": "model-switch", "config": {mc.CHOICES_KEY: raw}}))

    def using(self, model, **more):
        path = self.ws / "state" / "model-switch.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": model, "ts": "2026-11-22T00:00:00Z", **more}))

    def run_(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mc.main(["--workspace", str(self.ws)] + argv, core=CORE, popen=self.launch,
                         execute=lambda *a, **k: self.fake.execute(*a, **k), sleep=lambda s: self.nap(s))
        return rc, json.loads(out.getvalue())

    def owed(self):
        return [r["attempt"] for r in mc.outcomes(self.ws)
                if not r["told"] and not r.get("superseded") and r["state"] != "started"]

    def superseded(self):
        return [r["attempt"] for r in mc.outcomes(self.ws) if r.get("superseded")]

    def tap(self, alias, *, said=None, event="$card1", card_id=mc.CARD_ID, **kw):
        said = SAID[alias] if said is None else said
        kw.setdefault("reply_to_event", event)
        kw.setdefault("card_click", json.dumps({"card_id": card_id, "action": said}))
        path = self.ws / "task.txt"
        path.write_text(task(said, **kw))
        return self.run_(["reply", "--task-file", str(path)])


class Menu(Base):
    def test_one_reply_button_per_model_and_the_model_in_use(self):
        self.using("opus")
        body, card = mc.menu_card("opus", mc.choices())
        self.assertIsNone(ac.problem(card))
        self.assertEqual((card["id"], card["state"]), (mc.CARD_ID, "info"))
        self.assertEqual([(b["label"], b["kind"], b["reply"], b["command"], b["style"]) for b in card["buttons"]],
                         [("Opus", "reply", "Switch the core to Opus", "card:model:core:opus", "primary"),
                          ("Sonnet", "reply", "Switch the core to Sonnet", "card:model:core:sonnet", "secondary"),
                          ("Fable", "reply", "Switch the core to Fable", "card:model:core:fable", "secondary"),
                          ("Default", "reply", "Switch the core to Default", "card:model:core:default", "secondary")])
        self.assertEqual(card["text"], "I'm using Opus. Pick a model to switch to.")
        self.assertIn("I'm using Opus.", body)

    def test_no_record_is_the_default_model(self):
        self.assertEqual(self.run_(["status"]), (0, {"model": None, "name": "the default model", "source": None, "ts": None}))
        _, card = mc.menu_card(mc.current_model(self.ws)["model"], mc.choices())
        self.assertEqual(card["text"], "I'm using the default model. Pick a model to switch to.")
        self.assertEqual({b["style"] for b in card["buttons"]}, {"secondary"})
        (self.ws / "state").mkdir()
        (self.ws / "state" / "model-switch.json").write_text("not json")
        self.assertEqual(mc.current_model(self.ws)["name"], "the default model")
        (self.ws / "state" / "model-switch.json").write_text("[]")
        self.assertEqual(mc.current_model(self.ws)["name"], "the default model")

    def test_status_reads_the_switch_record(self):
        self.using("claude-opus-4-8[1m]")
        self.assertEqual(self.run_(["status"]), (0, {"model": "claude-opus-4-8[1m]", "name": "claude-opus-4-8[1m]",
                                                     "source": "model-switch.json", "ts": "2026-11-22T00:00:00Z"}))

    def test_print_posts_nothing(self):
        rc, out = self.run_(["post", "--room", DM, "--print"])
        self.assertEqual((rc, out["handler"], out["card"]["id"]), (0, mc.HANDLER, mc.CARD_ID))
        self.assertNotIn("\n", out["body"])
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(mc.issued_cards.ledger_path(self.ws).exists())

    def test_post_sends_to_the_owner_dm_and_records_the_card(self):
        rc, out = self.run_(["post", "--room", DM, "--reply-to", "$ask"])
        self.assertEqual((rc, out), (0, {"status": "posted", "event_id": "$card1"}))
        action, arguments, _, room = self.fake.calls[0]
        self.assertEqual((action, room, arguments["reply_to"]), ("room.message.send", DM, "$ask"))
        entry = mc.issued_cards.live(self.ws, "$card1")
        self.assertEqual((entry["card_id"], entry["room"], entry["lane"], entry["handler"]),
                         (mc.CARD_ID, DM, "s_dspace", "skills/model-switch/scripts/model_card.py reply"))
        self.assertRegex(entry["handler"], ac._HANDLER_RE)
        self.assertEqual(entry["offers"][SAID["fable"]], "card:model:core:fable")

    def test_post_refuses_any_room_but_the_owner_dm_and_names_a_missing_core(self):
        rc, out = self.run_(["post", "--room", ROOM])
        self.assertEqual((rc, out["status"]), (1, "failed"))
        self.assertEqual(self.fake.calls, [])
        out = io.StringIO()
        with mock.patch.object(mc, "_core", side_effect=RuntimeError("no core lane")), contextlib.redirect_stdout(out):
            rc = mc.main(["--workspace", str(self.ws), "post", "--room", DM])
        self.assertEqual((rc, json.loads(out.getvalue())["why"]), (1, "RuntimeError: no core lane"))


class Tap(Base):
    def setUp(self):
        super().setUp()
        self.using("opus")
        self.assertEqual(self.run_(["post", "--room", DM])[0], 0)

    def test_the_owner_tap_starts_the_detached_switch_and_answers_switching(self):
        rc, out = self.tap("fable")
        say = "Switching the core to Fable; it takes effect after this turn. I'll confirm here once it has switched."
        attempt = out.pop("attempt")
        self.assertRegex(attempt, r"^[0-9]+-[0-9a-f]{8}$")
        self.assertEqual((rc, out), (0, {"status": "started", "model": "fable", "pid": 4242, "say": say,
                                         "event_id": "$card2"}))
        (argv, kw), = self.launch.calls
        self.assertEqual(argv[:3], ["nohup", "bash", "-c"])
        self.assertEqual(argv[4:], ["model-card", str(mc.SWITCH), "fable", "180", str(self.ws / "state"),
                                    sys.executable, str(SCRIPT), str(self.ws), DM, attempt])
        self.assertEqual(self.fake.calls[1][2], f"model-card:{attempt}:switching")
        self.assertTrue(mc.SWITCH.is_file())
        self.assertIn('bash "$1" "$2" --confirm --accept-timeout "$3" --state-dir "$4"; ', argv[3])
        self.assertTrue(argv[3].endswith('"$5" "$6" --workspace "$7" settle --room "$8" --model "$2" --attempt "$9" --rc $?'))
        self.assertNotIn("&&", argv[3], "the outcome is posted whatever the switch's exit code")
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        card = self.fake.cards()[1]
        self.assertEqual((card["id"], card["state"], card["text"]), (mc.NOTE_ID, "pending", say))
        self.assertNotIn("buttons", card)
        self.assertEqual(self.fake.calls[1][1]["reply_to"], "$card1")
        self.assertIn("switch to fable", (self.ws / "logs" / "model-card.log").read_text())
        # The menu is still on the owner's screen after the card that answered it, and still acts.
        self.assertIsNotNone(mc.issued_cards.live(self.ws, "$card1"), "the answered menu keeps acting")
        self.assertEqual(self.tap("sonnet")[1]["status"], "started")
        self.assertEqual(len(self.launch.calls), 2)

    def test_the_detached_line_passes_the_switch_exit_code_to_settle(self):
        """The line itself, run for real against stand-in scripts: `settle` gets the switch's own code."""
        fake_switch = self.ws / "switch.sh"
        fake_switch.write_text('echo "switch $*"; exit 8\n')
        fake_py = self.ws / "py.sh"
        fake_py.write_text('#!/bin/sh\necho "settle $*"\n')
        fake_py.chmod(0o755)
        got = subprocess.run(["bash", "-c", mc.DETACHED, "model-card", str(fake_switch), "fable", "180", "/st",
                              str(fake_py), "card.py", "/w s", DM, "17-ab"], capture_output=True, text=True, check=True)
        self.assertEqual(got.stdout.splitlines(),
                         ["switch fable --confirm --accept-timeout 180 --state-dir /st",
                          f"settle card.py --workspace /w s settle --room {DM} --model fable --attempt 17-ab --rc 8"])

    def test_a_tap_that_is_not_the_owners_on_this_card_starts_nothing(self):
        for kw in ({"user": "@x:s.space"}, {"tier": "team"}, {"room": ROOM, "kind": "room"}, {"event": "$other"},
                   {"card_id": "look-alike"}, {"said": "Switch the core to Haiku"}, {"card_click": "{"}):
            rc, out = self.tap("fable", **kw)
            self.assertEqual((rc, out["status"], out["say"]), (1, "forged_or_stale_click", ac.UNMATCHED_CLICK), kw)
        rc, out = self.tap("fable", card_click="")
        self.assertEqual((rc, out["status"], out["say"]), (1, "forged_or_stale_click", ac.UNTAPPED_ACTION))
        rc, out = self.tap("fable", said="what model are you on?", card_click="")
        self.assertEqual((rc, out), (1, {"status": "owner_words", "words": "what model are you on?"}))
        rc, out = self.tap("fable", said="hello", card_click="", reply_to_event="")
        self.assertEqual((rc, out), (1, {"status": "not_a_card"}))
        self.assertEqual(self.launch.calls, [])
        self.assertEqual(len(self.fake.calls), 1, "only the menu was ever posted")

    def test_another_cards_command_and_an_unknown_model_start_nothing(self):
        _, card = ac.build(mc.CARD_ID, "info", title="t", body="b", buttons=[
            ac.button("a", "reply", reply="Do a", command="card:workers:w1:pause"),
            ac.button("b", "reply", reply="Do b", command="card:model:core:haiku"),
            ac.button("c", "reply", reply="Do c", command="card:model:worker:opus")])
        mc.issued_cards.record(self.ws, "$card1", card, room=DM, lane="s_dspace", handler="skills/agent-cards/scripts/cards.py reply")
        rc, out = self.tap("fable", said="Do a")
        self.assertEqual((rc, out), (1, {"status": "other_card", "card": "workers",
                                         "run": "skills/agent-cards/scripts/cards.py reply"}))
        for said in ("Do b", "Do c"):
            rc, out = self.tap("fable", said=said)
            self.assertEqual((rc, out["status"]), (2, "refused"))
        self.assertEqual(self.launch.calls, [])

    def test_the_model_already_in_use_changes_nothing(self):
        rc, out = self.tap("opus")
        self.assertEqual((rc, out), (0, {"status": "say", "say": "I'm already using Opus. Nothing changed."}))
        self.assertEqual(self.launch.calls, [])
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIsNotNone(mc.issued_cards.live(self.ws, "$card1"), "the menu still acts")

    def test_a_launch_that_fails_says_so_and_a_card_that_cannot_post_leaves_the_words(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mc.main(["--workspace", str(self.ws), "reply", "--task-file", str(self.task_file("fable"))],
                         core=CORE, execute=self.fake.execute, popen=mock.Mock(side_effect=OSError("no nohup")))
        got = json.loads(out.getvalue())
        self.assertEqual((rc, got["status"]), (1, "failed"))
        self.assertIn("no nohup", got["why"])
        self.assertEqual(got["say"], "I could not switch to Fable. I'm still using Opus. The switch did not finish.")
        self.assertEqual([(c["state"], c["text"]) for c in self.fake.cards()[1:]],
                         [("pending", mc.switching_words("fable")), ("error", got["say"])],
                         "the switching card is followed at once by what happened")
        self.assertEqual([c[2] for c in self.fake.calls[1:]],
                         [f"model-card:{got['attempt']}:switching", f"model-card:{got['attempt']}:outcome"])
        self.assertEqual(self.owed(), [])
        self.assertEqual(self.run_(["post", "--room", DM])[0], 0)
        self.fake.execute = lambda *a, **k: {"is_error": True, "code": "M_LIMIT"}
        rc, got = self.tap("fable", event="$card4")
        self.assertEqual((rc, got["status"], got["say"]), (0, "started", mc.switching_words("fable")))
        self.assertNotIn("event_id", got)
        self.assertIn("M_LIMIT", got["card_why"])
        self.assertEqual(len(self.launch.calls), 1, "the switch still starts; its outcome will say how it ended")

    def task_file(self, alias):
        path = self.ws / "task.txt"
        path.write_text(task(SAID[alias], reply_to_event="$card1",
                             card_click=json.dumps({"card_id": mc.CARD_ID, "action": SAID[alias]})))
        return path

    def test_an_unreadable_task_file_is_refused(self):
        rc, out = self.run_(["reply", "--task-file", str(self.ws / "missing.txt")])
        self.assertEqual((rc, out["status"]), (2, "refused"))


class Settle(Base):
    def settle(self, alias, rc, room=DM):
        return self.run_(["settle", "--room", room, "--model", alias, "--rc", str(rc)])

    def test_success_posts_the_done_card(self):
        self.using("fable")
        rc, out = self.settle("fable", 0)
        attempt = out.pop("attempt")
        self.assertEqual((rc, out), (0, {"status": "posted", "event_id": "$card1", "outcome": "done",
                                         "say": "Switched. I'm now using Fable."}))
        self.assertEqual(self.fake.calls[0][2], f"model-card:{attempt}:outcome")
        card, = self.fake.cards()
        self.assertEqual((card["id"], card["state"], card["text"]), (mc.NOTE_ID, "done", "Switched. I'm now using Fable."))
        self.assertEqual(self.fake.calls[0][3], DM)
        self.assertEqual(self.settle("default", 0)[1]["say"], "Switched. I'm now using Default.")

    def test_every_documented_failure_posts_the_error_card_with_its_reason(self):
        self.using("opus")
        documented = sorted(mc.REASONS)
        self.assertEqual(documented, list(range(1, 12)))
        switch = mc.SWITCH.read_text()
        for code in documented:
            self.assertTrue(f"exit {code}" in switch, f"switch-model.sh no longer exits {code}")
            rc, out = self.settle("fable", code)
            reason = mc.REASONS[code]
            want = f"I could not switch to Fable. I'm still using Opus. {reason[0].upper()}{reason[1:]}."
            self.assertEqual((rc, out["outcome"], out["say"]), (0, "error", want), code)
            card = self.fake.cards()[-1]
            self.assertEqual((card["id"], card["state"], card["text"]), (mc.NOTE_ID, "error", want))
            self.assertNotIn("buttons", card)
        self.assertEqual(len(set(mc.REASONS.values())), len(documented))
        self.assertIn("in time", mc.REASONS[8])
        self.assertIn("input box", mc.REASONS[5])
        self.assertIn("not confirmed", mc.REASONS[6])

    def test_an_unknown_exit_code_and_no_record(self):
        rc, out = self.settle("sonnet", 137)
        self.assertEqual(out["say"], "I could not switch to Sonnet. I'm still using the default model. "
                                     "The switch did not finish.")
        self.assertEqual(self.fake.cards()[0]["state"], "error")

    def test_a_record_the_cli_never_accepted_does_not_count_as_in_use_after_a_failure(self):
        self.using("fable", accepted=False, previous="sonnet")
        self.assertIn("I'm still using Sonnet. ", self.settle("fable", 10)[1]["say"])
        self.using("fable", accepted=False, previous=None)
        self.assertIn("I'm still using the default model. ", self.settle("fable", 10)[1]["say"])

    def test_the_outcome_leaves_the_menu_acting_and_goes_only_to_the_owner_dm(self):
        self.assertEqual(self.run_(["post", "--room", DM])[0], 0)
        self.assertIsNotNone(mc.issued_cards.live(self.ws, "$card1"))
        self.assertEqual(self.settle("fable", 8)[0], 0)
        self.assertEqual(self.fake.cards()[-1]["id"], mc.NOTE_ID)
        self.assertIsNotNone(mc.issued_cards.live(self.ws, "$card1"), "an outcome card does not retire the menu")
        # A newer menu does.
        self.assertEqual(self.run_(["post", "--room", DM])[0], 0)
        self.assertEqual((mc.issued_cards.live(self.ws, "$card1"), mc.issued_cards.live(self.ws, "$card3") is not None),
                         (None, True))
        sent = len(self.fake.calls)
        rc, out = self.settle("fable", 0, room=ROOM)
        self.assertEqual((rc, out["status"], out["outcome"]), (1, "failed", "done"))
        self.assertEqual(len(self.fake.calls), sent, "nothing is posted outside the owner's DM")
        self.assertEqual(self.sleeps, [mc.SETTLE_PAUSE_S] * (mc.SETTLE_TRIES - 1))


class Attempts(Base):
    """One tap is one attempt: switching card, then the switch, then one outcome that is never lost."""

    DOWN = {"is_error": True, "code": "M_UNAVAILABLE"}

    def setUp(self):
        super().setUp()
        self.using("opus")
        self.assertEqual(self.run_(["post", "--room", DM])[0], 0)

    def settle(self, attempt, rc=8, alias="fable"):
        return self.run_(["settle", "--room", DM, "--model", alias, "--rc", str(rc), "--attempt", attempt])

    def failing(self, times):
        """The Action call refuses its next `times` sends, then works again."""
        real, left = self.fake.execute, [times]

        def execute(*a, **k):
            if left[0] > 0:
                left[0] -= 1
                return self.DOWN
            return real(*a, **k)
        self.fake.execute = execute

    def test_the_switching_card_is_posted_before_the_switch_is_started(self):
        order = []
        real = self.fake.execute
        self.fake.execute = lambda *a, **k: order.append("card " + k["operation_id"].rsplit(":", 1)[-1]) or real(*a, **k)
        self.launch = lambda argv, **kw: order.append("start") or mock.Mock(pid=7)
        rc, out = self.tap("fable")
        self.assertEqual((rc, out["status"], order), (0, "started", ["card switching", "start"]))

    def test_a_quick_refusal_is_posted_after_the_switching_card_under_the_attempts_ids(self):
        settled = []

        def launch(argv, **kw):
            """A switch that is refused at once: `settle` runs before `reply` has returned."""
            with contextlib.redirect_stdout(io.StringIO()):
                settled.append(mc.main(["--workspace", argv[11], "settle", "--room", argv[12], "--model", argv[6],
                                        "--attempt", argv[13], "--rc", "5"], core=CORE, execute=self.fake.execute))
            return mock.Mock(pid=7)
        self.launch = launch
        rc, out = self.tap("fable")
        attempt = out["attempt"]
        self.assertEqual((rc, out["status"], settled), (0, "started", [0]))
        self.assertEqual([c[2] for c in self.fake.calls[1:]],
                         [f"model-card:{attempt}:switching", f"model-card:{attempt}:outcome"])
        self.assertEqual([c["state"] for c in self.fake.cards()[1:]], ["pending", "error"])
        self.assertIn("unsent text in the core's input box", self.fake.cards()[-1]["text"])
        self.assertEqual([(r["attempt"], r["told"], r["room"], r["model"], r["state"]) for r in mc.outcomes(self.ws)],
                         [(attempt, True, DM, "fable", "error")])

    def test_a_temporary_send_failure_is_retried_and_posts_once(self):
        self.failing(2)
        rc, out = self.settle("17-ab")
        self.assertEqual((rc, out["status"], out["event_id"], out["attempt"]), (0, "posted", "$card2", "17-ab"))
        self.assertEqual(self.sleeps, [mc.SETTLE_PAUSE_S, mc.SETTLE_PAUSE_S])
        self.assertEqual([c[2] for c in self.fake.calls[1:]], ["model-card:17-ab:outcome"])
        self.assertEqual((self.owed(), self.launch.calls), ([], []))

    def test_the_outcome_is_saved_before_the_first_send(self):
        seen = []
        self.fake.execute = lambda *a, **k: seen.append(self.owed()) or self.DOWN
        self.assertEqual(self.settle("17-ab")[0], 1)
        self.assertEqual(seen, [["17-ab"]] * mc.SETTLE_TRIES)

    def test_settle_twice_for_one_attempt_posts_once(self):
        self.assertEqual(self.settle("17-ab", 8)[0], 0)
        rc, out = self.settle("17-ab", 0)
        self.assertEqual((rc, out["status"], out["event_id"], out["outcome"]), (0, "already_told", "$card2", "error"))
        self.assertIn("did not accept the switch in time", out["say"])
        self.assertEqual(len(self.fake.calls), 2)

    def test_an_outcome_that_could_not_be_sent_is_kept_and_the_next_post_sends_it_first(self):
        self.failing(mc.SETTLE_TRIES)
        rc, out = self.settle("17-ab")
        self.assertEqual((rc, out["status"], out["outcome"]), (1, "failed", "error"))
        self.assertIn("M_UNAVAILABLE", out["why"])
        self.assertEqual((self.owed(), len(self.sleeps), len(self.fake.calls)), (["17-ab"], mc.SETTLE_TRIES - 1, 1))
        rc, out = self.run_(["post", "--room", DM])
        self.assertEqual((rc, out), (0, {"status": "posted", "event_id": "$card3", "delivered": ["17-ab"]}))
        self.assertEqual(self.fake.calls[1][2], "model-card:17-ab:outcome")
        self.assertIn("did not accept the switch in time", self.fake.cards()[1]["text"])
        self.assertEqual(self.fake.cards()[2]["state"], "info", "then the menu it was asked for")
        self.assertIsNotNone(mc.issued_cards.live(self.ws, "$card3"))
        self.assertEqual((self.owed(), self.superseded()), ([], []))
        self.assertEqual(self.run_(["post", "--room", DM])[1], {"status": "posted", "event_id": "$card4"})
        self.assertEqual(self.launch.calls, [], "no retry runs the switch")

    def test_an_owed_outcome_is_never_posted_once_a_newer_attempt_has_started(self):
        self.failing(mc.SETTLE_TRIES)
        self.assertEqual(self.settle("17-ab")[0], 1)
        self.assertEqual(self.owed(), ["17-ab"])
        rc, out = self.tap("sonnet")
        newer = out["attempt"]
        self.assertEqual((rc, out["status"]), (0, "started"))
        self.assertNotIn("delivered", out)
        self.assertEqual([c[2] for c in self.fake.calls[1:]], [f"model-card:{newer}:switching"])
        self.assertEqual((self.owed(), self.superseded()), ([], ["17-ab"]))
        self.assertEqual([(r["attempt"], r["state"]) for r in mc.outcomes(self.ws)], [("17-ab", "error"), (newer, "started")])
        rc, out = self.run_(["post", "--room", DM])
        self.assertEqual((rc, out), (0, {"status": "posted", "event_id": "$card3"}))
        rc, out = self.settle("17-ab")
        self.assertEqual((rc, out["status"], self.sleeps[mc.SETTLE_TRIES - 1:]), (0, "superseded", []))
        self.using("sonnet")
        rc, out = self.settle(newer, 0, "sonnet")
        self.assertEqual((rc, out["status"], out["say"]), (0, "posted", "Switched. I'm now using Sonnet."))
        self.assertEqual([c[2].split(":")[1] for c in self.fake.calls[1:]], [newer, "menu", newer])
        self.assertFalse(any("17-ab" in c[2] for c in self.fake.calls))
        self.assertEqual(len(self.launch.calls), 1)

    def test_settles_own_retries_stop_when_a_newer_attempt_appears(self):
        sends = []
        self.fake.execute = lambda *a, **k: sends.append(k["operation_id"]) or self.DOWN

        def nap(seconds):
            """While the older outcome waits to try again, the owner taps another model."""
            self.sleeps.append(seconds)
            mc.save_outcome(self.ws, "18-cd", DM, "sonnet", None, mc.time.time())
        self.nap = nap
        rc, out = self.settle("17-ab")
        self.assertEqual((rc, out["status"], out["outcome"]), (0, "superseded", "error"))
        self.assertEqual((sends, self.sleeps), (["model-card:17-ab:outcome"], [mc.SETTLE_PAUSE_S]))
        self.assertEqual(self.superseded(), ["17-ab"])

    def test_a_newer_outcome_supersedes_an_older_one_still_owed(self):
        for attempt in ("17-ab", "18-cd"):
            self.failing(mc.SETTLE_TRIES)
            self.assertEqual(self.settle(attempt, 3 if attempt == "18-cd" else 8)[0], 1)
        self.assertEqual(self.owed(), ["17-ab", "18-cd"])
        rc, out = self.run_(["post", "--room", DM])
        self.assertEqual((rc, out["delivered"]), (0, ["18-cd"]))
        self.assertEqual([c[2] for c in self.fake.calls[1:2]], ["model-card:18-cd:outcome"])
        self.assertIn("The core is not running.", self.fake.cards()[1]["text"])
        self.assertEqual((self.owed(), self.superseded(), len(self.fake.calls)), ([], ["17-ab"], 3))

    def test_an_attempt_that_never_settled_owes_nothing(self):
        rc, out = self.tap("sonnet")
        self.assertEqual(mc.tell(self.ws, out["attempt"], core=CORE, execute=self.fake.execute, now=mc.time.time()),
                         {"status": "failed", "why": "that attempt has no outcome yet"})
        self.assertEqual(self.run_(["post", "--room", DM])[1], {"status": "posted", "event_id": "$card3"})
        self.assertEqual([c["state"] for c in self.fake.cards()], ["info", "pending", "info"])

    def test_a_tap_whose_attempt_cannot_be_recorded_starts_nothing(self):
        import file_lock
        sent = len(self.fake.calls)
        with mock.patch.object(file_lock, "locked_file", side_effect=PermissionError("read-only")):
            rc, out = self.tap("sonnet")
        self.assertEqual((rc, out), (1, {"status": "failed", "say": mc.UNRECORDED,
                                         "why": "the attempt could not be recorded (PermissionError)"}))
        self.assertEqual((self.launch.calls, len(self.fake.calls), mc.outcomes(self.ws)), ([], sent, []),
                         "no switch, no card, no record")

    def test_a_tap_that_starts_nothing_still_sends_an_owed_outcome(self):
        self.failing(mc.SETTLE_TRIES)
        self.assertEqual(self.settle("17-ab")[0], 1)
        rc, out = self.tap("fable", user="@x:s.space")
        self.assertEqual((rc, out["status"], out["delivered"]), (1, "forged_or_stale_click", ["17-ab"]))
        self.failing(mc.SETTLE_TRIES)
        self.assertEqual(self.settle("18-cd")[0], 1)
        self.assertEqual(self.run_(["post", "--room", DM])[1]["delivered"], ["18-cd"])
        self.failing(mc.SETTLE_TRIES)
        self.assertEqual(self.settle("19-ef")[0], 1)
        rc, out = self.tap("opus", event="$card4")
        self.assertEqual((out["say"], out["delivered"]), ("I'm already using Opus. Nothing changed.", ["19-ef"]))
        self.assertEqual(self.launch.calls, [])

    def test_an_outcome_still_refused_stays_owed_and_a_day_old_one_is_dropped(self):
        self.failing(mc.SETTLE_TRIES + 1)
        self.assertEqual(self.settle("17-ab")[0], 1)
        rc, out = self.run_(["post", "--room", DM])
        self.assertEqual((rc, out), (0, {"status": "posted", "event_id": "$card2"}))
        self.assertEqual(self.owed(), ["17-ab"])
        path = self.ws.joinpath(*mc.OUTCOMES)
        rows = json.loads(path.read_text())["outcomes"]
        rows[0]["ts"] -= mc.OUTCOME_TTL_S + 1
        path.write_text(json.dumps({"outcomes": rows}))
        self.assertEqual(mc.tell(self.ws, "17-ab", core=CORE, execute=self.fake.execute, now=rows[0]["ts"] + mc.OUTCOME_TTL_S + 2),
                         {"status": "failed", "why": "no outcome is saved for that attempt"})
        self.assertEqual((mc.outcomes(self.ws), len(self.fake.calls)), ([], 2))
        for text in ("not json", "[]", '{"outcomes": 3}'):
            path.write_text(text)
            self.assertEqual(mc.outcomes(self.ws), [])

    def test_an_outcome_that_cannot_be_saved_posts_no_card(self):
        import file_lock
        sent = len(self.fake.calls)
        with mock.patch.object(file_lock, "locked_file", side_effect=PermissionError("read-only")):
            rc, out = self.settle("17-ab", 0)
            self.assertEqual((rc, out), (1, {
                "status": "failed", "attempt": "17-ab", "outcome": "done", "say": "Switched. I'm now using Fable.",
                "saved": False, "why": "the outcome could not be saved, so its card was not posted (PermissionError)"}))
            self.assertEqual(mc.tell(self.ws, "17-ab", core=CORE, execute=self.fake.execute, now=1.0),
                             {"status": "failed", "why": "the outcome record could not be used (PermissionError)"})
        self.assertEqual((len(self.fake.calls), self.sleeps), (sent, []), "the newer attempt's card is left as it is")

    def test_settle_without_an_attempt_id_makes_one(self):
        rc, out = self.run_(["settle", "--room", DM, "--model", "fable", "--rc", "0"])
        self.assertEqual((rc, out["status"]), (0, "posted"))
        self.assertEqual(self.fake.calls[-1][2], f"model-card:{out['attempt']}:outcome")


class Manifest(Base):
    """The buttons are the manifest's choices; a tap's slug maps back to the manifest's exact id."""

    CHOICES = SHIPPED

    def test_the_buttons_are_the_manifest_choices_in_order(self):
        self.assertEqual(mc.choices(), [("default", "Default (auto-fallback)", "default"), ("opus", "Opus", "opus"),
                                        ("sonnet[1m]", "Sonnet (1M context)", "sonnet-1m"), ("haiku", "Haiku", "haiku"),
                                        ("fable[1m]", "Fable (1M context)", "fable-1m")])
        rc, out = self.run_(["post", "--room", DM, "--print"])
        self.assertEqual([(b["label"], b["command"]) for b in out["card"]["buttons"]],
                         [("Default (auto-fallback)", "card:model:core:default"), ("Opus", "card:model:core:opus"),
                          ("Sonnet (1M context)", "card:model:core:sonnet-1m"), ("Haiku", "card:model:core:haiku"),
                          ("Fable (1M context)", "card:model:core:fable-1m")])
        self.assertEqual([b["reply"] for b in out["card"]["buttons"]], [SAID[m] for m, _, _ in mc.choices()])

    def test_the_shipped_manifest_gives_a_card_the_client_draws(self):
        real = mc.choices(SCRIPT.parents[1] / "manifest.json")
        self.assertGreaterEqual(len(real), 2)
        self.assertIn("default", [m for m, _, _ in real])
        self.assertTrue(all(ac.parse_reply(ac.reply(mc.KIND, mc.TARGET, slug)) for _, _, slug in real))
        _, card = mc.menu_card(real[-1][0], real)
        self.assertIsNone(ac.problem(card))
        self.assertEqual([b["style"] for b in card["buttons"]][-1], "primary")
        self.assertNotIn("MODELS", SCRIPT.read_text(), "no list of models is built into the script")

    def test_a_one_million_model_in_use_is_marked_named_and_not_switched_again(self):
        self.using("fable[1m]")
        rc, out = self.run_(["post", "--room", DM])
        card = self.fake.cards()[0]
        self.assertEqual([b["label"] for b in card["buttons"] if b["style"] == "primary"], ["Fable (1M context)"])
        self.assertEqual(card["text"], "I'm using Fable (1M context). Pick a model to switch to.")
        self.assertNotIn("\\", card["text"] + self.fake.calls[0][1]["body"])
        self.assertEqual(self.run_(["status"])[1]["name"], "Fable (1M context)")
        rc, out = self.tap("fable[1m]")
        self.assertEqual((rc, out), (0, {"status": "say", "say": "I'm already using Fable (1M context). Nothing changed."}))
        self.assertEqual(self.launch.calls, [])

    def test_a_tap_switches_to_the_manifest_id_exactly(self):
        self.using("fable[1m]")
        self.run_(["post", "--room", DM])
        rc, out = self.tap("sonnet[1m]")
        say = ("Switching the core to Sonnet (1M context); it takes effect after this turn. "
               "I'll confirm here once it has switched.")
        self.assertEqual((rc, out["status"], out["model"], out["say"]), (0, "started", "sonnet[1m]", say))
        argv = self.launch.calls[0][0]
        self.assertEqual((argv[6], argv[13]), ("sonnet[1m]", out["attempt"]))
        self.assertEqual(self.fake.cards()[1]["text"], say)
        rc, told = self.run_(["settle", "--room", DM, "--model", argv[6], "--attempt", argv[13], "--rc", "8"])
        self.assertEqual(told["say"], "I could not switch to Sonnet (1M context). I'm still using Fable (1M context). "
                                      "The core did not accept the switch in time.")
        self.assertEqual(self.fake.cards()[2]["text"], told["say"])
        self.using("sonnet[1m]")
        self.assertEqual(self.run_(["settle", "--room", DM, "--model", "sonnet[1m]", "--rc", "0"])[1]["say"],
                         "Switched. I'm now using Sonnet (1M context).")

    def test_the_detached_line_hands_a_bracketed_id_through_unchanged(self):
        fake_switch = self.ws / "switch.sh"
        fake_switch.write_text('echo "switch $1"; exit 0\n')
        fake_py = self.ws / "py.sh"
        fake_py.write_text('#!/bin/sh\necho "settle $8"\n')
        fake_py.chmod(0o755)
        got = subprocess.run(["bash", "-c", mc.DETACHED, "model-card", str(fake_switch), "fable[1m]", "180", "/st",
                              str(fake_py), "card.py", str(self.ws), DM, "17-ab"], cwd=str(self.ws),
                             capture_output=True, text=True, check=True)
        self.assertEqual(got.stdout.splitlines(), ["switch fable[1m]", "settle fable[1m]"])

    def test_a_recorded_id_the_manifest_lacks_marks_no_button_and_is_named_as_recorded(self):
        self.using("fable")
        self.run_(["post", "--room", DM])
        card = self.fake.cards()[0]
        self.assertEqual({b["style"] for b in card["buttons"]}, {"secondary"})
        self.assertEqual(card["text"], "I'm using fable. Pick a model to switch to.")
        self.assertEqual(mc.name_of("claude-fable-9[1m]"), "claude-fable-9[1m]")
        self.assertEqual(mc.name_of("a*b"), "a\\*b", "anything else is shown as literal text")
        rc, out = self.tap("fable[1m]")
        self.assertEqual((rc, out["status"], self.launch.calls[0][0][6]), (0, "started", "fable[1m]"))

    def test_a_slug_the_manifest_no_longer_has_starts_nothing(self):
        self.run_(["post", "--room", DM])
        self.choose("default=Default;opus=Opus")
        rc, out = self.tap("fable[1m]")
        self.assertEqual((rc, out), (2, {"status": "refused", "why": mc.GONE}))
        self.assertEqual((self.launch.calls, len(self.fake.calls)), ([], 1))
        self.assertEqual(mc.GONE, "That choice is no longer on my list. Ask me for the model card again.")

    def test_an_unreadable_or_empty_list_posts_no_menu_and_says_so(self):
        for text in ("not json", "[]", json.dumps({"config": {}}), json.dumps({"config": {mc.CHOICES_KEY: 3}}),
                     json.dumps({"config": {mc.CHOICES_KEY: " ; =x ; y= ; []=Brackets"}})):
            self.manifest.write_text(text)
            self.assertEqual(mc.choices(), [], text)
            for argv in (["post", "--room", DM], ["post", "--room", DM, "--print"]):
                self.assertEqual(self.run_(argv), (1, {"status": "failed", "why": mc.NO_CHOICES}))
        self.manifest.unlink()
        self.assertEqual(self.run_(["post", "--room", DM]), (1, {"status": "failed", "why": mc.NO_CHOICES}))
        self.assertEqual(self.fake.calls, [])
        self.assertIn("can't read the list of models", mc.NO_CHOICES)

    def test_a_list_edited_between_the_card_and_the_tap_never_switches_to_another_id(self):
        self.choose("claude-opus-4.5=Opus;default=Default")
        self.run_(["post", "--room", DM])
        self.assertEqual(mc.issued_choices(self.ws)["$card1"]["ids"], {"claude-opus-4-5": "claude-opus-4.5", "default": "default"})
        self.choose("claude-opus-4-5=Opus;default=Default")
        self.assertEqual(mc.choices()[0], ("claude-opus-4-5", "Opus", "claude-opus-4-5"), "the same slug, another id")
        rc, out = self.tap("opus")
        self.assertEqual((rc, out), (2, {"status": "refused", "why": mc.GONE}))
        self.assertEqual((self.launch.calls, len(self.fake.calls), mc.outcomes(self.ws)), ([], 1, []))
        self.run_(["post", "--room", DM])
        rc, out = self.tap("opus", event="$card2")
        self.assertEqual((rc, out["status"], self.launch.calls[0][0][6]), (0, "started", "claude-opus-4-5"))

    def test_two_ids_with_one_slug_each_get_a_button_and_switch_to_their_own_id(self):
        self.choose("Opus[1m]=Opus big;opus[1m]=Opus small;opus-1m-2=Third;opus = Opus ;opus=Again")
        self.assertEqual(mc.choices(), [("Opus[1m]", "Opus big", "opus-1m"), ("opus[1m]", "Opus small", "opus-1m-2"),
                                        ("opus-1m-2", "Third", "opus-1m-2-2"), ("opus", "Opus", "opus")])
        self.run_(["post", "--room", DM])
        commands = [b["command"] for b in self.fake.cards()[0]["buttons"]]
        self.assertEqual(len(set(commands)), 4)
        for n, (said, model) in enumerate((("Switch the core to Opus big", "Opus[1m]"),
                                           ("Switch the core to Opus small", "opus[1m]"))):
            rc, out = self.tap("opus", said=said, event=f"$card{len(self.fake.calls)}")
            self.assertEqual((rc, out["status"], self.launch.calls[n][0][6]), (0, "started", model))
            self.run_(["post", "--room", DM])

    def test_what_an_issued_menus_buttons_stand_for_is_never_rebound(self):
        # Two menus in one second that look the same and stand for different ids: the send is named
        # for the exact mapping, so the second is not answered with the first's event.
        self.choose("Opus[1m]=Opus")
        self.run_(["post", "--room", DM])
        self.choose("opus[1m]=Opus")
        self.run_(["post", "--room", DM])
        first, second = (c[2].rsplit(":", 1)[1] for c in self.fake.calls)
        self.assertNotEqual(first, second, "whatever the second, the two sends are named apart")
        self.assertEqual([mc.issued_choices(self.ws)[e]["ids"] for e in ("$card1", "$card2")],
                         [{"opus-1m": "Opus[1m]"}, {"opus-1m": "opus[1m]"}])
        # And if a send does come back with an event already kept, its binding stays the first one.
        with mock.patch.object(mc, "_post", return_value={"status": "posted", "event_id": "$card1"}):
            rc, _ = self.run_(["post", "--room", DM])
        self.assertEqual((rc, mc.issued_choices(self.ws)["$card1"]["ids"]), (0, {"opus-1m": "Opus[1m]"}))
        mc.remember_choices(self.ws, "$card1", [("sonnet", "Sonnet", "opus-1m")], 9.0)
        self.assertEqual(mc.issued_choices(self.ws)["$card1"], {"ids": {"opus-1m": "Opus[1m]"}, "ts": mock.ANY})
        self.assertEqual(mc.issuance([("a", "A", "s")]), mc.issuance([("a", "Other title", "s")]), "titles do not count")

    def test_a_title_no_button_can_carry_is_skipped_and_the_rest_is_posted(self):
        self.choose(f"opus=Opus;sonnet={'S' * 81};haiku={'H' * 80};fable=;default=Default")
        rc, out = self.run_(["post", "--room", DM])
        self.assertEqual((rc, out["status"]), (0, "posted"))
        self.assertEqual([b["label"] for b in self.fake.cards()[0]["buttons"]], ["Opus", "H" * 80, "Default"])
        self.assertNotIn("sonnet", mc.issued_choices(self.ws)["$card1"]["ids"])

    def test_an_id_that_is_not_a_model_name_is_never_offered_or_started(self):
        self.choose("--offline=Offline;-x=Dash;opus --offline=Spaced;$(id)=Shell;" + "a" * 81 + "=Long;opus=Opus")
        self.assertEqual(mc.choices(), [("opus", "Opus", "opus")])
        self.run_(["post", "--room", DM])
        self.assertEqual([b["label"] for b in self.fake.cards()[0]["buttons"]], ["Opus"])
        _, card = ac.build(mc.CARD_ID, "info", title="t", body="b", buttons=[
            ac.button("Offline", "reply", reply="Do offline", command="card:model:core:offline")])
        mc.issued_cards.record(self.ws, "$card1", card, room=DM, lane="s_dspace", handler=mc.HANDLER)
        mc.remember_choices(self.ws, "$card1", [("--offline", "Offline", "offline")], 1.0)
        rc, out = self.tap("opus", said="Do offline")
        self.assertEqual((rc, out), (2, {"status": "refused", "why": mc.GONE}))
        self.assertEqual(self.launch.calls, [])

    def test_a_tap_on_a_card_with_no_ids_kept_is_refused(self):
        self.run_(["post", "--room", DM])
        path = self.ws.joinpath(*mc.ISSUED)
        for text in (None, "not json", "[]", '{"cards": 3}', '{"cards": {"$card1": {"ids": 3}}}',
                     '{"cards": {"$card1": {"ids": {"opus": 3}}}}', '{"cards": {"$other": {"ids": {"opus": "opus"}}}}'):
            path.unlink() if text is None else path.write_text(text)
            rc, out = self.tap("opus")
            self.assertEqual((rc, out), (2, {"status": "refused", "why": mc.GONE}), text)
        self.assertEqual((self.launch.calls, len(self.fake.calls)), ([], 1))

    def test_a_menu_whose_ids_cannot_be_kept_says_its_buttons_will_not_act(self):
        with mock.patch.object(mc, "remember_choices", side_effect=PermissionError("read-only")):
            rc, out = self.run_(["post", "--room", DM])
        self.assertEqual((rc, out), (0, {"status": "posted", "event_id": "$card1",
                                         "why": "its buttons will not act (PermissionError)"}))
        self.assertEqual(self.tap("opus")[1], {"status": "refused", "why": mc.GONE})

    def test_only_the_newest_menus_ids_are_kept(self):
        for n in range(mc.ISSUED_KEPT + 2):
            mc.remember_choices(self.ws, f"$m{n}", [("opus", "Opus", "opus")], float(n))
        kept = mc.issued_choices(self.ws)
        self.assertEqual((len(kept), "$m0" in kept, "$m1" in kept, f"$m{mc.ISSUED_KEPT + 1}" in kept),
                         (mc.ISSUED_KEPT, False, False, True))

    def test_more_choices_than_a_card_holds_shows_the_first_six_and_says_to_ask(self):
        self.choose(";".join(f"m{c}=Model {c.upper()}" for c in "abcdefgh"))
        rc, out = self.run_(["post", "--room", DM])
        card = self.fake.cards()[0]
        self.assertEqual((rc, [b["label"] for b in card["buttons"]]), (0, [f"Model {c}" for c in "ABCDEF"]))
        self.assertIsNone(ac.problem(card))
        self.assertEqual(card["text"], "I'm using the default model. Pick a model to switch to. "
                                       "For a model that is not on a button, tell me its name.")
        self.assertNotIn("Switch the core to Model G", mc.issued_cards.live(self.ws, "$card1")["offers"])


class Edges(Base):
    """What a card says when the ledger, the card itself or the core's own row cannot be had."""

    def test_a_card_whose_record_cannot_be_written_is_posted_and_says_its_buttons_will_not_act(self):
        with mock.patch.object(mc.issued_cards, "record", side_effect=PermissionError("read-only")) as record:
            rc, out = self.run_(["post", "--room", DM])
        self.assertEqual((rc, out), (0, {"status": "posted", "event_id": "$card1",
                                         "why": "its buttons will not act (PermissionError)"}))
        self.assertEqual((len(self.fake.calls), record.call_count), (1, 1))
        self.assertIsNone(mc.issued_cards.live(self.ws, "$card1"))

    def test_a_menu_the_client_would_not_draw_is_failed_and_nothing_is_sent(self):
        self.using("m" * 5000)
        for argv in (["post", "--room", DM], ["post", "--room", DM, "--print"]):
            rc, out = self.run_(argv)
            self.assertEqual((rc, out["status"]), (1, "failed"), argv)
            self.assertIn("the card would not render", out["why"])
        self.assertEqual(self.fake.calls, [])

    def test_a_tap_is_judged_by_the_cards_own_lane_when_the_core_row_cannot_be_read(self):
        self.using("opus")
        self.assertEqual(self.run_(["post", "--room", DM])[0], 0)
        self.assertEqual(mc.issued_cards.live(self.ws, "$card1")["lane"], "s_dspace")
        path = self.ws / "task.txt"

        def reply(**kw):
            path.write_text(task(SAID["fable"], reply_to_event="$card1", **kw,
                                 card_click=json.dumps({"card_id": mc.CARD_ID, "action": SAID["fable"]})))
            out = io.StringIO()
            with mock.patch.object(mc, "_core", side_effect=RuntimeError("no core lane")) as core, \
                    contextlib.redirect_stdout(out):
                rc = mc.main(["--workspace", str(self.ws), "reply", "--task-file", str(path)],
                             execute=self.fake.execute, popen=self.launch)
            return rc, json.loads(out.getvalue()), core.call_count

        rc, out, _ = reply(user="@x:s.space")
        self.assertEqual((rc, out["status"], out["why"]), (1, "forged_or_stale_click", "only the owner can use this card"))
        self.assertEqual(self.launch.calls, [])
        rc, out, looked = reply()
        self.assertEqual((rc, out["status"], out["model"]), (0, "started", "fable"))
        self.assertEqual(len(self.launch.calls), 1)
        self.assertEqual((looked, out["card_why"]), (2, "RuntimeError: no core lane"))
        self.assertNotIn("event_id", out)
        self.assertEqual(len(self.fake.calls), 1, "no card is posted as a core that cannot be found")

    def test_without_a_workspace_flag_the_resolved_workspace_is_read(self):
        import workspace_default
        self.using("sonnet")
        out = io.StringIO()
        with mock.patch.object(workspace_default, "resolve_workspace", return_value=str(self.ws)) as resolve, \
                contextlib.redirect_stdout(out):
            rc = mc.main(["status"])
        self.assertEqual((rc, json.loads(out.getvalue())["name"], resolve.call_count), (0, "Sonnet", 1))


class Resolvers(unittest.TestCase):
    """The core's row, its Action call and the owner's DM come from pool_provision and connections."""

    def test_each_default_asks_its_module_with_the_workspace_and_lane(self):
        ws = Path("/nowhere")
        pool = mock.Mock()
        pool.core_lane.return_value = CORE
        pool.core_actions.return_value = (mock.Mock(execute="the action call"), CORE)
        conn = mock.Mock()
        conn.owner_dm.return_value = DM
        with mock.patch.dict(sys.modules, {"pool_provision": pool, "connections": conn}):
            self.assertIs(mc._core(ws), CORE)
            self.assertEqual(mc._execute(ws, "s_dspace"), "the action call")
            self.assertEqual(mc._owner_dm(ws, CORE), DM)
        pool.core_lane.assert_called_once_with(ws, None)
        pool.core_actions.assert_called_once_with(ws, "s_dspace")
        conn.owner_dm.assert_called_once_with(CORE, ws)

    def test_post_uses_them_when_nothing_is_injected(self):
        fake = Fake()
        pool = mock.Mock()
        pool.core_lane.return_value = CORE
        pool.core_actions.return_value = (mock.Mock(execute=fake.execute), CORE)
        conn = mock.Mock()
        conn.owner_dm.return_value = DM
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, {"pool_provision": pool, "connections": conn}), \
                contextlib.redirect_stdout(out):
            rc = mc.main(["--workspace", tmp, "post", "--room", DM])
            entry = mc.issued_cards.live(Path(tmp), "$card1")
        self.assertEqual((rc, json.loads(out.getvalue())), (0, {"status": "posted", "event_id": "$card1"}))
        self.assertEqual((fake.calls[0][3], entry["lane"], entry["room"]), (DM, "s_dspace", DM))
        pool.core_actions.assert_called_once_with(Path(tmp), "s_dspace")


class Source(unittest.TestCase):
    def test_the_model_in_use_never_comes_from_a_settings_file_and_no_path_is_hardcoded(self):
        text = SCRIPT.read_text()
        for banned in ("settings.json", ".claude", "/Users/", "Application Support", "expanduser", "environ"):
            self.assertNotIn(banned, text)
        self.assertEqual(mc.SWITCH, SCRIPT.parent / "switch-model.sh")
        self.assertEqual(mc._REPO, ROOT)

    def test_the_script_runs_on_its_own_from_any_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = subprocess.run([sys.executable, str(SCRIPT), "--workspace", tmp, "status"], cwd=tmp,
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual((got.returncode, json.loads(got.stdout)["name"]), (0, "the default model"), got.stderr)

    def test_a_brain_settings_file_in_the_workspace_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / ".claude-sutando").mkdir()
            (ws / ".claude-sutando" / "settings.json").write_text(json.dumps({"model": "opus"}))
            self.assertEqual(mc.current_model(ws)["name"], "the default model")


class CodexCore(Base):
    """A Codex core's card lists Codex's own models and reads only a Codex record."""

    RUNTIME = "codex"

    def setUp(self):
        super().setUp()
        self.home = self.ws / "codex-home"
        self.home.mkdir()
        (self.home / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-b", "display_name": "GPT B", "visibility": "list", "priority": 3},
            {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide", "priority": 1},
            {"slug": "gpt-a", "display_name": "GPT A", "visibility": "list", "priority": 2},
            {"slug": "opus", "display_name": "Not Codex", "visibility": "list", "priority": 4}]}))
        patch = mock.patch.object(mc, "codex_home", lambda: self.home)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_buttons_are_the_models_codex_lists_by_its_priority(self):
        self.assertEqual(mc.choices(), [("gpt-a", "GPT A", "gpt-a"), ("gpt-b", "GPT B", "gpt-b")])

    def test_the_manifest_codex_list_wins_when_set(self):
        self.manifest.write_text(json.dumps({"name": "model-switch", "config": {
            mc.CHOICES_KEY: SIMPLE, mc.CODEX_CHOICES_KEY: "gpt-x=GPT X"}}))
        self.assertEqual(mc.choices(), [("gpt-x", "GPT X", "gpt-x")])

    def test_only_a_codex_record_names_the_model_in_use(self):
        self.using("opus", accepted=True)
        self.assertIsNone(mc.current_model(self.ws)["model"], "a Claude core's record is not this core's")
        self.using("gpt-a", runtime="codex", accepted=True)
        self.assertEqual(mc.current_model(self.ws)["name"], "GPT A")

    def test_a_seat_pick_is_said_pending_never_switched_and_the_model_in_use_stays_the_working_one(self):
        self.using("gpt-b", runtime="codex", status="pending", working="gpt-a", accepted=False, rev=2)
        state, words = mc.outcome(self.ws, "gpt-b", mc.PENDING_RC)
        self.assertEqual(state, "pending")
        self.assertNotIn("Switched", words)
        self.assertIn("not switched yet", words)
        self.assertIn("I'm still using GPT A", words)
        self.assertEqual(mc.current_model(self.ws)["name"], "GPT A", "the menu marks the working model")
        body, card = mc.notice_card(state, "Pending", words)
        self.assertEqual((card["state"], card["header"]["status"]["label"]), ("pending", "Pending"))

    def test_a_pick_saved_with_no_core_running_is_pending_for_the_next_start(self):
        self.using("gpt-b", runtime="codex", status="pending", working="gpt-a", accepted=False, rev=2,
                   applied="next-start")
        state, words = mc.outcome(self.ws, "gpt-b", mc.NEXT_START_RC)
        self.assertEqual(state, "pending")
        self.assertIn("applies at the next core start", words)
        self.assertNotIn("Switched", words)

    def test_a_refused_pick_names_the_working_model_in_use(self):
        self.using("gpt-b", runtime="codex", status="rejected", working="gpt-a", accepted=False, rev=2)
        self.assertEqual(mc.current_model(self.ws)["name"], "GPT A")

    def test_saved_for_the_next_start_is_said_as_such(self):
        self.using("gpt-a", runtime="codex", accepted=True)
        self.using("gpt-b", runtime="codex", accepted=False, previous="gpt-a")
        state, words = mc.outcome(self.ws, "gpt-b", 11)
        self.assertEqual(state, "error")
        self.assertIn("I'm still using GPT A", words)
        self.assertIn("uses it from its next start", words)


class Runtime(unittest.TestCase):
    """Which core the card is for: `sutando-config.sh core-runtime`, read once; 'claude' when unreadable."""

    def setUp(self):
        mc.core_runtime.cache_clear()
        self.addCleanup(mc.core_runtime.cache_clear)

    def test_the_configured_runtime_else_claude(self):
        for result, want in ((subprocess.CompletedProcess([], 0, "codex\n", ""), "codex"),
                             (subprocess.CompletedProcess([], 1, "codex\n", ""), "claude"),
                             (subprocess.CompletedProcess([], 0, "", ""), "claude"),
                             (OSError("no bash"), "claude"),
                             (subprocess.TimeoutExpired("bash", 20), "claude")):
            mc.core_runtime.cache_clear()
            kw = {"side_effect": result} if isinstance(result, BaseException) else {"return_value": result}
            with mock.patch.object(mc.subprocess, "run", **kw) as run:
                self.assertEqual(mc.core_runtime(), want, result)
            self.assertEqual(run.call_args[0][0][-1], "core-runtime")

    def test_the_codex_home_is_codex_core_models(self):
        with mock.patch.object(mc.codex_core_model, "home", return_value=Path("/x")) as home:
            self.assertEqual(mc.codex_home(), Path("/x"))
        home.assert_called_once_with(mc._REPO)


if __name__ == "__main__":
    unittest.main()
