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


class HumanActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.store = RequestStore(str(base / "rt.sqlite"))
        self.ha = HumanActionAdapter(str(base / "ha"))
        self.d = RuntimeDispatcher(self.store, self.ha, "@agent:x", executors={})

    def tearDown(self):
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
        card = self.ha.store.get(self.d._ha_of[rid])
        q = card["questions"][0]
        self.assertIn("Sign the agreement", q["question"])
        self.assertIn("Review section 4", q["question"])
        self.assertEqual([o["label"] for o in q["options"]], ["Done", "Decline"])

    def test_card_done_completes_and_decline_declines(self):
        for answer, expected in ((1, "completed"), (2, "declined")):
            rid = self._request()
            aid = self.d._ha_of[rid]
            self.ha.store.resolve(aid, {"1": [answer]}, "@owner:x")
            self.d._settle(rid)
            rec = self.store.get(rid)
            self.assertEqual(rec["status"], expected)
            self.assertEqual(rec["resolvedBy"], "@owner:x")

    def test_api_complete_resolves_request_and_closes_card(self):
        rid = self._request()
        out = asyncio.run(self.d.handle("human_action.complete",
                                        {"requestId": rid, "note": "signed"}))
        self.assertEqual(out["status"], "completed")
        self.assertEqual(self.store.get(rid)["result"], {"note": "signed"})
        card = self.ha.store.get(self.d._ha_of[rid])
        self.assertEqual(card["status"], "resolved")  # no dangling card

    def test_api_decline_and_terminal_is_idempotent_safe(self):
        rid = self._request()
        out = asyncio.run(self.d.handle("human_action.decline", {"requestId": rid}))
        self.assertEqual(out["status"], "declined")
        again = asyncio.run(self.d.handle("human_action.complete", {"requestId": rid}))
        self.assertTrue(again["alreadyTerminal"])
        self.assertEqual(again["status"], "declined")  # terminal state unchanged

    def test_complete_rejects_wrong_type_and_unknown(self):
        r = asyncio.run(self.d.handle("approval.request", {"action": "x"}))
        with self.assertRaises(ProtocolError):
            asyncio.run(self.d.handle("human_action.complete",
                                      {"requestId": r["requestId"]}))
        with self.assertRaises(ProtocolError):
            asyncio.run(self.d.handle("human_action.complete",
                                      {"requestId": "nope"}))

    def test_status_returns_request_state(self):
        rid = self._request()
        st = asyncio.run(self.d.handle("human_action.status", {"requestId": rid}))
        self.assertEqual(st["status"], "pending")
        self.assertEqual(st["requestId"], rid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
