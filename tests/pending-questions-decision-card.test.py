#!/usr/bin/env python3
"""A pending question with a default or options is asked as a decision card, built by the one
shared helper (src/hitl/cards.py) that every card producer uses; its click closes the row.

- the helper: one button per action, unique ids, turn_on_action by default, the store dedupes by guard;
- delegation: report-feedback, the worker pool's wedge cards and queue_question build cards only
  through the helper, and core's reader reaches the adapter's `clicks`;
- queue_question: options bound for AG2 Space -> a card (Approve first, guard = ask id) and no text;
  no options, another bridge, or a card that cannot be stored -> the text ask;
- a click maps back to its row through the ask id and closes it via `resolve`.
"""
from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import ast
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
PQ = REPO / "skills" / "pending-questions" / "scripts"
for p in (REPO / "src", PQ):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pending_questions_ask as ask
import pending_questions_room_db as room_db
import pending_questions_reader as reader
from hitl import cards
from hitl.manager import HitlManager, HitlStore
from hitl.schema import (Action, ActionReply, STATUS_CANCELLED, STATUS_IN_PROGRESS, STATUS_RESOLVED,
                         StaleRequirementError)

OPTIONS = [("Hold", "wait for the CLA")]


def _ws(last_active=None) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="pq-card-"))
    (ws / "state").mkdir()
    (ws / "results").mkdir()
    if last_active:
        (ws / "state" / "last-owner-activity.json").write_text(json.dumps({"channel": last_active}))
    return ws


def _manager(ws: Path) -> HitlManager:
    return HitlManager(HitlStore(ws / "state" / "hitl" / "requirements"))


def _proactive(ws: Path) -> list:
    return sorted(p.name for p in (ws / "results").glob("proactive-*"))


class Helper(unittest.TestCase):
    def test_one_button_per_action_in_order_with_a_turn_on_click(self):
        req = cards.choice_request("Merge it?", [{"id": "a", "label": "Approve"}, ("b", "Hold"),
                                                 Action(id="c", kind="stop", label="Stop")],
                                   "g1", {"k": "v"}, runtime="rt", title="T")
        self.assertEqual(req.kind, "choice")
        self.assertTrue(req.turn_on_action)
        self.assertEqual([(a.id, a.label, a.kind) for a in req.actions],
                         [("a", "Approve", "confirmation"), ("b", "Hold", "confirmation"), ("c", "Stop", "stop")])
        self.assertEqual((req.guard, req.subject, req.message, req.title), ("g1", {"k": "v"}, "Merge it?", "T"))

    def test_no_actions_or_a_repeated_id_is_refused(self):
        with self.assertRaises(ValueError):
            cards.choice_request("q", [], "g", {}, runtime="rt")
        with self.assertRaises(ValueError):
            cards.choice_request("q", [("a", "A"), ("a", "B")], "g", {}, runtime="rt")

    def test_create_stores_it_and_the_same_guard_is_the_same_card(self):
        ws = _ws()
        m = _manager(ws)
        a = cards.create_choice("q", [("a", "A")], "g", {}, runtime="rt", device={"id": "d"}, manager=m)
        b = cards.create_choice("q", [("a", "A")], "g", {}, runtime="rt", device={"id": "d"}, manager=m)
        self.assertEqual(a.id, b.id)
        self.assertTrue(m.needs_projection(a.id), "the gateway projects what is stored")

    def test_manager_for_is_the_workspace_store(self):
        ws = _ws()
        req = cards.create_choice("q", [("a", "A")], "g", {}, runtime="rt", workspace=ws)
        self.assertTrue((ws / "state" / "hitl" / "requirements" / f"{req.id}.json").is_file())


class Delegation(unittest.TestCase):
    FILES = ("skills/report-feedback/report-feedback.py", "skills/worker-pool/scripts/pool_wedge_cards.py",
             "skills/pending-questions/scripts/pending_questions_ask.py")

    def test_no_card_producer_builds_a_requirement_or_a_store_itself(self):
        for rel in self.FILES:
            tree = ast.parse((REPO / rel).read_text())
            calls = {n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
                     for n in ast.walk(tree) if isinstance(n, ast.Call)}
            with self.subTest(rel=rel):
                self.assertNotIn("HumanRequirement", calls)
                self.assertNotIn("HitlStore", calls)
                self.assertTrue(calls & {"create_choice", "choice_request"}, "builds through hitl.cards")

    def test_report_feedback_asks_through_the_helper(self):
        spec = importlib.util.spec_from_file_location("rf_card_t", REPO / "skills" / "report-feedback" / "report-feedback.py")
        rf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rf)
        ws = _ws()
        with mock.patch.object(cards, "create_choice", wraps=cards.create_choice) as cc:
            rid = rf.register_ask(ws, "fb_0123456789", "title", "host")
            rf.register_held(ws, rf.hitl_manager(ws), "fb_0123456789", "title", "host")
        self.assertEqual([c.args[2] for c in cc.call_args_list], ["fb_0123456789", "fb_0123456789:held"])
        self.assertTrue(all(c.kwargs["runtime"] == rf.HITL_RUNTIME for c in cc.call_args_list))
        self.assertTrue(_manager(ws).get(rid).turn_on_action)

    def test_the_pool_store_handle_is_the_helpers(self):
        src = (REPO / "skills" / "worker-pool" / "scripts" / "pool_wedge_cards.py").read_text()
        self.assertIn("from hitl.cards import manager_for", src)
        self.assertEqual(src.count("choice_request("), 4, "every wedge card is built by the helper")

    def test_queue_question_builds_its_card_through_the_helper(self):
        ws = _ws("ag2space")
        with mock.patch.object(cards, "create_choice", wraps=cards.create_choice) as cc:
            out = ask.queue_question("Merge #1?", workspace=ws, host="h", default_action="merge it",
                                     options=OPTIONS, manager=_manager(ws))
        cc.assert_called_once()
        self.assertEqual(cc.call_args.args[2], out["ask_id"])
        self.assertEqual(cc.call_args.args[3], {"ask_id": out["ask_id"],
                                                "choices": {"approve": "merge it", "option-1": "wait for the CLA"}})
        self.assertEqual(cc.call_args.kwargs["runtime"], ask.CARD_RUNTIME)

    def test_core_reader_reaches_the_adapters_clicks(self):
        fake = mock.Mock(clicks=mock.Mock(return_value=[{"ask_id": "x"}]))
        with mock.patch.object(reader, "_adapter", return_value=(fake, "p")):
            self.assertEqual(reader.clicks("/ws"), ([{"ask_id": "x"}], None))
        fake.clicks.assert_called_once_with(Path("/ws"))
        with mock.patch.object(reader, "_adapter", return_value=(object(), "p")):
            self.assertEqual(reader.clicks("/ws")[0], None, "an adapter without clicks closes nothing")


class QueueQuestion(unittest.TestCase):
    def test_options_bound_for_ag2space_ask_by_card_and_queue_no_text(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = ask.queue_question("Merge #1?", "CI is green", workspace=ws, host="h",
                                 default_action="merge it", reason="green", options=OPTIONS, manager=m)
        self.assertTrue(out["card"])
        self.assertIsNone(out["proactive_file"])
        self.assertEqual(_proactive(ws), [], "no duplicate text ask")
        req = m.get(out["card"])
        self.assertEqual((req.runtime, req.guard, req.subject["ask_id"]), (ask.CARD_RUNTIME, out["ask_id"], out["ask_id"]))
        self.assertEqual([(a.id, a.label) for a in req.actions], [("approve", "Approve"), ("option-1", "Hold")])
        self.assertTrue(req.turn_on_action)
        self.assertIn("Approve: merge it", req.message)
        self.assertIn(f"**Sent:** card {out['card']}", out["sent_line"])
        self.assertTrue(out["outbox"], "the question is still held for its row")
        self.assertTrue(any("decision card" in ln for ln in ask.report_lines(out)))

    def test_a_default_alone_is_enough_and_a_task_from_ag2space_routes_the_card(self):
        ws = _ws("discord")
        with mock.patch.object(ask, "destination_from_task", return_value=ask.Destination(bridge="ag2space")):
            tf = ws / "task.txt"
            tf.write_text("task: x\n")
            out = ask.queue_question("Go?", task_file=str(tf), workspace=ws, host="h",
                                     default_action="go", manager=_manager(ws))
        self.assertTrue(out["card"])
        self.assertEqual([(a.label) for a in _manager(ws).get(out["card"]).actions], ["Approve", "Decline"])

    def test_options_alone_are_the_buttons_as_given_with_no_approve(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = ask.queue_question("Which port?", workspace=ws, host="h",
                                 options=[("8080", "the dev port"), ("Hold", "wait")], manager=m)
        req = m.get(out["card"])
        self.assertEqual([(a.id, a.label) for a in req.actions], [("option-0", "8080"), ("option-1", "Hold")])
        self.assertNotIn("Approve", req.message)

    def test_storing_a_card_is_reported_as_not_yet_shown(self):
        ws = _ws("ag2space")
        out = ask.queue_question("Merge?", workspace=ws, host="h", default_action="merge",
                                 options=OPTIONS, manager=_manager(ws))
        sent = next(ln for ln in ask.report_lines(out) if ln.startswith("sent:"))
        self.assertIn("STORED", sent)
        self.assertIn("not yet shown", out["sent_line"])

    def test_no_options_and_no_default_stays_plain_text(self):
        ws = _ws("ag2space")
        out = ask.queue_question("What next?", workspace=ws, host="h", manager=_manager(ws))
        self.assertIsNone(out["card"])
        self.assertEqual(_proactive(ws), [out["proactive_file"]])
        self.assertEqual(_manager(ws).active(), [])

    def test_another_bridge_gets_text_since_only_ag2space_projects_cards(self):
        ws = _ws("discord")
        out = ask.queue_question("Merge?", workspace=ws, host="h", default_action="merge", options=OPTIONS,
                                 manager=_manager(ws))
        self.assertIsNone(out["card"])
        self.assertTrue(out["proactive_file"])

    def test_a_card_that_cannot_be_stored_falls_back_to_text(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        with mock.patch.object(m, "create", side_effect=OSError("disk full")):
            out = ask.queue_question("Merge?", workspace=ws, host="h", default_action="merge",
                                     options=OPTIONS, manager=m)
        self.assertIsNone(out["card"])
        self.assertIn("disk full", out["card_error"])
        self.assertEqual(_proactive(ws), [out["proactive_file"]])
        self.assertTrue(any("asked as text instead" in ln for ln in ask.report_lines(out)))


class UnshownCard(unittest.TestCase):
    def _card(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = ask.queue_question("Merge #1?", workspace=ws, host="h", default_action="merge",
                                 options=OPTIONS, manager=m)
        return ws, m, out, m.get(out["card"])

    def test_a_card_never_shown_within_the_grace_is_asked_as_text_once(self):
        ws, m, out, req = self._card()
        self.assertEqual(ask.unshown_cards_as_text(ws, m, now=req.created_at + 10, host="h"), [], "inside the grace")
        self.assertEqual(_proactive(ws), [])
        done = ask.unshown_cards_as_text(ws, m, now=req.created_at + ask.CARD_SHOW_GRACE_SEC, host="h")
        self.assertEqual([(d["ask_id"], d["hitl_id"]) for d in done], [(out["ask_id"], req.id)])
        files = _proactive(ws)
        self.assertEqual(files, [done[0]["proactive_file"]])
        body = (ws / "results" / files[0]).read_text()
        self.assertIn("Merge #1?", body)
        self.assertIn("was not shown", body)
        self.assertEqual(ask.unshown_cards_as_text(ws, m, now=req.created_at + 9999, host="h"), [], "once per card")
        self.assertEqual(m.get(req.id).status, "pending", "the card stays answerable")

    def test_a_shown_card_is_never_doubled_as_text(self):
        ws, m, out, req = self._card()
        m.record_projection(req.id, req.revision, "$event")
        self.assertEqual(ask.unshown_cards_as_text(ws, m, now=req.created_at + 9999, host="h"), [])
        self.assertEqual(_proactive(ws), [])

    def test_reconcile_runs_the_fallback_even_with_no_room(self):
        ws, m, out, req = self._card()
        with mock.patch.object(ask, "CARD_SHOW_GRACE_SEC", 0), \
                mock.patch.object(room_db, "room_store", return_value=(None, "no room")):
            rec = room_db.reconcile_pass(ws)
        self.assertEqual([t["ask_id"] for t in rec["texted"]], [out["ask_id"]])
        self.assertEqual(len(_proactive(ws)), 1)


class Clicks(unittest.TestCase):
    def _clicked(self, action_id="option-1"):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = ask.queue_question("Merge?", workspace=ws, host="h", default_action="merge",
                                 options=OPTIONS, manager=m)
        req = m.get(out["card"])
        m.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision,
                                   action_id=action_id, guard=req.guard))
        return ws, m, out

    def test_a_click_closes_its_row_and_is_settled_by_the_ack(self):
        ws, m, out = self._clicked()
        closer = mock.Mock(return_value=(True, "room database: closed"))
        done = room_db.clicks(ws, manager=m, closer=closer)
        closer.assert_called_once_with(ws, out["ask_id"], "Answered")
        self.assertEqual([(c["ask_id"], c["action_id"], c["consequence"], c["closed"]) for c in done],
                         [(out["ask_id"], "option-1", "wait for the CLA", True)])
        self.assertEqual(m.get(out["card"]).status, STATUS_IN_PROGRESS, "claimed until the core acks")
        self.assertEqual(room_db.ack_click(ws, out["ask_id"], m)[0], True)
        self.assertEqual(m.get(out["card"]).status, STATUS_RESOLVED)
        self.assertEqual(room_db.clicks(ws, manager=m, closer=closer), [], "acknowledged: reported once")
        self.assertEqual(room_db.ack_click(ws, out["ask_id"], m)[0], False, "nothing left to ack")

    def test_a_run_that_dies_before_the_ack_replays_the_same_choice_without_closing_again(self):
        ws, m, out = self._clicked()
        closer = mock.Mock(return_value=(True, "room database: closed"))
        room_db.clicks(ws, manager=m, closer=closer)
        again = room_db.clicks(ws, manager=m, closer=closer)
        self.assertEqual(closer.call_count, 1, "the row is not closed twice")
        self.assertEqual((again[0]["replay"], again[0]["stale"], again[0]["action_id"]), (True, False, "option-1"))
        line = reader.click_line(again[0])
        self.assertIn("chose option-1 'Hold' -> wait for the CLA", line)
        self.assertIn("replayed", line)

    def test_a_click_on_a_row_closed_already_is_stale_never_a_choice(self):
        ws, m, out = self._clicked("approve")
        done = room_db.clicks(ws, manager=m, closer=lambda *a: (False, f"{room_db.REFUSED_PREFIX} — closed already"))
        self.assertEqual((done[0]["label"], done[0]["stale"], done[0]["closed"]), ("Approve", True, False))
        self.assertEqual(m.get(out["card"]).status, STATUS_CANCELLED)
        line = reader.click_line(done[0])
        self.assertNotIn("chose", line)
        self.assertIn("already answered — not a decision", line)

    def test_a_close_that_could_not_be_recorded_keeps_the_card_and_is_not_a_new_choice_next_run(self):
        ws, m, out = self._clicked()
        fail = lambda *a: (False, "not recorded: nothing held")  # noqa: E731
        first = room_db.clicks(ws, manager=m, closer=fail)
        self.assertEqual(m.get(out["card"]).status, STATUS_IN_PROGRESS)
        self.assertIn("chose", reader.click_line(first[0]))
        again = room_db.clicks(ws, manager=m, closer=fail)
        self.assertTrue(again[0]["reported_before"])
        self.assertNotIn("chose", reader.click_line(again[0]))

    def test_the_real_resolve_records_the_close_locally_without_a_room(self):
        ws, m, out = self._clicked()
        with mock.patch.object(room_db, "room_store", return_value=(None, "no room")):
            done = room_db.clicks(ws, manager=m)
        self.assertTrue(done[0]["closed"], done[0]["close_message"])
        self.assertTrue((Path(out["outbox"]).parent / "closed" / f"{out['ask_id']}.json").is_file())
        self.assertEqual(m.get(out["card"]).status, STATUS_IN_PROGRESS)

    def test_an_answer_by_text_retires_the_card_so_a_late_click_is_refused(self):
        ws = _ws("ag2space")
        out = ask.queue_question("Merge?", workspace=ws, host="h", default_action="merge",
                                 options=OPTIONS, manager=_manager(ws))
        with mock.patch.object(room_db, "room_store", return_value=(None, "no room")):
            ok, msg = room_db.resolve(ws, out["ask_id"], "Answered")
        self.assertTrue(ok, msg)
        self.assertIn("retired", msg)
        m = _manager(ws)
        req = m.get(out["card"])
        self.assertEqual(req.status, STATUS_CANCELLED)
        with self.assertRaises(StaleRequirementError):
            m.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision - 1,
                                       action_id="approve", guard=req.guard))
        self.assertEqual(room_db.clicks(ws, manager=m), [])

    def test_core_resolve_retires_a_card_already_clicked(self):
        ws, m, out = self._clicked()
        adapter = str(PQ / "pending_questions_room_db.py")
        loaded = reader.load_adapter(adapter)
        with mock.patch.object(loaded, "room_store", return_value=(None, "no room")):
            ok, msg = reader.resolve(ws, out["ask_id"], "Resolved", adapter)
        self.assertTrue(ok, msg)
        self.assertEqual(m.get(out["card"]).status, STATUS_CANCELLED)
        self.assertEqual(ask.answered_cards(m), [], "the click is never handed to the core")

    def test_a_refused_close_also_retires_the_card(self):
        ws = _ws("ag2space")
        out = ask.queue_question("Merge?", workspace=ws, host="h", default_action="merge",
                                 options=OPTIONS, manager=_manager(ws))
        with mock.patch.object(room_db, "_close_row", return_value=(False, f"{room_db.REFUSED_PREFIX} — closed")):
            room_db.resolve(ws, out["ask_id"], "Answered")
        self.assertEqual(_manager(ws).get(out["card"]).status, STATUS_CANCELLED)

    def test_other_runtimes_cards_are_not_this_skills(self):
        ws = _ws()
        m = _manager(ws)
        req = cards.create_choice("q", [("a", "A")], "g", {}, runtime="report-feedback", manager=m)
        m.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision, action_id="a", guard="g"))
        self.assertEqual(ask.answered_cards(m), [])


def _room_db_suite():
    spec = importlib.util.spec_from_file_location("pq_room_db_suite", REPO / "tests" / "pending-questions-room-db.test.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class OwnerStatusWins(unittest.TestCase):
    """The production close writer, over the adapter's real `apply`: an owner's terminal Status
    refuses the click's close in the same guarded write, so the click is stale, never `chose`."""

    def setUp(self):
        import pending_questions_store as pqs
        self.pqs, self.rdb = pqs, _room_db_suite()
        self.ws = _ws("ag2space")
        env = mock.patch.dict("os.environ", {"SUTANDO_HOST_LABEL": self.rdb.HOST})
        env.start()
        self.addCleanup(env.stop)
        self.store = pqs.RoomDbStore(self.rdb.InProcClient(), lock=self.ws / "state" / "pq-db.lock",
                                     host=self.rdb.HOST)
        self.m = _manager(self.ws)
        with mock.patch.object(ask, "card_reaches_owner", return_value=True):
            out = room_db.ask_owner("Merge #12?", urgency="durable", workspace=self.ws, host=self.rdb.HOST,
                                    store=self.store, default_action="merge it", options=OPTIONS)
        self.ask_id, self.card = out["ask_id"], out["card"]
        self.assertTrue(self.card and out["record"], out)

    def _owner_sets(self, status):
        self.store.client.set_cells(self.pqs.DB_SCHEMA, self.store._rid(self.ask_id), {"status": status})

    def _click(self, action_id):
        req = self.m.get(self.card)
        self.m.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision,
                                        action_id=action_id, guard=req.guard))
        with mock.patch.object(room_db, "room_store", return_value=(self.store, "room")):
            return room_db.clicks(self.ws, manager=self.m)

    def test_owner_answered_then_a_contradicting_click_is_stale(self):
        self._owner_sets("answered")
        [c] = self._click("option-1")
        self.assertEqual((c["stale"], c["closed"]), (True, False), c["close_message"])
        self.assertNotIn("chose", reader.click_line(c))
        self.assertEqual(self.m.get(self.card).status, STATUS_CANCELLED)
        self.assertEqual(self.store.status_of(self.ask_id), "Answered")

    def test_owner_resolved_then_a_contradicting_click_is_stale(self):
        self._owner_sets("resolved")
        [c] = self._click("approve")
        self.assertEqual((c["stale"], c["closed"]), (True, False), c["close_message"])
        self.assertNotIn("chose", reader.click_line(c))
        self.assertEqual(self.store.status_of(self.ask_id), "Resolved")

    def test_an_open_row_is_closed_by_the_click(self):
        [c] = self._click("approve")
        self.assertEqual((c["stale"], c["closed"]), (False, True), c["close_message"])
        self.assertEqual(self.store.status_of(self.ask_id), "Answered")

    def test_a_plain_resolve_still_closes_beside_an_owner_status(self):
        self._owner_sets("resolved")
        self.store.close(self.ask_id, "Answered")


class ExactChoice(unittest.TestCase):
    def _clicked(self, question, options, action_id, default=None):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = ask.queue_question(question, workspace=ws, host="h", default_action=default,
                                 options=options, manager=m)
        req = m.get(out["card"])
        m.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision,
                                   action_id=action_id, guard=req.guard))
        return room_db.clicks(ws, manager=m, closer=lambda *a: (True, "closed"))[0]

    def test_a_long_question_still_reports_the_exact_operation(self):
        long_q = "Context. " * 60 + "Which deploy?"
        c = self._clicked(long_q, [("Ship", "deploy build 4411 to prod"), ("Hold", "wait a day")], "option-0")
        line = reader.click_line(c)
        self.assertIn("chose option-0 'Ship' -> deploy build 4411 to prod", line)

    def test_duplicate_labels_print_distinct_ids_and_consequences(self):
        opts = [("Yes", "merge #1"), ("Yes", "merge #2")]
        one = reader.click_line(self._clicked("Which?", opts, "option-0"))
        two = reader.click_line(self._clicked("Which?", opts, "option-1"))
        self.assertIn("option-0 'Yes' -> merge #1", one)
        self.assertIn("option-1 'Yes' -> merge #2", two)
        self.assertNotEqual(one, two)


class EdgePaths(unittest.TestCase):
    def test_the_card_carries_the_row_link(self):
        msg = ask.card_message("Q?", None, "go", None, (), link="https://row.test.invalid/1")
        self.assertIn("https://row.test.invalid/1", msg)

    def test_a_card_that_cannot_be_retired_is_reported_and_the_close_stands(self):
        ws = _ws()
        with mock.patch.object(room_db, "_close_row", return_value=(True, "closed")), \
                mock.patch.object(ask, "retire_cards", side_effect=OSError("store gone")):
            ok, msg = room_db.resolve(ws, "ask-x", "Answered", manager=_manager(ws))
        self.assertTrue(ok)
        self.assertIn("could not be retired", msg)

    def test_withdrawn_card_whose_text_cannot_be_written_reports_the_send_error(self):
        ws = _ws("ag2space")
        with mock.patch.object(ask.Outbox, "save", side_effect=OSError("disk full")), \
                mock.patch.object(ask, "write_proactive", side_effect=OSError("results gone")):
            out = ask.queue_question("Merge?", workspace=ws, host="h", default_action="merge",
                                     options=OPTIONS, manager=_manager(ws))
        self.assertIn("results gone", out["send_error"])

    def test_unshown_fallback_errors_are_reported_not_raised(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = ask.queue_question("Merge?", workspace=ws, host="h", default_action="merge",
                                 options=OPTIONS, manager=m)
        with mock.patch.object(m, "active", side_effect=OSError("store gone")):
            self.assertIn("store gone", ask.unshown_cards_as_text(ws, m)[0]["error"])
        with mock.patch.object(ask, "write_proactive", side_effect=OSError("results gone")):
            [t] = ask.unshown_cards_as_text(ws, m, now=time.time() + 9999, host="h")
        self.assertEqual((t["ask_id"], "results gone" in t["error"]), (out["ask_id"], True))

    def test_the_ask_and_pq_reconcile_report_a_text_fallback(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        ask.queue_question("Merge?", workspace=ws, host="h", default_action="merge", options=OPTIONS, manager=m)
        with mock.patch.object(ask, "CARD_SHOW_GRACE_SEC", 0), \
                mock.patch.object(ask, "card_reaches_owner", return_value=False), \
                mock.patch.object(ask, "write_proactive", wraps=ask.write_proactive):
            out = room_db.ask_owner("Other?", urgency="durable", workspace=ws, host="h", store=None)
        lines = room_db.report_lines(out)
        self.assertTrue(any("was never shown; asked as text" in ln for ln in lines), lines)
        spec = importlib.util.spec_from_file_location("pq_cli_r", PQ / "pq.py")
        pq = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pq)
        rec = {"flushed": [], "closed": [], "moved": [], "errors": [],
               "texted": [{"hitl_id": "hitl_1", "ask_id": "a1", "proactive_file": "proactive-a1-text.txt"}]}
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(room_db, "reconcile_pass", return_value=rec), contextlib.redirect_stdout(buf):
            pq.main(["reconcile", "--workspace", str(ws)])
        self.assertIn("was never shown; asked as text", buf.getvalue())


class OutboxFailure(unittest.TestCase):
    def test_a_card_whose_question_cannot_be_held_is_withdrawn_and_asked_as_text(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        with mock.patch.object(ask.Outbox, "save", side_effect=OSError("disk full")):
            out = ask.queue_question("Merge?", workspace=ws, host="h", default_action="merge",
                                     options=OPTIONS, manager=m)
        self.assertIsNone(out["card"])
        self.assertIn("disk full", out["outbox_error"])
        self.assertEqual(_proactive(ws), [out["proactive_file"]], "asked as text instead")
        [req] = [r for r in m.store.all() if r.runtime == ask.CARD_RUNTIME]
        self.assertEqual(req.status, STATUS_CANCELLED)
        self.assertFalse(m.needs_projection(req.id), "an unshown card is never posted")

    def test_withdrawing_a_shown_card_still_projects_the_cancellation(self):
        ws = _ws()
        m = _manager(ws)
        req = cards.create_choice("q", [("a", "A")], "g", {}, runtime="rt", manager=m)
        m.record_projection(req.id, req.revision, "$ev")
        self.assertFalse(cards.withdraw(m, req.id))
        self.assertTrue(m.needs_projection(req.id), "the shown card is edited to cancelled")
        self.assertFalse(cards.withdraw(m, "hitl_missing0000"))

    def test_a_withdraw_that_fails_is_reported(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        with mock.patch.object(ask.Outbox, "save", side_effect=OSError("disk full")), \
                mock.patch.object(cards, "withdraw", side_effect=OSError("store gone")):
            out = ask.queue_question("Merge?", workspace=ws, host="h", default_action="merge",
                                     options=OPTIONS, manager=m)
        self.assertIn("not withdrawn", out["card_error"])
        self.assertTrue(out["proactive_file"])


class CoreEntries(unittest.TestCase):
    ADAPTER = str(PQ / "pending_questions_room_db.py")

    def _clicked_ws(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = ask.queue_question("Merge?", workspace=ws, host="h", default_action="merge",
                                 options=OPTIONS, manager=m)
        req = m.get(out["card"])
        m.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision,
                                   action_id="approve", guard=req.guard))
        return ws, out

    def _main(self, argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = reader.main(argv)
        return rc, buf.getvalue()

    def test_clicks_then_ack_through_the_reader_cli(self):
        ws, out = self._clicked_ws()
        loaded = reader.load_adapter(self.ADAPTER)
        with mock.patch.object(loaded, "room_store", return_value=(None, "no room")):
            rc, text = self._main(["clicks", "--workspace", str(ws), "--store-adapter", self.ADAPTER])
            self.assertEqual(rc, 0, text)
            self.assertIn(f"[{out['ask_id']}] chose approve 'Approve' -> merge", text)
            rc, text = self._main(["clicks", "--json", "--workspace", str(ws), "--store-adapter", self.ADAPTER])
            self.assertEqual(json.loads(text)[0]["replay"], True)
            rc, text = self._main(["ack", out["ask_id"], "--workspace", str(ws), "--store-adapter", self.ADAPTER])
            self.assertEqual(rc, 0, text)
            rc, text = self._main(["clicks", "--workspace", str(ws), "--store-adapter", self.ADAPTER])
            self.assertIn("no question-card clicks waiting", text)
            rc, text = self._main(["ack", out["ask_id"], "--workspace", str(ws), "--store-adapter", self.ADAPTER])
            self.assertEqual(rc, 1)

    def test_a_close_that_failed_makes_the_cli_exit_nonzero(self):
        ws, out = self._clicked_ws()
        loaded = reader.load_adapter(self.ADAPTER)
        with mock.patch.object(loaded, "_close_for_click", return_value=(False, "not recorded: x")):
            rc, text = self._main(["clicks", "--workspace", str(ws), "--store-adapter", self.ADAPTER])
        self.assertEqual(rc, 1)

    def test_ack_needs_an_id_and_an_adapter(self):
        with self.assertRaises(SystemExit):
            self._main(["ack", "--workspace", "/nonexistent"])
        with mock.patch.object(reader, "_adapter", return_value=(None, "no adapter")):
            self.assertEqual(reader.ack("/ws", "a")[0], False)
            self.assertEqual(reader.clicks("/ws"), (None, "no store: no adapter"))
            rc, text = self._main(["clicks", "--workspace", "/nonexistent"])
            self.assertEqual(rc, 1)
            self.assertIn("not read", text)
        boom = mock.Mock(clicks=mock.Mock(side_effect=OSError("x")), ack_click=mock.Mock(side_effect=OSError("y")))
        with mock.patch.object(reader, "_adapter", return_value=(boom, "p")):
            self.assertIn("adapter failed", reader.clicks("/ws")[1])
            self.assertIn("adapter failed", reader.ack("/ws", "a")[1])

    def test_pq_cli_forwards_clicks_and_ack(self):
        spec = importlib.util.spec_from_file_location("pq_cli_t", PQ / "pq.py")
        pq = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pq)
        with mock.patch.object(reader, "main", return_value=0) as rm:
            self.assertEqual(pq.main(["clicks", "--json"]), 0)
            self.assertEqual(pq.main(["ack", "ask-1"]), 0)
        self.assertEqual(rm.call_args_list[0].args[0][:2], ["clicks", "--json"])
        self.assertEqual(rm.call_args_list[1].args[0][:2], ["ack", "ask-1"])
        self.assertIn("--store-adapter", rm.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
