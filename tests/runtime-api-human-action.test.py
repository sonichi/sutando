#!/usr/bin/env python3
"""Tests for the human_action.* surface (third HITL request type).

Contract (owner taxonomy 2026-08-08): a human_action request is a durable,
addressable object for a real-world act a person must perform. Card answers
Done/Decline map to completed/declined; the API completion path resolves the
request AND closes the mirrored card; complete/decline reject non-human_action
requests and are idempotent-safe on terminal ones.

Run: python3 tests/runtime-api-human-action.test.py
Exit: 0 on pass, 1 on fail.
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

from dispatcher import RuntimeDispatcher  # noqa: E402
from request_store import RequestStore  # noqa: E402

from ha_adapter import HumanActionAdapter  # noqa: E402
from protocol import ProtocolError  # noqa: E402

SETTLE_METHODS = ("human_action.complete", "human_action.decline")


class HumanActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.store = RequestStore(str(base / "rt.sqlite"))
        self.ha = HumanActionAdapter(str(base / "ha"))
        self.d = RuntimeDispatcher(self.store, self.ha, "@agent:x", executors={})
        # Settling is an authorized-device action, so the API-settle tests
        # drive a GRANTED dispatcher; `self.d` stays ungranted like a Unix client.
        self.granted = RuntimeDispatcher(
            self.store, self.ha, "@agent:x", executors={},
            granted_methods=frozenset(SETTLE_METHODS))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _request(self, **extra) -> str:
        r = asyncio.run(self.d.handle("human_action.request",
                                      {"action": "Sign the agreement",
                                       "instructions": "Review section 4 first",
                                       **extra}))
        self.assertEqual(r["status"], "pending")
        return r["requestId"]

    def test_request_mirrors_card_with_act_and_outcome_options(self):
        rid = self._request()
        req = self.ha.manager.get(self.d._ha_of[rid])
        self.assertIn("Sign the agreement", req.message)
        self.assertIn("Review section 4", req.message)
        self.assertEqual([a.label for a in req.actions], ["Done", "Decline"])

    def test_card_done_completes_and_decline_declines(self):
        for answer, expected in ((1, "completed"), (2, "declined")):
            rid = self._request()
            aid = self.d._ha_of[rid]
            self.ha.resolve(aid, {"1": [answer]}, "@owner:x")
            self.d._settle(rid)
            rec = self.store.get(rid)
            self.assertEqual(rec["status"], expected)
            self.assertEqual(rec["resolvedBy"], "@owner:x")

    def test_api_complete_resolves_request_and_closes_card(self):
        rid = self._request()
        out = asyncio.run(self.granted.handle("human_action.complete",
                                              {"requestId": rid, "note": "signed"}))
        self.assertEqual(out["status"], "completed")
        self.assertEqual(self.store.get(rid)["result"], {"note": "signed"})
        self.assertEqual(self.ha.manager.get(self.d._ha_of[rid]).status, "resolved")  # no dangling card

    def test_api_decline_and_terminal_is_idempotent_safe(self):
        rid = self._request()
        out = asyncio.run(self.granted.handle("human_action.decline",
                                              {"requestId": rid}))
        self.assertEqual(out["status"], "declined")
        again = asyncio.run(self.granted.handle("human_action.complete",
                                                {"requestId": rid}))
        self.assertTrue(again["alreadyTerminal"])
        self.assertEqual(again["status"], "declined")  # terminal state unchanged

    def test_complete_rejects_wrong_type_and_unknown(self):
        r = asyncio.run(self.d.handle("approval.request", {"action": "x"}))
        for rid in (r["requestId"], "nope"):
            with self.assertRaises(ProtocolError) as cm:
                asyncio.run(self.granted.handle("human_action.complete",
                                                {"requestId": rid}))
            # -32602 (bad param), NOT the -32601 grant refusal: the argument
            # checks must still be reachable for an authorized caller.
            self.assertEqual(cm.exception.code, -32602)

    def test_ungranted_caller_cannot_settle_and_leaves_the_row_pending(self):
        rid = self._request()
        with self.assertRaises(ProtocolError) as cm:
            asyncio.run(self.d.handle("human_action.complete",
                                      {"requestId": rid}))
        self.assertEqual(cm.exception.code, -32601)
        self.assertEqual(self.store.get(rid)["status"], "pending")
        self.assertEqual(self.ha.manager.get(self.d._ha_of[rid]).status,
                         "pending")
        with self.assertRaises(ProtocolError):
            asyncio.run(self.d.handle("human_action.decline", {"requestId": rid}))
        self.assertEqual(self.store.get(rid)["status"], "pending")

    def test_status_returns_request_state(self):
        rid = self._request()
        st = asyncio.run(self.d.handle("human_action.status", {"requestId": rid}))
        self.assertEqual(st["status"], "pending")
        self.assertEqual(st["requestId"], rid)


class AdapterEdgeTests(unittest.TestCase):
    """The adapter's own branches, driven without a dispatcher: the free-text
    decision both ways, expiry, malformed answers, the stale duplicate, and the
    answer-shape helper's fallbacks (the coverage gate's 12 lines on #3753)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ha = HumanActionAdapter(str(Path(self.tmp.name) / "ha"))

    def tearDown(self):
        self.tmp.cleanup()

    def _req(self, rid, **params):
        return {"requestId": rid, "params": params}

    def test_free_text_decision_round_trips_the_typed_answer(self):
        aid = self.ha.open_elicitation(self._req("r-ft", type="text", question="Your name?"))
        req = self.ha.manager.get(aid)
        self.assertEqual([(a.id, a.kind, a.label) for a in req.actions], [("answer", "free_text", "Answer")])
        self.assertIn("expires_at", req.to_wire())
        self.ha.resolve(aid, {"1": "Alice"}, "@owner:x")
        self.assertEqual(self.ha.poll_resolution(aid), ("resolved", {"1": "Alice"}, "@owner:x"))

    def test_an_expired_requirement_reports_expired_on_every_poll(self):
        r = self._req("r-exp", action="x"); r["expiresAt"] = 1.0
        aid = self.ha.open_approval(r)
        self.assertEqual(self.ha.poll_resolution(aid), ("expired", None, None))
        status, answers, _by = self.ha.poll_resolution(aid)
        self.assertEqual((status, answers), ("expired", None))

    def test_resolve_refuses_an_unknown_requirement_and_a_bad_index(self):
        from hitl.schema import MalformedActionError
        with self.assertRaises(MalformedActionError):
            self.ha.resolve("ha-nope", {"1": [1]}, "@owner:x")
        aid = self.ha.open_approval(self._req("r-idx", action="x"))
        with self.assertRaises(MalformedActionError):
            self.ha.resolve(aid, {"1": [9]}, "@owner:x")
        with self.assertRaises(MalformedActionError):
            self.ha.resolve(aid, {"1": "Maybe"}, "@owner:x")

    def test_a_duplicate_answer_is_swallowed_not_raised(self):
        aid = self.ha.open_approval(self._req("r-dup", action="x"))
        self.ha.resolve(aid, {"1": [1]}, "@owner:x")
        self.assertIsNone(self.ha.resolve(aid, {"1": [2]}, "@owner:x"))
        self.assertEqual(self.ha.poll_resolution(aid)[0], "resolved")
        self.assertEqual(self.ha.manager.get(aid).chosen_action, "approve")

    def test_answer_shape_fallbacks(self):
        aid = self.ha.open_approval(self._req("r-shape", action="x"))
        req = self.ha.manager.get(aid)
        req.chosen_action = "ghost"; req.answer = None
        self.assertEqual(self.ha._answers(req), {})
        req.chosen_action = "approve"; req.subject["options"] = []
        self.assertEqual(self.ha._answers(req), {"1": "Approve"})


class ProductionComposedSettleTests(unittest.TestCase):
    """The control that matters: the dispatcher the SHIPPED server composes,
    reached the way `RuntimeServer.client()` reaches it. A hand-built
    dispatcher could be given grants the real Unix transport never has."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        import server as rt  # noqa: PLC0415
        self.srv = rt.RuntimeServer(socket_path=str(base / "s.sock"),
                                    db_path=str(base / "rt.sqlite"),
                                    ha_dir=str(base / "ha"))
        self.d = self.srv.dispatcher

    def tearDown(self):
        self.srv.store.close()
        self.tmp.cleanup()

    def test_unix_transport_grants_nothing(self):
        self.assertEqual(set(self.d.granted_methods), set())

    def test_ungranted_unix_client_cannot_settle_its_own_request(self):
        rid = asyncio.run(self.d.handle(
            "human_action.request", {"action": "Sign the agreement"}))["requestId"]
        for method in SETTLE_METHODS:
            with self.assertRaises(ProtocolError) as cm:
                asyncio.run(self.d.handle(method, {"requestId": rid}))
            self.assertEqual(cm.exception.code, -32601)
        rec = self.srv.store.get(rid)
        self.assertEqual(rec["status"], "pending")
        self.assertIsNone(rec.get("resolvedBy"))
        self.assertEqual(self.srv.ha.manager.get(self.d._ha_of[rid]).status,
                         "pending")


if __name__ == "__main__":
    unittest.main(verbosity=2)
