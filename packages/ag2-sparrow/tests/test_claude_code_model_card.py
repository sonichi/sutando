#!/usr/bin/env python3
"""claude-local's model card: the owner's /model in their DM and their tap on the card are answered by
`inbox claim` (never submitted to the session), a pick starts the live switch (#6679) detached, and the
outcome is told in the DM. Stub CLI and a stand-in launcher only: no real Claude or tmux runs."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
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

from ag2_sparrow.adapters.claude_code import account_card, model_card  # noqa: E402

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
        self.assertIn("answered by its card (model-card)", out["refused"])
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
        queued, = self.queued()
        self.assertIn("Switching to Opus", queued["card"]["text"])
        self.assertEqual(queued.get("edit"), "$menu", "shown in place of the menu tapped, not as a second card")

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
        self.assertEqual((rc, out.get("admission")), (0, "owner"), out)
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

    def picked(self, mid="opus", card=""):
        cfg = cli.read_config(cli.config_path())
        ms = model_card.switcher(self.root, cfg)
        ms.save(wanted=mid, asked_in=DM, told="", card=card)
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

    def test_the_outcome_of_a_tapped_pick_is_shown_in_place_of_the_card_tapped(self):
        cfg = self.picked(card="$menu")
        lc = self.Launcher([(True, "switched to opus")])
        self.assertEqual(model_card.apply(self.root, cfg, self.home, launcher_factory=lc, sleep=lambda s: None), "switched")
        queued, = self.queued()
        self.assertEqual((queued.get("edit"), queued["card"]["state"]), ("$menu", "done"))

    def test_a_busy_box_is_tried_again_and_a_switch_that_never_lands_is_told_as_not_switched(self):
        cfg = self.picked("haiku")
        lc = self.Launcher([(False, "refused: the session's input box holds unsent text; nothing sent"),
                            (False, "unconfirmed: /model haiku was sent but Claude Code did not report the switch"),
                            (False, "unconfirmed: again")])
        self.assertEqual(model_card.apply(self.root, cfg, self.home, launcher_factory=lc, sleep=lambda s: None),
                         "not-switched")
        self.assertEqual(len(lc.calls), model_card.APPLY_TRIES)
        self.assertIn("I could not switch to Haiku (Claude Code did not confirm the switch). I'm still using Default.",
                      self.queued()[-1]["card"]["text"])
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

    def clock(self):
        now = [0.0]
        return now, (lambda: now[0]), (lambda secs: now.__setitem__(0, now[0] + secs))

    def test_a_long_turn_is_waited_out_then_the_switch_is_made(self):
        cfg = self.picked()
        now, clock, sleep = self.clock()
        states = iter([True] * 30 + [False] * 10)
        lc = self.Launcher([(True, "switched to opus")])
        with mock.patch.object(model_card, "busy", lambda root: next(states)):
            outcome = model_card.apply(self.root, cfg, self.home, launcher_factory=lc, sleep=sleep, clock=clock)
        self.assertEqual((outcome, len(lc.calls)), ("switched", 1))
        self.assertGreaterEqual(now[0], 30 * model_card.IDLE_POLL_S, "it waited for the turn to end")
        self.assertLess(now[0], model_card.IDLE_WAIT_S + model_card.APPLY_DELAY_S + 1)

    def test_a_session_busy_past_the_wait_is_told_not_switched_and_why(self):
        cfg = self.picked()
        now, clock, sleep = self.clock()
        lc = self.Launcher([])
        with mock.patch.object(model_card, "busy", lambda root: True):
            outcome = model_card.apply(self.root, cfg, self.home, launcher_factory=lc, sleep=sleep, clock=clock)
        self.assertEqual((outcome, lc.calls), ("not-switched", []), "nothing typed into a busy session")
        self.assertGreaterEqual(now[0], model_card.IDLE_WAIT_S)
        self.assertIn("I could not switch to Opus (%s)." % model_card.BUSY, self.queued()[-1]["card"]["text"])

    def claim_of(self, task_id, generation):
        d = Path(self.root) / "state" / "claims" / task_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "claim.json").write_text(json.dumps({"task_id": task_id, "generation": generation}))

    def test_an_offer_in_flight_or_an_unreadable_record_is_busy(self):
        self.assertFalse(model_card.busy(self.root))
        live = model_card.task_io_consumer(self.root)
        self.claim_of("task-a", live)
        with mock.patch.object(model_card.records, "load_plugin", return_value={"inflight": {"task-a": {}}}):
            self.assertTrue(model_card.busy(self.root))
        with mock.patch.object(model_card.records, "load_plugin", side_effect=model_card.records.RecordError("bad")):
            self.assertTrue(model_card.busy(self.root))

    def test_the_stall_check_sees_only_the_running_sessions_turns(self):
        # Live 2026-11-23: a TURN_CONFIRMED entry from before a restart raised a stall card every ten minutes.
        from ag2_sparrow.adapters.claude_code import detector, launcher
        live = model_card.task_io_consumer(self.root)
        self.claim_of("task-old", {"pid": live["pid"] + 1, "start": 1.0})
        left = {"task-old": {"phase": "TURN_CONFIRMED", "turn_id": "t-1"}, "task-gone": {"phase": "CALLING"}}
        self.assertEqual(inbox.live_inflight(self.root, left), {})
        self.assertIsNone(detector.inflight_task({"inflight": inbox.live_inflight(self.root, left)}))
        self.assertEqual(detector.inflight_task({"inflight": left}), "task-gone", "unfiltered, it read as a turn")
        self.claim_of("task-now", live)
        both = dict(left, **{"task-now": {"phase": "TURN_CONFIRMED"}})
        self.assertEqual(inbox.live_inflight(self.root, both), {"task-now": {"phase": "TURN_CONFIRMED"}})
        self.assertEqual(inbox.live_inflight(self.root, None), {})
        self.assertEqual(inbox.live_inflight(self.root, ["not", "a", "dict"]), {})
        # The holder unknown (its record absent, or one that could not be read: both answer None) proves
        # nothing stale, so every entry with a claim is kept; only the one with no claim at all is not.
        for unknown in (None, {}, {"pid": None, "start": None}):
            with mock.patch.object(inbox.task_io, "holder_consumer", return_value=unknown):
                self.assertEqual(sorted(inbox.live_inflight(self.root, both)), ["task-now", "task-old"], unknown)
                with mock.patch.object(model_card.records, "load_plugin", return_value={"inflight": left}):
                    self.assertTrue(model_card.busy(self.root), "unknown is not idle")
        from ag2_sparrow import holder_lock
        with mock.patch.object(holder_lock, "status", side_effect=OSError("unreadable")):
            self.assertEqual(sorted(inbox.live_inflight(self.root, both)), ["task-now", "task-old"])
            self.assertEqual(detector.inflight_task({"inflight": inbox.live_inflight(self.root, left)}), "task-old")
        # A holder that is read, and names another generation, is the one case that drops a claimed entry.
        self.assertEqual(sorted(inbox.live_inflight(self.root, both)), ["task-now"])
        self.assertIn("inflight=inbox.live_inflight(self.root, plugin.get(\"inflight\"))", Path(launcher.__file__).read_text())

    def test_a_claim_that_cannot_be_read_or_names_no_generation_counts_as_a_turn(self):
        self.assertIsNone(inbox.claim_generation(self.root, "task-none"))
        self.claim_of("task-bare", None)
        self.assertEqual(inbox.claim_generation(self.root, "task-bare"), "")
        with mock.patch.object(inbox.lifecycle.ClaimStore, "load", side_effect=OSError("unreadable")):
            self.assertEqual(inbox.claim_generation(self.root, "task-bare"), "")
            with mock.patch.object(model_card.records, "load_plugin", return_value={"inflight": {"task-bare": {}}}):
                self.assertTrue(model_card.busy(self.root))

    def test_an_offer_left_in_flight_by_an_earlier_session_never_blocks_the_switch(self):
        # Live 2026-11-22: a TURN_CONFIRMED entry from before a restart (another consumer pid) held every switch.
        live = model_card.task_io_consumer(self.root)
        self.claim_of("task-old", {"pid": live["pid"] + 1, "start": 1.0})
        plugin = {"inflight": {"task-old": {"phase": "TURN_CONFIRMED", "turn_id": "t-1"}, "task-gone": {"phase": "PREPARED"}}}
        with mock.patch.object(model_card.records, "load_plugin", return_value=plugin):
            self.assertFalse(model_card.busy(self.root), "an earlier generation's entry, and one with no claim, are not turns")
            cfg = self.picked()
            lc = self.Launcher([(True, "switched to opus")])
            outcome = model_card.apply(self.root, cfg, self.home, launcher_factory=lc, sleep=lambda s: None)
        self.assertEqual((outcome, lc.calls), ("switched", [("opus", True)]))
        self.claim_of("task-now", live)
        plugin["inflight"]["task-now"] = {"phase": "TURN_CONFIRMED"}
        with mock.patch.object(model_card.records, "load_plugin", return_value=plugin):
            self.assertTrue(model_card.busy(self.root), "the running session's own turn still is")

    def test_a_pick_no_apply_is_working_on_is_settled_and_told_never_left_pending(self):
        # Live 2026-11-22: wanted "opus" outlived its apply, and the card said "Pending: Opus" for good.
        cfg = self.picked()
        self.assertFalse(model_card.apply_alive(self.root))
        ms = model_card.switcher(self.root, cfg)
        self.assertEqual(ms.pending(ms.state()), "", "settled on the next look")
        self.assertEqual(self.state()["wanted"], "")
        note = self.queued()[-1]["card"]
        self.assertIn("I could not switch to Opus (%s)." % model_card.NOT_APPLIED, note["text"])
        body, card = ms.card(ms.state())
        self.assertNotIn("Pending", card["text"])

    def test_a_pick_whose_apply_is_running_stays_pending(self):
        cfg = self.picked()
        import os
        model_switch.ModelSwitch(str(Path(self.root) / "state"), self.root, None, "x", None, None, None).save(
            apply={"pid": os.getpid(), "start": None})
        self.assertTrue(model_card.apply_alive(self.root))
        ms = model_card.switcher(self.root, cfg)
        self.assertEqual(ms.pending(ms.state()), "opus")
        self.assertIn("Pending: Opus", ms.card(ms.state())[1]["text"])

    def test_start_apply_records_the_detached_process(self):
        class Proc:
            pid = 4242
        REAL_START_APPLY(self.root, lambda *a, **k: Proc())
        self.assertEqual(self.state()["apply"]["pid"], 4242)

    def test_an_apply_that_crashes_settles_the_pick_as_not_switched(self):
        cfg = self.picked()

        class Broken(self.Launcher):
            def switch_model(self, rec, model, confirm=False):
                raise RuntimeError("tmux gone")
        outcome = model_card.apply(self.root, cfg, self.home, launcher_factory=Broken([]), sleep=lambda s: None)
        self.assertEqual(outcome, "not-switched")
        self.assertEqual(self.state()["wanted"], "")
        self.assertIn("the switch failed (RuntimeError)", self.queued()[-1]["card"]["text"])

    def test_an_unknown_refusal_is_told_by_its_first_clause(self):
        cfg = self.picked()
        lc = self.Launcher([(False, "busy: another pane-key sender holds the guard; /model not sent, retry later")])
        model_card.apply(self.root, cfg, self.home, launcher_factory=lc, sleep=lambda s: None)
        self.assertIn("(busy: another pane-key sender holds the guard)", self.queued()[-1]["card"]["text"])


class Named(Base):
    """A model named in the owner's own words is picked directly; the card names models as Claude's menu does."""

    def test_switch_to_a_named_model_picks_it_directly_and_says_switching(self):
        rc, out = self.claim(self.task("task-h", "switch to haiku"))
        self.assertEqual(rc, 1)
        self.assertIn("(model-picked)", out["refused"])
        self.assertEqual(self.state()["wanted"], "haiku")
        self.assertEqual(self.started, [self.root])
        note, = [q["card"] for q in self.queued()]
        self.assertEqual(note["text"], "Switching to Haiku… I'll confirm here once it has switched.")
        self.assertFalse(note.get("buttons"), "no menu")

    def test_the_owners_words_name_one_model_or_none(self):
        cfg = cli.read_config(cli.config_path())
        ms = model_card.switcher(self.root, cfg)
        for said, mid in (("switch to haiku", "haiku"), ("Switch to Fable.", "fable[1m]"), ("use opus", "opus"),
                          ("change the model to sonnet", "sonnet[1m]"), ("换成 haiku", "haiku"),
                          ("切换到 Opus 模型", "opus"), ("please switch to the haiku model", "haiku")):
            self.assertEqual(ms.named_in(said), mid, said)
        for said in ("switch to something faster", "what model are you on?", "use the force", "/model haiku",
                     "switch to haiku and then write me a long essay about the history of poetry please"):
            self.assertEqual(ms.named_in(said), "", said)

    def test_other_owner_words_still_go_to_the_session(self):
        rc, out = self.claim(self.task("task-x", "switch to something faster"))
        self.assertEqual((rc, out.get("admission")), (0, "owner"), out)

    def test_a_model_is_shown_by_its_name_never_its_raw_id(self):
        cfg = cli.read_config(cli.config_path())
        ms = model_card.switcher(self.root, cfg)
        names = {m["id"]: m["name"] for m in ms.offered(ms.state())}
        for mid, name in (("fable", "Fable"), ("fable[1m]", "Fable (1M context)"), ("haiku", "Haiku"),
                          ("sonnet", "Sonnet"), ("claude-opus-4-7", "Opus"), ("default", "Default")):
            self.assertEqual(ms.display(names, mid), name, mid)
        ms.save(current="fable")
        self.assertIn("Now using Fable.", ms.card(ms.state())[1]["text"])
        self.assertNotIn("fable", ms.card(ms.state())[1]["text"])


class Account(Base):
    """`/account` or `/login` in the owner's DM, or a tap on the account card: the account (masked) and where
    to switch it, answered in the claim. Nothing signs in here, and no code ever travels in chat."""

    def setUp(self):
        super().setUp()
        self.answer = {"loggedIn": True, "email": "sarah@example.com", "subscriptionType": "max", "authMethod": "claude.ai"}
        self.runs = []

        def run(argv, **kw):
            self.runs.append((argv, kw.get("env", {}).get("CLAUDE_CONFIG_DIR")))
            if isinstance(self.answer, Exception):
                raise self.answer
            return mock.Mock(stdout=json.dumps(self.answer) if isinstance(self.answer, dict) else str(self.answer))
        p = mock.patch.object(account_card, "_run", run)
        p.start()
        self.addCleanup(p.stop)

    def account_menu(self):
        cfg = cli.read_config(cli.config_path())
        card = account_card.AccountCard(self.root, cfg, lambda *a: None, print).card("pending", "x", ask=True)[1]
        _, issued = connector_cards._core()
        issued.record(Path(self.root), "$acct", card, room=DM, lane="")

    def test_account_and_login_in_the_dm_show_the_masked_account_and_where_to_switch(self):
        for t, said in (("task-a", "/account"), ("task-l", "/login")):
            rc, out = self.claim(self.task(t, said))
            self.assertEqual(rc, 1, t)
            self.assertIn("answered by its card (account-card)", out["refused"])
            self.assertNotIn("admission", out, "never submitted to the session")
        cards = [q["card"] for q in self.queued()]
        self.assertEqual(len(cards), 2)
        text = cards[0]["text"]
        self.assertIn("I'm using sarah@example.com.", text)
        self.assertTrue(text.endswith(account_card.account_switch.BELOW), text)
        self.assertNotIn("Settings", text, "never sent elsewhere")
        self.assertEqual(cards[0]["account_controls"], {"runtime": "claude"})
        self.assertFalse(re.search(r"[\u4e00-\u9fff]", json.dumps(cards, ensure_ascii=False)), "English only")
        self.assertNotIn("(max)", json.dumps(cards), "the full email (owner 2026-11-24), never a plan")
        self.assertEqual([b["label"] for b in cards[0]["buttons"]], ["Switch account"])
        argv, config = self.runs[0]
        self.assertEqual(argv[-3:], ["auth", "status", "--json"])
        self.assertEqual(config, cli.read_config(cli.config_path()).get("CLAUDE_CONFIG_DIR") or None)

    def test_switch_account_points_to_the_app_and_starts_nothing(self):
        self.account_menu()
        said = "Switch account"
        rc, out = self.claim(self.task("task-s", said, reply_to="$acct",
                                       click={"card_id": account_card.account_switch.CARD, "action": said}))
        self.assertEqual(rc, 1)
        self.assertIn("(account-where)", out["refused"])
        queued, = self.queued()
        note = queued["card"]
        self.assertEqual(queued.get("edit"), "$acct", "shown in place of the card tapped, not as a second card")
        self.assertEqual(note["text"], "Open this on the computer that runs Claude Code to switch account. The sign-in "
                                       "code goes into the card there, never into this chat.")
        # Opened there, the same card draws the controls; it is not the menu, so it retires no account card.
        self.assertEqual((note["account_controls"], note["id"]), ({"runtime": "claude"}, account_card.account_switch.NOTE))
        self.assertNotIn("buttons", note)
        self.assertEqual(self.runs, [], "no sign-in, no status call")

    def test_a_tap_by_a_collaborator_is_refused_and_their_account_ask_goes_to_the_session(self):
        self.account_menu()
        said = "Switch account"
        rc, out = self.claim(self.task("task-c", said, reply_to="$acct", tier="team", extra="collaborator: true\n",
                                       click={"card_id": account_card.account_switch.CARD, "action": said}))
        self.assertEqual(rc, 1)
        self.assertIn("couldn't match that button", (self.results / "task-c.txt").read_text())
        rc, out = self.claim(self.task("task-r", "/account", kind="room"))
        self.assertEqual((rc, out.get("admission")), (0, "owner"), "outside the DM it is ordinary text")

    def test_signed_out_unreadable_and_proxied_are_said_as_such(self):
        cfg = cli.read_config(cli.config_path())
        self.answer = {"loggedIn": False}
        self.assertEqual(account_card.label(cfg), "")
        self.answer = "not json"
        with self.assertRaises(RuntimeError):
            account_card.label(cfg)
        self.answer = OSError("no claude")
        self.assertIsNone(account_card.status(cfg)["signed_in"])
        self.answer = {"loggedIn": True}
        self.assertEqual(account_card.label(cfg), "an Anthropic account")
        self.assertIn("credential proxy", account_card.label(cfg, env={"ANTHROPIC_BASE_URL": "http://localhost:7846"}))
        self.answer = {"loggedIn": True, "email": "sarah@example.com"}
        self.assertEqual(account_card.label(cfg), "sarah@example.com")
        for plan in ("max", "pro", "team"):  # owner 2026-11-24: an account card never shows a plan suffix
            self.answer = {"loggedIn": True, "email": "sarah@example.com", "subscriptionType": plan}
            self.assertEqual(account_card.label(cfg), "sarah@example.com", plan)
        self.answer = {"loggedIn": True, "subscriptionType": "max"}
        self.assertEqual(account_card.label(cfg), "an Anthropic account")
        card = account_card.AccountCard(self.root, cfg, lambda *a: None, print)
        self.answer = {"loggedIn": False}
        self.assertEqual(card.using(), "I'm not signed in.")
        self.answer = "nope"
        self.assertEqual(card.using(), "I could not read which account I'm signed in with.")
        out = []
        self.assertEqual(card.tapped("other", {}, str(self.results / "x.txt")), "account-unknown")


class Marks(Account):
    """The session answered its owner with a card's mark alone: the card is shown in their DM and the staged
    answer is its no-send line, as Antigravity and Codex do; any other answer is staged as written."""

    def test_the_model_mark_shows_the_menu_once_however_often_it_is_staged(self):
        t = self.task("task-n", "which model are you on? can I change it?")
        first = inbox.marked(self.root, t, b"[model-card]\n")
        self.assertEqual(first, model_switch.ANSWERED.encode())
        card, = [q["card"] for q in self.queued()]
        self.assertEqual(card["id"], model_switch.CARD)
        self.assertEqual(inbox.marked(self.root, t, b"[model-card]\n"), first, "a retry stages the same bytes")
        self.assertEqual(len(self.queued()), 1, "and shows no second card")

    def test_kept_mark_answers_older_than_a_day_are_removed_when_the_next_one_is_kept(self):
        marks = Path(self.root) / "state" / model_card.MARKED_DIR
        inbox.marked(self.root, self.task("task-old", "can I switch model?"), b"[model-card]\n")
        old = marks / "task-old.txt"
        self.assertTrue(old.is_file())
        os.utime(old, (1, 1))
        inbox.marked(self.root, self.task("task-new", "and now?"), b"[model-card]\n")
        self.assertEqual(sorted(p.name for p in marks.iterdir()), ["task-new.txt"])
        self.assertEqual(model_card.prune_marks(marks), 0, "a fresh one stays for its retry")
        self.assertEqual(model_card.prune_marks(marks / "missing"), 0)

    def test_the_account_mark_shows_the_account_card(self):
        t = self.task("task-q", "can you switch account?")
        self.assertTrue(inbox.marked(self.root, t, b"  [account-card]  ").startswith(b"[no-send]"))
        card, = [q["card"] for q in self.queued()]
        self.assertIn("I'm using sarah@example.com.", card["text"])

    def test_any_other_answer_or_sender_is_staged_as_written(self):
        t = self.task("task-x", "hi")
        self.assertEqual(inbox.marked(self.root, t, b"hello [model-card]"), b"hello [model-card]")
        c = self.task("task-cm", "model?", tier="team", extra="collaborator: true\n")
        self.assertEqual(inbox.marked(self.root, c, b"[model-card]"), b"[model-card]", "not the owner: plain text")
        r = self.task("task-rm", "model?", kind="room")
        self.assertIn(b"direct message", inbox.marked(self.root, r, b"[model-card]"), "outside the DM: where to ask")
        self.assertEqual(self.queued(), [])
        with mock.patch.object(inbox.cm, "load_config_at", return_value=({}, "unreadable")):
            self.assertEqual(inbox.marked(self.root, t, b"[model-card]"), b"[model-card]")
        with mock.patch.object(model_card, "marked_answer", side_effect=RuntimeError("broken")):
            self.assertEqual(inbox.marked(self.root, t, b"[model-card]"), b"[model-card]")

    def test_stage_stages_the_cards_answer_in_place_of_the_mark(self):
        seen = []

        class Lc:
            claims = mock.Mock()

            def stage_result(self, task_id, body, turn_id):
                seen.append(body)
                return "staged"

        with mock.patch.object(inbox, "_lifecycle", return_value=Lc()), \
                mock.patch.object(inbox, "_claims_step", return_value=contextlib.nullcontext()), \
                mock.patch.object(inbox, "_mirror", side_effect=lambda root, t, phase, out, extra=None: out), \
                mock.patch.object(inbox, "marked", return_value=b"[no-send] the model card answers\n") as m:
            inbox.stage(self.root, "task-s", "turn-1", b"[model-card]\n")
        self.assertEqual(seen, [b"[no-send] the model card answers\n"])
        m.assert_called_once_with(self.root, "task-s", b"[model-card]\n")

if __name__ == "__main__":
    unittest.main()
