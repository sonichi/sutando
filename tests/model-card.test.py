#!/usr/bin/env python3
"""The core's model card (skills/model-switch/scripts/model_card.py): menu, tap, and the outcome it owes.

- The menu offers Opus, Sonnet, Fable and Default as reply buttons and names the model in use, read only
  from <workspace>/state/model-switch.json; with no record it says "the default model".
- `reply` starts a switch only for the owner's tap on a card this agent posted: the detached line runs
  switch-model.sh and then `settle` with its exit code, and the answer is the "switching" card.
- `settle` always posts an outcome with the menu's card id: "Switched. I'm now using X." on exit 0, else
  "I could not switch to X. I'm still using Y. <reason>" for every exit code the script documents.
- A tap on the model already in use changes nothing.

The Action call, the core's row and the process launch are fakes; no network, no tmux, no real switch.

Run: python3 tests/model-card.test.py
"""
from __future__ import annotations

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
SAID = {alias: f"Switch the core to {label}" for label, alias in mc.MODELS}


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
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.fake, self.launch = Fake(), Launch()
        for target, name, value in ((mc, "_owner_dm", lambda ws, core: DM),
                                    (connections, "read_rows", lambda ws=None: [CORE])):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def using(self, model, **more):
        path = self.ws / "state" / "model-switch.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"model": model, "ts": "2026-11-22T00:00:00Z", **more}))

    def run_(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mc.main(["--workspace", str(self.ws)] + argv, core=CORE, execute=self.fake.execute,
                         popen=self.launch)
        return rc, json.loads(out.getvalue())

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
        body, card = mc.menu_card("opus")
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
        _, card = mc.menu_card(mc.current_model(self.ws)["model"])
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
        self.assertEqual((rc, out), (0, {"status": "started", "model": "fable", "pid": 4242, "say": say,
                                         "event_id": "$card2"}))
        (argv, kw), = self.launch.calls
        self.assertEqual(argv[:3], ["nohup", "bash", "-c"])
        self.assertEqual(argv[4:], ["model-card", str(mc.SWITCH), "fable", "180", str(self.ws / "state"),
                                    sys.executable, str(SCRIPT), str(self.ws), DM])
        self.assertTrue(mc.SWITCH.is_file())
        self.assertIn('bash "$1" "$2" --confirm --accept-timeout "$3" --state-dir "$4"; ', argv[3])
        self.assertTrue(argv[3].endswith('"$5" "$6" --workspace "$7" settle --room "$8" --model "$2" --rc $?'))
        self.assertNotIn("&&", argv[3], "the outcome is posted whatever the switch's exit code")
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        card = self.fake.cards()[1]
        self.assertEqual((card["id"], card["state"], card["text"]), (mc.CARD_ID, "pending", say))
        self.assertNotIn("buttons", card)
        self.assertEqual(self.fake.calls[1][1]["reply_to"], "$card1")
        self.assertIn("switch to fable", (self.ws / "logs" / "model-card.log").read_text())
        self.assertIsNone(mc.issued_cards.live(self.ws, "$card1"), "the answered menu stops acting")
        self.assertEqual(self.tap("sonnet")[1]["status"], "forged_or_stale_click")
        self.assertEqual(len(self.launch.calls), 1)

    def test_the_detached_line_passes_the_switch_exit_code_to_settle(self):
        """The line itself, run for real against stand-in scripts: `settle` gets the switch's own code."""
        fake_switch = self.ws / "switch.sh"
        fake_switch.write_text('echo "switch $*"; exit 8\n')
        fake_py = self.ws / "py.sh"
        fake_py.write_text('#!/bin/sh\necho "settle $*"\n')
        fake_py.chmod(0o755)
        got = subprocess.run(["bash", "-c", mc.DETACHED, "model-card", str(fake_switch), "fable", "180", "/st",
                              str(fake_py), "card.py", "/w s", DM], capture_output=True, text=True, check=True)
        self.assertEqual(got.stdout.splitlines(),
                         ["switch fable --confirm --accept-timeout 180 --state-dir /st",
                          f"settle card.py --workspace /w s settle --room {DM} --model fable --rc 8"])

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
        self.assertEqual(len(self.fake.calls), 1, "no switching card for a switch that never started")
        self.fake.execute = lambda *a, **k: {"is_error": True, "code": "M_LIMIT"}
        rc, got = self.tap("fable")
        self.assertEqual((rc, got["status"], got["say"]), (0, "started", mc.switching_words("fable")))
        self.assertNotIn("event_id", got)
        self.assertIn("M_LIMIT", got["card_why"])

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
        self.assertEqual((rc, out), (0, {"status": "posted", "event_id": "$card1", "outcome": "done",
                                         "say": "Switched. I'm now using Fable."}))
        card, = self.fake.cards()
        self.assertEqual((card["id"], card["state"], card["text"]), (mc.CARD_ID, "done", "Switched. I'm now using Fable."))
        self.assertEqual(self.fake.calls[0][3], DM)
        self.assertEqual(self.settle("default", 0)[1]["say"], "Switched. I'm now using the default model.")

    def test_every_documented_failure_posts_the_error_card_with_its_reason(self):
        self.using("opus")
        documented = sorted(mc.REASONS)
        self.assertEqual(documented, list(range(1, 11)))
        switch = mc.SWITCH.read_text()
        for code in documented:
            self.assertTrue(f"exit {code}" in switch, f"switch-model.sh no longer exits {code}")
            rc, out = self.settle("fable", code)
            reason = mc.REASONS[code]
            want = f"I could not switch to Fable. I'm still using Opus. {reason[0].upper()}{reason[1:]}."
            self.assertEqual((rc, out["outcome"], out["say"]), (0, "error", want), code)
            card = self.fake.cards()[-1]
            self.assertEqual((card["id"], card["state"], card["text"]), (mc.CARD_ID, "error", want))
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

    def test_the_outcome_retires_the_menu_and_goes_only_to_the_owner_dm(self):
        self.assertEqual(self.run_(["post", "--room", DM])[0], 0)
        self.assertIsNotNone(mc.issued_cards.live(self.ws, "$card1"))
        self.assertEqual(self.settle("fable", 8)[0], 0)
        self.assertIsNone(mc.issued_cards.live(self.ws, "$card1"))
        rc, out = self.settle("fable", 0, room=ROOM)
        self.assertEqual((rc, out["status"], out["outcome"]), (1, "failed", "done"))
        self.assertEqual(len(self.fake.calls), 2)


class Source(unittest.TestCase):
    def test_the_model_in_use_never_comes_from_a_settings_file_and_no_path_is_hardcoded(self):
        text = SCRIPT.read_text()
        for banned in ("settings.json", ".claude", "/Users/", "Application Support", "expanduser", "environ"):
            self.assertNotIn(banned, text)
        self.assertEqual(mc.SWITCH, SCRIPT.parent / "switch-model.sh")
        self.assertEqual(mc._REPO, ROOT)

    def test_a_brain_settings_file_in_the_workspace_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / ".claude-sutando").mkdir()
            (ws / ".claude-sutando" / "settings.json").write_text(json.dumps({"model": "opus"}))
            self.assertEqual(mc.current_model(ws)["name"], "the default model")


if __name__ == "__main__":
    unittest.main()
