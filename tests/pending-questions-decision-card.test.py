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
from _helpers.hermetic_env import hermetic_env, isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import ast
import importlib.util
import contextlib
import io
import json
import os
import sys
import subprocess
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
def _ask(*a, **kw):
    """The ask as a caller runs it: hold the question, then store the card."""
    out = ask.queue_question(*a, **kw)
    ask.create_card(out)
    return out


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

# Every wedge card path builds through the helper: pool-wedge-cards.test.py, WedgeCardsUseTheHelper.

    def test_queue_question_builds_its_card_through_the_helper(self):
        ws = _ws("ag2space")
        with mock.patch.object(cards, "choice_request", wraps=cards.choice_request) as cc:
            out = _ask("Merge #1?", workspace=ws, host="h", default_action="merge it",
                       options=OPTIONS, manager=_manager(ws))
        cc.assert_called_once()
        self.assertEqual(cc.call_args.args[2], out["ask_id"])
        self.assertEqual(cc.call_args.args[3], {"ask_id": out["ask_id"],
                                                "choices": {"approve": "merge it", "option-1": "wait for the CLA"}})
        self.assertEqual(cc.call_args.kwargs["runtime"], ask.CARD_RUNTIME)

class QueueQuestion(unittest.TestCase):
    def test_options_bound_for_ag2space_ask_by_card_and_queue_no_text(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = _ask("Merge #1?", "CI is green", workspace=ws, host="h",
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
            out = _ask("Go?", task_file=str(tf), workspace=ws, host="h",
                                     default_action="go", manager=_manager(ws))
        self.assertTrue(out["card"])
        self.assertEqual([(a.label) for a in _manager(ws).get(out["card"]).actions], ["Approve", "Decline"])

    def test_options_alone_are_the_buttons_as_given_with_no_approve(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = _ask("Which port?", workspace=ws, host="h",
                                 options=[("8080", "the dev port"), ("Hold", "wait")], manager=m)
        req = m.get(out["card"])
        self.assertEqual([(a.id, a.label) for a in req.actions], [("option-0", "8080"), ("option-1", "Hold")])
        self.assertNotIn("Approve", req.message)

    def test_no_options_and_no_default_stays_plain_text(self):
        ws = _ws("ag2space")
        out = _ask("What next?", workspace=ws, host="h", manager=_manager(ws))
        self.assertIsNone(out["card"])
        self.assertEqual(_proactive(ws), [out["proactive_file"]])
        self.assertEqual(_manager(ws).active(), [])

    def test_another_bridge_gets_text_since_only_ag2space_projects_cards(self):
        ws = _ws("discord")
        out = _ask("Merge?", workspace=ws, host="h", default_action="merge", options=OPTIONS,
                                 manager=_manager(ws))
        self.assertIsNone(out["card"])
        self.assertTrue(out["proactive_file"])

    def test_a_card_that_cannot_be_stored_falls_back_to_text(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        with mock.patch.object(m, "create", side_effect=OSError("disk full")):
            out = _ask("Merge?", workspace=ws, host="h", default_action="merge",
                                     options=OPTIONS, manager=m)
        self.assertIsNone(out["card"])
        self.assertIn("disk full", out["card_error"])
        self.assertEqual(_proactive(ws), [out["proactive_file"]])
        self.assertEqual(m.store.all(), [], "no card exists beside the text")
        self.assertTrue(any("asked as text instead" in ln for ln in ask.report_lines(out)))

    def test_a_card_that_cannot_be_built_is_asked_as_text(self):
        ws = _ws("ag2space")
        with mock.patch.object(ask, "build_card", side_effect=ValueError("bad options")):
            out = _ask("Merge?", workspace=ws, host="h", default_action="merge", manager=_manager(ws))
        self.assertIn("bad options", out["card_error"])
        self.assertTrue(out["proactive_file"])

    def test_a_text_ask_has_no_card_to_store(self):
        ws = _ws("discord")
        out = ask.queue_question("What next?", workspace=ws, host="h", manager=_manager(ws))
        self.assertEqual(ask.create_card(out), ask.NO_CARD)

    def test_text_that_cannot_be_written_reports_the_send_error(self):
        ws = _ws("discord")
        with mock.patch.object(ask, "write_proactive", side_effect=OSError("results gone")):
            out = _ask("Merge?", workspace=ws, host="h", manager=_manager(ws))
        self.assertIn("results gone", out["send_error"])
        self.assertIn("**Sent:** FAILED", out["sent_line"])


class OrderAndSingleSurface(unittest.TestCase):
    def test_the_card_is_stored_only_after_the_question_is_held_and_names_its_id(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = ask.queue_question("Merge?", workspace=ws, host="h", default_action="merge",
                                 options=OPTIONS, manager=m)
        self.assertEqual(m.store.all(), [], "built, not stored, while only the outbox holds it")
        held = json.loads(Path(out["outbox"]).read_text())
        self.assertIn(f"**Sent:** card {out['card']} for ag2space owner-dm", held["sent"])
        ask.create_card(out)
        self.assertEqual([r.id for r in m.store.all()], [out["card"]])

    def test_once_a_card_exists_no_text_is_sent_whatever_happens_to_its_post(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = _ask("Merge?", workspace=ws, host="h", default_action="merge", options=OPTIONS, manager=m)
        self.assertTrue(m.needs_projection(out["card"]), "never posted")
        self.assertEqual(_proactive(ws), [])
        for gone in ("wait_shown", "_require_shown", "_card_to_text"):
            self.assertFalse(hasattr(ask, gone), f"{gone}: no text path after a card exists")

    def test_a_question_that_cannot_be_held_gets_no_card_only_the_text(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        with mock.patch.object(ask.Outbox, "save", side_effect=OSError("disk full")):
            out = _ask("Merge?", workspace=ws, host="h", default_action="merge", options=OPTIONS, manager=m)
        self.assertIsNone(out["card"])
        self.assertIn("disk full", out["outbox_error"])
        self.assertEqual(m.store.all(), [], "no card without a durable question")
        self.assertEqual(_proactive(ws), [out["proactive_file"]])

    def test_the_store_ask_writes_the_row_before_the_card(self):
        ws = _ws("ag2space")
        order = []
        fake_store = type("Store", (), {"label": "room", "link": None})()
        with mock.patch.object(room_db, "write_question",
                               side_effect=lambda q, st, line: order.append(("row", line)) or mock.Mock(
                                   complete=False, link=None, db_error="test")), \
                mock.patch.object(ask, "create_card", side_effect=lambda out, **k: order.append(("card", out["card"]))):
            out = room_db.ask_owner("Merge?", urgency="durable", workspace=ws, host="h", store=fake_store,
                                    default_action="merge")
        self.assertEqual([o[0] for o in order], ["row", "card"])
        self.assertIn(out["card"], order[0][1], "the row names the card before it exists")


class CardOutcomes(unittest.TestCase):
    def _queued(self, ws, m, **kw):
        tf = ws / "task.txt"
        tf.write_text("task: x\n")
        with mock.patch.object(ask, "destination_from_task",
                               return_value=ask.Destination(bridge="ag2space", channel="!dm:test.invalid")):
            return ask.queue_question("Merge?", task_file=str(tf), workspace=ws, host="h", default_action="merge",
                                      options=OPTIONS, manager=m, link="https://row.test.invalid/7", **kw)

    def test_a_save_that_raises_after_its_rename_is_a_stored_card_and_no_text(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        real = m.store.save

        def save_then_fail(req, projection=None):
            real(req, projection)
            raise OSError("directory fsync failed")
        out = self._queued(ws, m)
        with mock.patch.object(m.store, "save", save_then_fail):
            self.assertEqual(ask.create_card(out), ask.CARD_STORED)
        self.assertIn("the card is there", out["card_note"])
        self.assertNotIn("card_error", {k for k, v in out.items() if v})
        lines = ask.report_lines(out)
        self.assertFalse(any("asked as text" in ln for ln in lines), lines)
        self.assertTrue(any("no text was sent" in ln for ln in lines), lines)
        self.assertEqual(_proactive(ws), [])
        self.assertIsNotNone(m.get(out["card"]))

    def test_an_uncheckable_store_is_unknown_and_kept_as_the_card(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = self._queued(ws, m)
        with mock.patch.object(m, "create", side_effect=OSError("disk")), \
                mock.patch.object(m, "get", side_effect=OSError("unreadable")):
            self.assertEqual(ask.create_card(out), ask.CARD_UNKNOWN)
        self.assertEqual(_proactive(ws), [], "no text beside a card that may exist")
        lines = ask.report_lines(out)
        self.assertFalse(any("asked as text" in ln for ln in lines), lines)
        self.assertTrue(any("could not be checked" in ln and "no text was sent" in ln for ln in lines), lines)

    def test_a_definite_failure_texts_the_same_destination_with_the_link_and_rewrites_the_record(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = self._queued(ws, m)
        with mock.patch.object(m, "create", side_effect=OSError("disk full")):
            self.assertEqual(ask.create_card(out), ask.CARD_NOT_STORED)
        self.assertEqual(_proactive(ws), [out["proactive_file"]])
        self.assertTrue(out["proactive_file"].endswith(".to-ag2space.txt"), out["proactive_file"])
        body = (ws / "results" / out["proactive_file"]).read_text()
        self.assertIn("[channel: !dm:test.invalid]", body)
        self.assertIn("https://row.test.invalid/7", body)
        held = json.loads(Path(out["outbox"]).read_text())
        self.assertEqual(held["sent"], out["sent_line"])
        self.assertIn(f"via {out['proactive_file']}", held["sent"])
        (ws / "results" / out["proactive_file"]).unlink()  # a drain took it
        self.assertTrue(ask.asked_recently(held["sent"], ws / "results"), "the next reminder does not re-ask")

    def test_an_outbox_that_cannot_be_rewritten_is_reported(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = self._queued(ws, m)
        with mock.patch.object(m, "create", side_effect=OSError("disk full")), \
                mock.patch.object(ask.Outbox, "save", side_effect=OSError("outbox gone")):
            ask.create_card(out)
        self.assertIn("not rewritten", out["outbox_error"])

    def test_the_ask_reports_a_card_as_stored_not_yet_shown(self):
        ws = _ws("ag2space")
        out = _ask("Merge?", workspace=ws, host="h", default_action="merge", options=OPTIONS, manager=_manager(ws))
        lines = ask.report_lines(out)
        self.assertTrue(any(ln.startswith("stored, not yet shown: decision card") for ln in lines), lines)
        self.assertFalse(any(ln.startswith("sent:") for ln in lines), lines)


class CardPointer(unittest.TestCase):
    def _live(self, posted=False):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = _ask("Merge #7 into main?", workspace=ws, host="h", default_action="merge it",
                   options=[("Hold", "wait for the CLA")], manager=m)
        req = m.get(out["card"])
        if posted:
            m.record_projection(req.id, req.revision, "$ev")
        q = {"ask_id": out["ask_id"], "title": "Merge #7 into main?", "body": out["sent_line"]}
        return ws, m, req, q

    def test_the_pointer_goes_out_after_the_grace_and_not_before(self):
        ws, m, req, q = self._live()
        early = req.created_at + ask.CARD_POINTER_GRACE_SEC - 1
        self.assertEqual(ask.send_card_pointers([q], ws, now=early, manager=m, host="h"), [])
        self.assertEqual(_proactive(ws), [])
        late = req.created_at + ask.CARD_POINTER_GRACE_SEC
        [(ask_id, name)] = ask.send_card_pointers([q], ws, now=late, manager=m, host="h")
        self.assertEqual((ask_id, _proactive(ws)), (q["ask_id"], [name]))
        self.assertEqual(m.get(req.id).status, "pending", "the card stays the only surface")

    def test_two_reminder_passes_send_a_single_pointer_recorded_on_the_card(self):
        ws, m, req, q = self._live()
        t = req.created_at + ask.CARD_POINTER_GRACE_SEC
        self.assertEqual(len(ask.send_card_pointers([q], ws, now=t, manager=m, host="h")), 1)
        stored = json.loads(m.store._path(req.id).read_text())
        self.assertEqual(stored["requirement"]["subject"]["pointer_sent_at"], t, "durable on the card")
        self.assertEqual(ask.send_card_pointers([q], ws, now=t + 10 * ask.SENT_QUIET_SEC, host="h"), [],
                         "a second pass, with a fresh store handle, sends nothing")
        self.assertEqual(len(_proactive(ws)), 1)

    def test_never_for_a_posted_or_settled_card_or_a_text_ask(self):
        ws, m, req, q = self._live(posted=True)
        self.assertEqual(ask.send_card_pointers([q], ws, now=req.created_at + 99999, manager=m, host="h"), [])
        ws, m, req, q = self._live()
        m.cancel(req.id)
        self.assertEqual(ask.send_card_pointers([q], ws, now=req.created_at + 99999, manager=m, host="h"), [])
        text_q = {"ask_id": "a", "title": "t", "body": "**Sent:** queued owner-dm via proactive-a.txt at 2026-01-01T00:00:00Z"}
        self.assertEqual(ask.send_card_pointers([text_q], ws, manager=m, host="h"), [])

    def test_the_pointer_is_not_a_decision_surface(self):
        ws, m, req, q = self._live()
        [(_, name)] = ask.send_card_pointers([q], ws, now=req.created_at + ask.CARD_POINTER_GRACE_SEC,
                                             manager=m, host="h")
        body = (ws / "results" / name).read_text()
        self.assertIn("Merge #7 into main?", body)
        self.assertIn("decision card", body)
        self.assertIn("may not have arrived", body)
        for word in ("Approve", "Hold", "merge it", "wait for the CLA", "Decline", "Reply here", "->"):
            self.assertNotIn(word, body, f"pointer must carry no option or answer wording: {word}")
        self.assertTrue(body.startswith("[dm-only]"))

    def test_a_pointer_that_cannot_be_written_is_reported(self):
        ws, m, req, q = self._live()
        with mock.patch.object(ask, "write_proactive", side_effect=OSError("results gone")):
            [(_, sent)] = ask.send_card_pointers([q], ws, now=req.created_at + ask.CARD_POINTER_GRACE_SEC,
                                                 manager=m, host="h")
        self.assertIn("results gone", sent)
        t = req.created_at + ask.CARD_POINTER_GRACE_SEC
        subject = m.get(req.id).subject
        self.assertEqual((subject.get("pointer_lease"), subject.get("pointer_sent_at")),
                         ({"observed_at": t, "elapsed": 0.0}, None))
        self.assertEqual(ask.send_card_pointers([q], ws, now=t + 60, manager=m, host="h"), [],
                         "a fresh claim is not retried yet")
        ask.send_card_pointers([q], ws, now=t + ask.POINTER_MAX_STEP_SEC + 60, manager=m, host="h")
        [(_, name)] = ask.send_card_pointers([q], ws, now=t + ask.POINTER_CLAIM_RETRY_SEC + 60, manager=m, host="h")
        self.assertEqual(_proactive(ws), [name], "the failed publish is retried once its claim is old")
        self.assertTrue(m.get(req.id).subject.get("pointer_sent_at"))
        self.assertEqual(ask.send_card_pointers([q], ws, now=t + 99999, manager=m, host="h"), [], "then never again")

    def test_the_reminder_runs_the_pointer_step(self):
        rem_spec = importlib.util.spec_from_file_location("pq_remind_p", PQ / "pending_questions_remind.py")
        rem = importlib.util.module_from_spec(rem_spec)
        rem_spec.loader.exec_module(rem)
        self.assertIs(rem.send_card_pointers, ask.send_card_pointers)
        ws, m, req, q = self._live()
        g = {"waiting": [q], "notes": [], "unavailable": False}
        with mock.patch.object(rem, "gather", return_value=g), \
                mock.patch.object(rem, "presenter_mode_active", return_value=False), \
                mock.patch.object(rem, "send_card_pointers", return_value=[(q["ask_id"], "proactive-x.txt")]) as sp, \
                mock.patch.object(rem, "deliver") as dv:
            rem.main(["--notify"], workspace=ws)
        sp.assert_called_once()
        dv.assert_not_called()

    def _flagless(self, presenter):
        """The unattended pass (no `--notify`) over a real card past its grace; only gather is stubbed."""
        rem_spec = importlib.util.spec_from_file_location("pq_remind_u", PQ / "pending_questions_remind.py")
        rem = importlib.util.module_from_spec(rem_spec)
        rem_spec.loader.exec_module(rem)
        ws, m, req, q = self._live()
        req.created_at -= ask.CARD_POINTER_GRACE_SEC + 60
        m.store.save(req)
        g = {"waiting": [q], "notes": [], "unavailable": False}
        buf = io.StringIO()
        with mock.patch.object(rem, "gather", return_value=g), \
                mock.patch.object(rem, "presenter_mode_active", return_value=presenter), \
                mock.patch.object(rem, "deliver") as dv, contextlib.redirect_stdout(buf):
            rem.main([], workspace=ws)
        dv.assert_not_called()  # the flagless pass never sends a reminder
        return ws, m, req, buf.getvalue()

    def test_the_unattended_pass_points_to_a_card_that_never_posted(self):
        ws, m, req, out = self._flagless(presenter=False)
        self.assertEqual(len(_proactive(ws)), 1, out)
        self.assertTrue(m.get(req.id).subject.get("pointer_sent_at"))
        self.assertIn("card pointer:", out)
        self.assertIn("no reminder sent", out)

    def test_presenter_mode_holds_the_pointer_for_a_later_pass(self):
        ws, m, req, _ = self._flagless(presenter=True)
        self.assertEqual(_proactive(ws), [])
        self.assertIsNone(m.get(req.id).subject.get("pointer_claim"), "nothing claimed: a later pass sends it")


class PointerEdges(unittest.TestCase):
    def _live(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out = _ask("Merge #7?", workspace=ws, host="h", default_action="merge it", options=OPTIONS, manager=m)
        req = m.get(out["card"])
        return ws, m, req, {"ask_id": out["ask_id"], "title": "Merge #7?", "body": out["sent_line"]}

    def test_marking_sent_never_revives_a_settled_or_archived_card(self):
        ws, m, req, q = self._live()
        token = ask.claim_pointer(m, req.id, req.created_at + ask.CARD_POINTER_GRACE_SEC)
        m.resolve(req.id)
        self.assertTrue(m.store.archive(req.id))
        ask.mark_pointer_sent(m, req.id, token, req.created_at + ask.CARD_POINTER_GRACE_SEC + 1)
        self.assertFalse(m.store._path(req.id).exists(), "the archived card is not written back live")
        self.assertIsNone(m.get(req.id).subject.get("pointer_sent_at"))

    def test_a_terminal_card_is_not_marked_either(self):
        ws, m, req, q = self._live()
        token = ask.claim_pointer(m, req.id, req.created_at + ask.CARD_POINTER_GRACE_SEC)
        m.cancel(req.id)
        before = m.get(req.id).revision
        ask.mark_pointer_sent(m, req.id, token, req.created_at + ask.CARD_POINTER_GRACE_SEC + 1)
        self.assertEqual(m.get(req.id).revision, before)
        self.assertIsNone(m.get(req.id).subject.get("pointer_sent_at"))

    def test_a_clock_that_went_back_cannot_steal_a_fresh_claim(self):
        ws, m, req, q = self._live()
        t = req.created_at + ask.CARD_POINTER_GRACE_SEC + 10
        self.assertTrue(ask.claim_pointer(m, req.id, t))
        self.assertIsNone(ask.claim_pointer(m, req.id, t - 1), "a backward step credits nothing")
        self.assertIsNone(ask.claim_pointer(m, req.id, t - 5))
        self.assertEqual(m.get(req.id).subject["pointer_lease"], {"observed_at": t - 5, "elapsed": 0.0})
        self.assertIsNone(ask.claim_pointer(m, req.id, t), "the catch-up over-credits the 5 s, at most a step")
        self.assertEqual(m.get(req.id).subject["pointer_lease"]["elapsed"], 5.0)

    def test_the_pointer_claim_lease_table(self):
        """The same invariant as the click lease, at W=300, step=150: around the window, a rollback,
        a year's spike then correction, and a renewal."""
        ws, m, req, q = self._live()
        t = req.created_at + ask.CARD_POINTER_GRACE_SEC + 10
        year = 365 * 86400
        first = ask.claim_pointer(m, req.id, t)
        cases = [  # (pass time, claim taken?, credited after the pass, why)
            (t + 150, False, 150.0, "one step"),
            (t + 299, False, 299.0, "W - 1"),
            (t + 298, False, 299.0, "1 s back: credits 0, no steal"),
            (t + 300, True, 0.0, "the catch-up over-credits at most a step: a new claim, a new lease"),
            (t + 299, False, 0.0, "1 s back after the renewal: the fresh claim is kept"),
            (t + 300 + year, False, 150.0, "a year's spike credits one step only"),
            (t + 300, False, 150.0, "corrected: credits 0"),
            (t + 450, True, 0.0, "normal time resumes: no freeze"),
        ]
        tokens = [first]
        for now, takes, credited, why in cases:
            with self.subTest(why=why):
                got = ask.claim_pointer(m, req.id, now)
                self.assertEqual(bool(got), takes)
                if got:
                    tokens.append(got)
                self.assertEqual(m.get(req.id).subject["pointer_lease"]["elapsed"], credited)
        self.assertEqual(len(set(tokens)), 3, "two replacements, each at a spent lease")

    def test_a_claim_stamped_just_under_a_window_ahead_is_replaced_one_window_after_first_sight(self):
        ws, m, req, q = self._live()
        t = req.created_at + ask.CARD_POINTER_GRACE_SEC + 10
        self.assertTrue(ask.claim_pointer(m, req.id, t + ask.POINTER_CLAIM_RETRY_SEC - 1))
        for now in (t, t + 150, t + ask.POINTER_CLAIM_RETRY_SEC - 1):
            self.assertIsNone(ask.claim_pointer(m, req.id, now), "never replaced on sight")
        self.assertTrue(ask.claim_pointer(m, req.id, t + ask.POINTER_CLAIM_RETRY_SEC), "one window after first sight")

    def test_every_pass_credits_a_claim_and_grace_only_gates_the_replacement(self):
        """keweichen's trace through send_card_pointers: a claim at C+1810 whose publish failed, the
        clock corrected to C, then C+150, C+300, C+1800 (C = the card's creation, grace 1800 s)."""
        ws, m, req, q = self._live()
        C = req.created_at
        with mock.patch.object(ask, "write_proactive", side_effect=OSError("results full")):
            ask.send_card_pointers([q], ws, now=C + 1810, manager=m, host="h")
        self.assertEqual(m.get(req.id).subject["pointer_lease"], {"observed_at": C + 1810, "elapsed": 0.0})
        trace = []
        for now in (C, C + 150, C + 300):
            self.assertEqual(ask.send_card_pointers([q], ws, now=now, manager=m, host="h"), [], "before grace")
            trace.append(m.get(req.id).subject["pointer_lease"]["elapsed"])
        self.assertEqual(trace, [0.0, 150.0, 300.0], "lease progress is credited and saved through the correction")
        self.assertEqual([n for n in _proactive(ws) if "pointer" in n], [], "no pointer before grace")
        claim_before = m.get(req.id).subject["pointer_claim"]
        sent = ask.send_card_pointers([q], ws, now=C + 1800, manager=m, host="h")
        self.assertEqual(len(sent), 1, "the first grace-eligible pass replaces the spent claim")
        self.assertNotEqual(m.get(req.id).subject["pointer_claim"], claim_before)
        self.assertEqual(len([n for n in _proactive(ws) if "pointer" in n]), 1, "exactly one pointer")

    def test_a_card_posted_after_the_claim_and_before_the_write_gets_no_pointer(self):
        ws, m, req, q = self._live()
        real = ask.claim_pointer

        def claim_then_post(mgr, card_id, now):
            token = real(mgr, card_id, now)
            mgr.record_projection(card_id, mgr.get(card_id).revision, "$posted")  # the projector's ledger
            return token
        with mock.patch.object(ask, "claim_pointer", claim_then_post):
            self.assertEqual(ask.send_card_pointers([q], ws, now=req.created_at + ask.CARD_POINTER_GRACE_SEC,
                                                    manager=m, host="h"), [])
        self.assertEqual([n for n in _proactive(ws) if "pointer" in n], [])
        self.assertIsNone(m.get(req.id).subject.get("pointer_sent_at"))


class ReminderNeverTextsACard(unittest.TestCase):
    def _rem(self):
        spec = importlib.util.spec_from_file_location("pq_remind_f", PQ / "pending_questions_remind.py")
        rem = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rem)
        return rem

    def _run(self, rem, ws, questions, argv):
        g = {"waiting": questions, "notes": [], "unavailable": False}
        with mock.patch.object(rem, "gather", return_value=g), \
                mock.patch.object(rem, "presenter_mode_active", return_value=False), \
                mock.patch.object(rem, "should_notify", return_value=True), \
                mock.patch.object(rem, "write_notify_stamp"), \
                mock.patch.object(rem, "deliver", return_value="delivered") as dv:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rem.main(argv, workspace=ws)
        return dv, buf.getvalue()

    def test_force_does_not_text_a_question_whose_card_is_live(self):
        rem = self._rem()
        ws = _ws("ag2space")
        out = _ask("Merge?", workspace=ws, host="h", default_action="merge", options=OPTIONS, manager=_manager(ws))
        carded = {"ask_id": out["ask_id"], "title": "Merge?", "body": out["sent_line"]}
        dv, text = self._run(rem, ws, [carded], ["--notify", "--force"])
        dv.assert_not_called()
        self.assertIn("wait as live decision cards; no text sent", text)

    def test_a_mixed_list_texts_only_the_questions_without_a_live_card(self):
        rem = self._rem()
        ws = _ws("ag2space")
        out = _ask("Merge?", workspace=ws, host="h", default_action="merge", options=OPTIONS, manager=_manager(ws))
        carded = {"ask_id": out["ask_id"], "title": "Merge?", "body": out["sent_line"]}
        plain = {"ask_id": "ask-plain", "title": "Plain?", "body": "no record"}
        dv, _ = self._run(rem, ws, [carded, plain], ["--notify", "--force"])
        dv.assert_called_once()
        self.assertEqual([q["ask_id"] for q in dv.call_args.args[0]], ["ask-plain"])


class ReminderSeesTheCard(unittest.TestCase):
    def _body(self, ws, m):
        out = _ask("Merge?", workspace=ws, host="h", default_action="merge", options=OPTIONS, manager=m)
        return out, "# Request\n\nMerge?\n\n# Delivery\n\n" + out["sent_line"] + "\n"

    def test_the_reminder_never_texts_beside_a_live_card_posted_or_not(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out, body = self._body(ws, m)
        _, at = ask.card_sent(body)
        self.assertTrue(ask.asked_recently(body, ws / "results", now=at + 10 * ask.SENT_QUIET_SEC), "unposted")
        m.record_projection(out["card"], m.get(out["card"]).revision, "$ev", {"notified_at": at + 500})
        self.assertTrue(ask.asked_recently(body, ws / "results", now=at + 10 * ask.SENT_QUIET_SEC), "posted")
        self.assertEqual(ask.sent_at(body, ws), at + 500, "recency from the gateway's post")
        self.assertEqual(ask.sent_at(body), at, "no workspace: the record's stamp")

    def test_a_card_that_is_gone_leaves_the_question_due(self):
        ws = _ws("ag2space")
        m = _manager(ws)
        out, body = self._body(ws, m)
        m.cancel(out["card"])
        self.assertFalse(ask.asked_recently(body, ws / "results"))
        absent = body.replace(out["card"], "hitl_000000000000")
        self.assertFalse(ask.asked_recently(absent, ws / "results"))
        self.assertEqual(ask.sent_at(body, ws), ask.card_sent(body)[1])

    def test_a_store_that_cannot_be_read_is_never_texted_beside(self):
        ws = _ws("ag2space")
        out, body = self._body(ws, _manager(ws))
        with mock.patch("hitl.cards.manager_for", side_effect=OSError("store gone")):
            self.assertTrue(ask.asked_recently(body, ws / "results"))
            self.assertFalse(ask.card_posted(ws, out["card"]))

    def test_the_reminder_uses_this_check(self):
        rem_spec = importlib.util.spec_from_file_location("pq_remind_t", PQ / "pending_questions_remind.py")
        rem = importlib.util.module_from_spec(rem_spec)
        rem_spec.loader.exec_module(rem)
        self.assertIs(rem.asked_recently, ask.asked_recently)

    def test_a_bad_stamp_is_no_record(self):
        self.assertIsNone(ask.card_sent("**Sent:** card hitl_x for ag2space at 2026-13-99T99:99:99Z"))
        self.assertIsNone(ask.card_sent("no record"))


def _room_db_suite():
    spec = importlib.util.spec_from_file_location("pq_room_db_suite", REPO / "tests" / "pending-questions-room-db.test.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CHILD = """
import os, sys
from pathlib import Path
from unittest import mock
sys.path[:0] = [{src!r}, {pq!r}]
import pending_questions_ask as ask, pending_questions_room_db as rdb
ws = Path({ws!r})
store, where = rdb.room_store(ws)
assert store is not None, where
{body}
"""


class CrashBoundaries(unittest.TestCase):
    """A real process death (os._exit) at each boundary, against the production stores:
    the outbox, the HITL store and the room database through the adapter's own serve."""

    def setUp(self):
        self.rdbt = _room_db_suite()
        self.ws = _ws("ag2space")
        self.rdbt._install_fake_capability(self.ws)
        keys = {"FAKE_ROOM_STATE": str(self.ws / "room.json"), "SUTANDO_HOST_LABEL": self.rdbt.HOST}
        self.env = hermetic_env(**keys)
        patch = mock.patch.dict("os.environ", keys)
        patch.start()
        self.addCleanup(patch.stop)

    def child(self, body):
        r = subprocess.run([sys.executable, "-c", self.code(body)], env=self.env, capture_output=True, text=True,
                           timeout=120)
        self.assertEqual(r.returncode, 17, r.stderr[-2000:])
        return r.stdout

    def code(self, body):
        return CHILD.format(src=str(REPO / "src"), pq=str(PQ), ws=str(self.ws), body=body)

    def clicked(self, action_id="option-1"):
        store, _ = room_db.room_store(self.ws)
        out = room_db.ask_owner("Merge #12?", urgency="durable", workspace=self.ws, host=self.rdbt.HOST,
                                store=store, default_action="merge it", options=[("Hold", "wait")])
        m = _manager(self.ws)
        req = m.get(out["card"])
        m.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision, action_id=action_id,
                                   guard=req.guard))
        return store, out, m

    def test_a_definite_card_failure_rewrites_the_row_so_the_reminder_does_not_reask(self):
        store, _ = room_db.room_store(self.ws)
        with mock.patch("hitl.manager.HitlManager.create", side_effect=OSError("disk full")):
            out = room_db.ask_owner("Merge #12?", urgency="durable", workspace=self.ws, host=self.rdbt.HOST,
                                    store=store, default_action="merge it", options=[("Hold", "wait")])
        self.assertEqual(out["card_outcome"], ask.CARD_NOT_STORED)
        [row] = store.entries()
        self.assertIn(f"via {out['proactive_file']}", row["body"])
        self.assertNotIn("**Sent:** card", row["body"])
        (self.ws / "results" / out["proactive_file"]).unlink()  # a drain took it
        self.assertTrue(ask.asked_recently(row["body"], self.ws / "results"), "not re-asked")

    def ask(self, store, **patches):
        return room_db.ask_owner("Merge #12?", urgency="durable", workspace=self.ws, host=self.rdbt.HOST,
                                 store=store, default_action="merge it", options=[("Hold", "wait")])

    def held(self):
        return {e["ask_id"]: e for e in ask.Outbox(self.ws).entries()}

    def test_a_row_rewrite_that_fails_is_held_and_the_next_reconcile_rewrites_it(self):
        store, _ = room_db.room_store(self.ws)
        with mock.patch("hitl.manager.HitlManager.create", side_effect=OSError("disk full")), \
                mock.patch.object(type(store), "redeliver", side_effect=OSError("room gone")):
            out = self.ask(store)
        self.assertIn("held for reconcile", out["db_error"])
        held = self.held()[out["ask_id"]]
        self.assertTrue(held["redeliver"])
        self.assertIn(f"via {out['proactive_file']}", held["sent"])
        [row] = store.entries()
        self.assertIn("**Sent:** card", row["body"], "not yet rewritten")
        rec = room_db.reconcile_pass(self.ws)
        self.assertEqual(rec["errors"], [])
        [row] = store.entries()
        self.assertIn(f"via {out['proactive_file']}", row["body"])
        self.assertNotIn(out["ask_id"], self.held(), "the held entry goes once the row carries it")

    def test_an_unconfirmed_row_write_is_reconciled_to_the_text_record(self):
        store, _ = room_db.room_store(self.ws)
        with mock.patch("hitl.manager.HitlManager.create", side_effect=OSError("disk full")), \
                mock.patch.object(type(store), "complete", return_value=False):
            out = self.ask(store)
        self.assertTrue(str(out["record"]).startswith("outbox "), out["record"])
        self.assertTrue(self.held()[out["ask_id"]]["redeliver"])
        room_db.reconcile_pass(self.ws)
        [row] = store.entries()
        self.assertIn(f"via {out['proactive_file']}", row["body"])
        self.assertNotIn("**Sent:** card", row["body"])
        self.assertNotIn(out["ask_id"], self.held())

    def test_the_fallback_carries_the_rows_own_link_not_the_one_guessed_before(self):
        store, _ = room_db.room_store(self.ws)
        real = room_db.write_question

        def write_with_new_link(q, st, line):
            w = real(q, st, line)
            w.link = "https://row.test.invalid/after-write"
            return w
        with mock.patch("hitl.manager.HitlManager.create", side_effect=OSError("disk full")), \
                mock.patch.object(room_db, "write_question", write_with_new_link):
            out = self.ask(store)
        body = (self.ws / "results" / out["proactive_file"]).read_text()
        self.assertIn("https://row.test.invalid/after-write", body)

    def test_death_after_the_hold_before_the_row_rewrite_is_reconciled(self):
        self.child("with mock.patch('hitl.manager.HitlManager.create', side_effect=OSError('disk full')), \\\n"
                   "        mock.patch.object(type(store), 'redeliver', side_effect=lambda *a: os._exit(17)):\n"
                   f"    rdb.ask_owner('Merge #12?', urgency='durable', workspace=ws, host={self.rdbt.HOST!r}, "
                   "store=store, default_action='merge it', options=[('Hold', 'wait')])")
        store, _ = room_db.room_store(self.ws)
        [entry] = self.held().values()
        self.assertTrue(entry["redeliver"])
        room_db.reconcile_pass(self.ws)
        [row] = store.entries()
        self.assertIn(entry["sent"].split(" at ")[0], row["body"])
        self.assertEqual(self.held(), {})

    def test_death_between_the_fallback_text_and_its_record_texts_again_never_silence(self):
        self.child("real = ask.write_proactive\n"
                   "def write_then_die(*a, **k):\n    real(*a, **k)\n    os._exit(17)\n"
                   "with mock.patch('hitl.manager.HitlManager.create', side_effect=OSError('disk full')), \\\n"
                   "        mock.patch.object(ask, 'write_proactive', write_then_die):\n"
                   f"    rdb.ask_owner('Merge #12?', urgency='durable', workspace=ws, host={self.rdbt.HOST!r}, "
                   "store=store, default_action='merge it', options=[('Hold', 'wait')])")
        store, _ = room_db.room_store(self.ws)
        [row] = store.entries()
        self.assertIn("**Sent:** card", row["body"], "the record still names the card that was never stored")
        self.assertEqual(len(_proactive(self.ws)), 1, "the text went out")
        self.assertFalse(ask.asked_recently(row["body"], self.ws / "results"), "due: text again, not silence")

    def ask_in_child(self, die_at):
        self.child(f"with mock.patch.object({die_at}, side_effect=lambda *a, **k: os._exit(17)):\n"
                   f"    rdb.ask_owner('Merge #12?', urgency='durable', workspace=ws, host={self.rdbt.HOST!r}, "
                   "store=store, default_action='merge it', options=[('Hold', 'wait')])")

    def _carded(self):
        store, _ = room_db.room_store(self.ws)
        out = room_db.ask_owner("Merge #12?", urgency="durable", workspace=self.ws, host=self.rdbt.HOST,
                                store=store, default_action="merge it", options=[("Hold", "wait")])
        req = _manager(self.ws).get(out["card"])
        q = {"ask_id": out["ask_id"], "title": "Merge #12?", "body": out["sent_line"]}
        return out, req, q

    def pointer_child(self, q, now, extra=""):
        return self.code(extra + f"res = ask.send_card_pointers([{q!r}], ws, now={now!r}, host='h')\n"
                         "print(len(res), flush=True)\nos._exit(17)")

    def test_death_after_the_pointer_claim_is_retried_later_and_delivers_once(self):
        out, req, q = self._carded()
        now = req.created_at + ask.CARD_POINTER_GRACE_SEC
        self.child(f"with mock.patch.object(ask, 'write_proactive', side_effect=lambda *a, **k: os._exit(17)):\n"
                   f"    ask.send_card_pointers([{q!r}], ws, now={now!r}, host='h')")
        subject = _manager(self.ws).get(req.id).subject
        self.assertEqual((subject.get("pointer_lease"), subject.get("pointer_sent_at")),
                         ({"observed_at": now, "elapsed": 0.0}, None), "the claim and its lease landed before the death")
        self.assertEqual([n for n in _proactive(self.ws) if "pointer" in n], [])
        for inside in (now + ask.POINTER_MAX_STEP_SEC, now + ask.POINTER_CLAIM_RETRY_SEC - 1):
            out_before = subprocess.run([sys.executable, "-c", self.pointer_child(q, inside)], env=self.env,
                                        capture_output=True, text=True, timeout=120)
            self.assertEqual(out_before.stdout.strip(), "0", "not retried inside the claim window")
        later = now + ask.POINTER_CLAIM_RETRY_SEC
        retried = subprocess.run([sys.executable, "-c", self.pointer_child(q, later)], env=self.env,
                                 capture_output=True, text=True, timeout=120)
        self.assertEqual((retried.returncode, retried.stdout.strip()), (17, "1"), retried.stderr[-1500:])
        again = subprocess.run([sys.executable, "-c", self.pointer_child(q, later + 99999)], env=self.env,
                               capture_output=True, text=True, timeout=120)
        self.assertEqual(again.stdout.strip(), "0")
        self.assertEqual(len([n for n in _proactive(self.ws) if "pointer" in n]), 1, "exactly one across retries")

    def test_a_pointer_publish_that_fails_in_a_real_process_is_retried(self):
        out, req, q = self._carded()
        now = req.created_at + ask.CARD_POINTER_GRACE_SEC
        failed = subprocess.run([sys.executable, "-c", self.pointer_child(
            q, now, "ask.write_proactive = lambda *a, **k: (_ for _ in ()).throw(OSError('results full'))\n")],
            env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(failed.returncode, 17, failed.stderr[-1500:])
        self.assertIsNone(_manager(self.ws).get(req.id).subject.get("pointer_sent_at"))
        subprocess.run([sys.executable, "-c", self.pointer_child(q, now + ask.POINTER_MAX_STEP_SEC)],
                       env=self.env, capture_output=True, text=True, timeout=120)
        retried = subprocess.run([sys.executable, "-c", self.pointer_child(q, now + ask.POINTER_CLAIM_RETRY_SEC)],
                                 env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(retried.stdout.strip(), "1", retried.stderr[-1500:])
        self.assertEqual(len([n for n in _proactive(self.ws) if "pointer" in n]), 1)

    def test_two_overlapping_reminders_queue_one_pointer(self):
        out, req, q = self._carded()
        now = req.created_at + ask.CARD_POINTER_GRACE_SEC
        slow = ("import time\nreal = ask.write_proactive\n"
                "def slow(*a, **k):\n    time.sleep(1.0)\n    return real(*a, **k)\n"
                "ask.write_proactive = slow\n")
        procs = [subprocess.Popen([sys.executable, "-c", self.pointer_child(q, now, slow)], env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outs = [p.communicate(timeout=120) for p in procs]
        self.assertEqual([p.returncode for p in procs], [17, 17], "".join(e[-1500:] for _, e in outs))
        self.assertEqual(sorted(int(o.strip()) for o, _ in outs), [0, 1], "exactly one claimant published")
        self.assertEqual(len([n for n in _proactive(self.ws) if "pointer" in n]), 1)

    def test_a_card_posted_before_the_claim_gets_no_pointer(self):
        out, req, q = self._carded()
        m = _manager(self.ws)
        real = ask.claim_pointer

        def post_then_claim(mgr, card_id, now):
            m.record_projection(card_id, m.get(card_id).revision, "$posted")  # the projector wins the race
            return real(mgr, card_id, now)
        with mock.patch.object(ask, "claim_pointer", post_then_claim):
            self.assertEqual(ask.send_card_pointers([q], self.ws, now=req.created_at + ask.CARD_POINTER_GRACE_SEC,
                                                    manager=m, host="h"), [])
        self.assertEqual([n for n in _proactive(self.ws) if "pointer" in n], [])

    def test_a_fallback_with_history_unwritable_keeps_the_hold_and_never_counts_zero(self):
        store, _ = room_db.room_store(self.ws)
        ro = OSError("state marker read-only")
        with mock.patch("hitl.manager.HitlManager.create", side_effect=OSError("disk full")), \
                mock.patch.object(room_db, "mark_store_used", side_effect=ro), \
                mock.patch("pending_questions_store.mark_store_used", side_effect=ro):
            out = room_db.ask_owner("Merge #12?", urgency="durable", workspace=self.ws, host=self.rdbt.HOST,
                                    store=store, default_action="merge it", options=[("Hold", "wait")])
        self.assertIn("read-only", out["history_error"])
        held = {e["ask_id"]: e for e in ask.Outbox(self.ws).entries()}
        self.assertIn(out["ask_id"], held, "the hold is the evidence; it stays")
        self.assertTrue(held[out["ask_id"]]["redeliver"])
        with mock.patch.object(room_db, "room_store", return_value=(None, "room unreachable")):
            g = room_db.gather(self.ws)
        self.assertTrue(g["unavailable"] or out["ask_id"] in [w["ask_id"] for w in g["waiting"]], g)
        self.assertNotEqual((g["unavailable"], len(g["waiting"])), (False, 0), "never a measured zero")
        room_db.reconcile_pass(self.ws)
        [row] = store.entries()
        self.assertIn(f"via {out['proactive_file']}", row["body"])
        self.assertEqual(ask.Outbox(self.ws).entries(), [], "released once history is committed")

    def test_death_while_holding_the_question_leaves_no_card(self):
        self.ask_in_child("ask.Outbox, 'save'")
        self.assertEqual(_manager(self.ws).store.all(), [], "no card without a durable question")

    def test_death_after_the_row_before_the_card_leaves_a_row_the_reminder_resurfaces(self):
        self.ask_in_child("ask, 'create_card'")
        self.assertEqual(_manager(self.ws).store.all(), [])
        store, _ = room_db.room_store(self.ws)
        [row] = store.entries()
        self.assertRegex(row["body"], r"\*\*Sent:\*\* card hitl_\w+ for ag2space owner-dm")
        self.assertFalse(ask.asked_recently(row["body"], self.ws / "results"), "not a send: resurfaces")

class EdgePaths(unittest.TestCase):
    def test_the_card_carries_the_row_link(self):
        msg = ask.card_message("Q?", None, "go", None, (), link="https://row.test.invalid/1")
        self.assertIn("https://row.test.invalid/1", msg)

if __name__ == "__main__":
    unittest.main()
