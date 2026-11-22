#!/usr/bin/env python3
"""claude-local's model card: the owner's /model in their DM and their tap on the card are answered by
`inbox claim` (never submitted to the session), a pick starts the live switch (#6679) detached, and the
outcome is told in the DM. Stub CLI and a stand-in launcher only: no real Claude or tmux runs."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_code_fixtures import cli, ConnectBase, hold_consumer, inbox, unhold  # noqa: E402
from ag2_sparrow import connector_cards, model_switch  # noqa: E402
from ag2_sparrow.adapters.claude_code import model_card  # noqa: E402

OWNER, DM = "@owner:s.space", "!dm:s.space"
REAL_START_APPLY = model_card.start_apply


class Base(ConnectBase):
    def setUp(self):
        super().setUp()
        self.bridge = self.home / "bridge-state"
        self.bridge.mkdir()
        self.configure(BRIDGE_STATE_DIR=str(self.bridge), ALLOW_COLLABORATOR="1")
        self.assertEqual(inbox.prepare_root(str(self.work)), "")
        self.holder, _ = hold_consumer(self.work)
        self.addCleanup(unhold, self.holder)
        self.root = str(self.work)
        self.started = []
        p = mock.patch.object(model_card, "start_apply", lambda root, popen=None, environ=None: self.started.append(root))
        p.start()
        self.addCleanup(p.stop)

    def task(self, task_id, said, tier="owner", kind="dm", click=None, reply_to="", sender=OWNER, extra=""):
        head = ("access_tier: %s\nuser_id: %s\nchannel_id: %s\nchannel_kind: %s\n%s" % (tier, sender, DM, kind, extra)
                + ("reply_to_event: %s\n" % reply_to if reply_to else "")
                + ("card_click: %s\n" % json.dumps(click) if click else ""))
        (self.inbox / (task_id + ".txt")).write_text("id: %s\n%stask: %s\nsource: ag2space\n" % (task_id, head, said))
        return task_id

    def claim(self, task_id):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["inbox", "--root", self.root, "claim", task_id])
        return rc, json.loads(out.getvalue())

    def queued(self):
        return [json.loads(p.read_text()) for p in sorted(self.bridge.joinpath(*connector_cards.OUTBOX).glob("*.json"))]

    def menu_posted(self):
        """The menu, as the bridge posts it and the connector records it: its buttons then act."""
        cfg = cli.read_config(cli.config_path())
        body, card = model_card.switcher(self.root, cfg).card(model_card.switcher(self.root, cfg).state())
        _, issued = connector_cards._core()
        issued.record(Path(self.root), "$menu", card, room=DM, lane="")
        return card

    def state(self):
        return json.loads((Path(self.root) / "state" / model_switch.FILE).read_text())


class Claim(Base):
    def test_the_owners_model_in_their_dm_is_the_menu_and_never_reaches_the_session(self):
        rc, out = self.claim(self.task("task-m", "/model"))
        self.assertEqual(rc, 1)
        self.assertIn("answered by the model card (model-card)", out["refused"])
        self.assertNotIn("admission", out, "nothing for the Mod to submit")
        self.assertEqual((self.results / "task-m.txt").read_text(), model_switch.ANSWERED)
        card, = [q["card"] for q in self.queued()]
        self.assertEqual([b["label"] for b in card["buttons"]], [name for _, name in model_card.CHOICES])
        self.assertIn("Now using Default.", card["text"])
        self.assertEqual(self.started, [])

    def test_the_owners_tap_picks_the_model_and_starts_the_live_switch(self):
        self.menu_posted()
        said = "Use Opus"
        rc, out = self.claim(self.task("task-t", said, reply_to="$menu",
                                       click={"card_id": model_switch.CARD, "action": said}))
        self.assertEqual(rc, 1)
        self.assertIn("(model-picked)", out["refused"])
        self.assertEqual(self.state()["wanted"], "opus")
        self.assertEqual(self.started, [self.root])
        self.assertEqual((self.results / "task-t.txt").read_text(), model_switch.ANSWERED)
        note, = [q["card"] for q in self.queued()]
        self.assertIn("Switching to Opus", note["text"])

    def test_a_tap_by_anyone_but_the_owner_in_their_dm_is_refused_and_picks_nothing(self):
        self.menu_posted()
        said = "Use Opus"
        for t, kw in (("task-c", {"tier": "team", "extra": "collaborator: true\n"}),
                      ("task-r", {"kind": "room"})):
            rc, out = self.claim(self.task(t, said, reply_to="$menu", click={"card_id": model_switch.CARD, "action": said},
                                           **kw))
            self.assertEqual(rc, 1, t)
            self.assertNotIn("admission", out, t)
            self.assertIn("couldn't match that button", (self.results / (t + ".txt")).read_text(), t)
        self.assertNotIn("wanted", self.state())
        self.assertEqual(self.started, [])

    def test_any_other_owner_message_is_claimed_for_the_session_as_before(self):
        for t, said in (("task-o", "what model are you on?"), ("task-w", "/models please")):
            rc, out = self.claim(self.task(t, said))
            self.assertEqual((rc, out.get("admission")), (0, "owner"), t)
        rc, out = self.claim(self.task("task-g", "/model", tier="owner", kind="room"))
        self.assertEqual((rc, out.get("admission")), (0, "owner"), "/model outside the DM is ordinary text")

    def test_a_card_that_cannot_be_consulted_never_holds_a_task_back(self):
        with mock.patch.object(model_card, "card_matter", side_effect=RuntimeError("broken")):
            rc, out = self.claim(self.task("task-b", "/model"))
        self.assertEqual((rc, out.get("admission")), (0, "owner"))
        with mock.patch.object(inbox.cm, "load_config_at", return_value=({}, "unreadable")):
            self.assertEqual(inbox.card_matter(self.root, "task-b"), ("", ""))


class Apply(Base):
    class Launcher:
        def __init__(self, answers):
            self.answers, self.calls = list(answers), []

        def __call__(self, cfg, config_dir):
            return self

        def switch_model(self, rec, model, confirm=False):
            self.calls.append((model, confirm))
            return self.answers.pop(0)

    def picked(self, mid="opus"):
        cfg = cli.read_config(cli.config_path())
        ms = model_card.switcher(self.root, cfg)
        ms.save(wanted=mid, asked_in=DM, told="")
        return cfg

    def test_the_pick_is_switched_live_with_confirm_and_told_as_switched(self):
        cfg = self.picked()
        lc = self.Launcher([(True, "switched to opus")])
        outcome = model_card.apply(self.root, cfg, self.home, launcher_factory=lc, sleep=lambda s: None)
        self.assertEqual((outcome, lc.calls), ("switched", [("opus", True)]))
        self.assertEqual((self.state()["current"], self.state()["confirmed"]), ("opus", "opus"))
        note, = [q["card"] for q in self.queued()]
        self.assertIn("Switched. I'm now using Opus.", note["text"])
        self.assertEqual(model_card.turn_model(self.root, cfg), "opus", "the isolated turn runs on the pick too")

    def test_a_busy_box_is_tried_again_and_a_switch_that_never_lands_is_told_as_not_switched(self):
        cfg = self.picked("haiku")
        lc = self.Launcher([(False, "refused: the session's input box holds unsent text; nothing sent"),
                            (False, "unconfirmed: /model haiku was sent but Claude Code did not report the switch"),
                            (False, "unconfirmed: again")])
        self.assertEqual(model_card.apply(self.root, cfg, self.home, launcher_factory=lc, sleep=lambda s: None),
                         "not-switched")
        self.assertEqual(len(lc.calls), model_card.APPLY_TRIES)
        self.assertIn("I could not switch to Haiku", self.queued()[-1]["card"]["text"])
        self.assertEqual(model_card.turn_model(self.root, cfg), "")

    def test_a_refusal_that_another_try_cannot_change_is_not_retried(self):
        cfg = self.picked()
        lc = self.Launcher([(False, "no running session to switch; nothing sent")])
        self.assertEqual(model_card.apply(self.root, cfg, self.home, launcher_factory=lc, sleep=lambda s: None),
                         "not-switched")
        self.assertEqual(len(lc.calls), 1)

    def test_nothing_wanted_is_nothing_switched(self):
        cfg = cli.read_config(cli.config_path())
        lc = self.Launcher([])
        self.assertEqual(model_card.apply(self.root, cfg, self.home, launcher_factory=lc, sleep=lambda s: None), "")
        self.assertEqual(lc.calls, [])

    def test_the_default_model_is_never_forced_on_the_isolated_turn(self):
        cfg = self.picked("default")
        model_card.apply(self.root, cfg, self.home, launcher_factory=self.Launcher([(True, "ok")]), sleep=lambda s: None)
        self.assertEqual(model_card.turn_model(self.root, cfg), "")

    def test_the_detached_step_is_this_module_in_a_session_of_its_own(self):
        calls = []
        REAL_START_APPLY(self.root, popen=lambda argv, **kw: calls.append((argv, kw)), environ={"X": "1"})
        argv, kw = calls[0]
        self.assertEqual(argv[1:], ["-m", model_card.__name__, "apply", "--root", self.root])
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["env"], {"X": "1"})
        REAL_START_APPLY(self.root, popen=mock.Mock(side_effect=OSError("no exec")), environ={})  # logged, not raised

    def test_the_apply_entry_reads_the_installed_config(self):
        out = io.StringIO()
        with mock.patch.object(model_card, "apply", return_value="switched") as run, contextlib.redirect_stdout(out):
            self.assertEqual(model_card.main(["apply", "--root", self.root]), 0)
        self.assertEqual((run.call_args[0][0], out.getvalue().strip()), (self.root, "switched"))
        with mock.patch.object(model_card, "apply", return_value=""), contextlib.redirect_stdout(io.StringIO()) as quiet:
            model_card.main(["apply", "--root", self.root])
        self.assertEqual(quiet.getvalue().strip(), "nothing to switch")
        with mock.patch.object(inbox, "_installed_config", return_value=self.home / "missing.env"):
            self.assertEqual(model_card.main(["apply", "--root", self.root]), 1)

    def test_a_broken_card_ledger_or_state_never_breaks_a_turn(self):
        cfg = cli.read_config(cli.config_path())
        with mock.patch.object(model_card, "switcher", side_effect=OSError("state unreadable")):
            self.assertEqual(model_card.turn_model(self.root, cfg), "")
        with mock.patch.object(connector_cards, "absorb", side_effect=OSError("ledger")):
            self.assertEqual(model_card.card_matter(self.root, self.task("task-z", "hello"), cfg), ("", ""))

if __name__ == "__main__":
    unittest.main()
