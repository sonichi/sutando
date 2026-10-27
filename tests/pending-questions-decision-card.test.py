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
import ast
import importlib.util
import json
import sys
import tempfile
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
from hitl.schema import Action, ActionReply, STATUS_IN_PROGRESS, STATUS_RESOLVED

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
        self.assertEqual(cc.call_args.args[3], {"ask_id": out["ask_id"]})
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
        self.assertEqual((req.runtime, req.guard, req.subject), (ask.CARD_RUNTIME, out["ask_id"], {"ask_id": out["ask_id"]}))
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

    def test_a_click_closes_its_row_through_resolve_and_settles_the_card(self):
        ws, m, out = self._clicked()
        closer = mock.Mock(return_value=(True, "room database: closed"))
        done = room_db.clicks(ws, manager=m, closer=closer)
        closer.assert_called_once_with(ws, out["ask_id"], "Answered")
        self.assertEqual([(c["ask_id"], c["label"], c["closed"]) for c in done], [(out["ask_id"], "Hold", True)])
        self.assertEqual(m.get(out["card"]).status, STATUS_RESOLVED)
        self.assertEqual(room_db.clicks(ws, manager=m, closer=closer), [], "reported once")

    def test_a_row_the_room_refuses_to_close_still_settles_the_card(self):
        ws, m, out = self._clicked("approve")
        done = room_db.clicks(ws, manager=m, closer=lambda *a: (False, f"{room_db.REFUSED_PREFIX} — closed already"))
        self.assertEqual(done[0]["label"], "Approve")
        self.assertEqual(m.get(out["card"]).status, STATUS_RESOLVED)

    def test_a_close_that_could_not_be_recorded_keeps_the_card_for_the_next_run(self):
        ws, m, out = self._clicked()
        room_db.clicks(ws, manager=m, closer=lambda *a: (False, "not recorded: nothing held"))
        self.assertEqual(m.get(out["card"]).status, STATUS_IN_PROGRESS)

    def test_the_real_resolve_records_the_close_locally_without_a_room(self):
        ws, m, out = self._clicked()
        with mock.patch.object(room_db, "room_store", return_value=(None, "no room")):
            done = room_db.clicks(ws, manager=m)
        self.assertTrue(done[0]["closed"], done[0]["message"])
        self.assertTrue((Path(out["outbox"]).parent / "closed" / f"{out['ask_id']}.json").is_file())
        self.assertEqual(m.get(out["card"]).status, STATUS_RESOLVED)

    def test_other_runtimes_cards_are_not_this_skills(self):
        ws = _ws()
        m = _manager(ws)
        req = cards.create_choice("q", [("a", "A")], "g", {}, runtime="report-feedback", manager=m)
        m.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision, action_id="a", guard="g"))
        self.assertEqual(ask.answered_cards(m), [])


if __name__ == "__main__":
    unittest.main()
