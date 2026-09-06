#!/usr/bin/env python3
"""Characterization of the gateway's CURRENT outbound drain — written
BEFORE Phase 2 routes it through DeliveryCore, so "production behavior
unchanged" (acceptance criterion 4) is a measurement rather than a claim.

These tests assert what the shipped code does today, including behavior
nobody would design on purpose. If Phase 2 changes any of it, that is a
decision to make deliberately, not a diff to discover afterwards.

Run: python3 tests/gateway-outbound-characterization.test.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from ag2_sparrow import remote_gateway_bridge as gw  # noqa: E402

TID = "task-0000000000000000ch"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gw-char-")
        root = Path(self.tmp.name)
        self._saved = {n: getattr(gw, n) for n in (
            "TASKS_DIR", "RESULTS_DIR", "ARCHIVE_RESULTS_DIR",
            "UNDELIVERABLE_RESULTS_DIR", "_STATE", "_WITHHELD_DM_CACHE",
            "_WITHHELD_CONTROL_DIR", "_GATEWAY_OWNER_DM_HINT", "_req",
            "_WITHHELD_TASK_OUTPUT", "_delivery_tid", "_log", "_broker_tid",
            "_reenroll_identity")}
        gw.TASKS_DIR = root / "tasks"
        gw.RESULTS_DIR = root / "results"
        gw.ARCHIVE_RESULTS_DIR = gw.RESULTS_DIR / "archive"
        gw.UNDELIVERABLE_RESULTS_DIR = gw.RESULTS_DIR / "undelivered"
        gw._STATE = root / "state"
        gw._WITHHELD_DM_CACHE = gw._STATE / "withheld-review-dm.json"
        gw._WITHHELD_CONTROL_DIR = gw._STATE / "withheld-review-control-results"
        gw._GATEWAY_OWNER_DM_HINT = ""
        gw._WITHHELD_TASK_OUTPUT = {}
        for d in (gw.TASKS_DIR, gw.RESULTS_DIR):
            d.mkdir(parents=True)
        self.posted = []
        self.room_posts = []

        def request(method, path, payload=None, **_kwargs):
            if (method, path) == ("GET", "/v1/agents"):
                return {"agents": [{
                    "id": "agent-test",
                    "owner": "@owner:example.test",
                    "owner_dm_room": "!owner:example.test",
                }]}
            if (method, path) == ("POST", "/v1/room"):
                self.room_posts.append(payload)
                return {"ok": True, "event_id": "$owner-review"}
            self.posted.append((method, path, payload))
            return {"ok": True}

        gw._req = request
        gw._reenroll_identity = lambda: "agent-test"
        gw._delivery_tid = lambda tid: tid
        gw._broker_tid = lambda tid: tid
        gw._log = lambda m: None

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(gw, n, v)
        self.tmp.cleanup()

    def _result(self, body: str, tier: str = "owner") -> Path:
        # The drain resolves tier from the TASK file, failing closed to guest —
        # so owner provenance must be explicit in the fixture, never an accident.
        (gw.TASKS_DIR / f"{TID}.txt").write_text(
            f"id: {TID}\ntask: characterization fixture\naccess_tier: {tier}\n")
        f = gw.RESULTS_DIR / f"{TID}.txt"
        f.write_text(body)
        return f


class DrainShape(_Base):
    """What the drain does with an in-flight result today."""

    def test_plain_result_posts_body_verbatim_and_archives(self):
        self._result("the reply")
        inflight = {TID}
        gw._post_ready_results(inflight)
        self.assertEqual(len(self.posted), 1)
        method, path, payload = self.posted[0]
        self.assertEqual((method, path), ("POST", "/v1/results"))
        body = payload["body"]
        # The readiness boundary stamps an ID before the drain sees the body; the
        # drain still adds nothing of its own — stamp, blank line, content, end.
        self.assertRegex(body, r"^\[task \d{8}-\d{3}\]\n\n",
                         "the delivery boundary stamps an id ahead of the body")
        self.assertEqual(body.split("\n\n", 1)[1], "the reply",
                         "and past that stamp the body is verbatim — no wrapper, no label")
        self.assertNotIn(TID, inflight, "delivered tid leaves the ledger")
        self.assertFalse((gw.RESULTS_DIR / f"{TID}.txt").exists(),
                         "the result file is consumed, not left behind")

    def test_result_not_in_ledger_is_never_read(self):
        """The property that produced the orphan class: the drain iterates
        the LEDGER, so a result whose tid is not tracked is invisible to
        it — no scan of results/ happens here."""
        self._result("the reply")
        gw._post_ready_results(set())
        self.assertEqual(self.posted, [])
        self.assertTrue((gw.RESULTS_DIR / f"{TID}.txt").exists())

    def test_absent_result_leaves_the_tid_in_flight(self):
        inflight = {TID}
        gw._post_ready_results(inflight)
        self.assertEqual(self.posted, [])
        self.assertIn(TID, inflight, "a missing result is retried, not dropped")

    def test_unsafe_tid_is_discarded_without_reading_a_path(self):
        inflight = {"../../etc/passwd"}
        gw._post_ready_results(inflight)
        self.assertEqual(self.posted, [])
        self.assertEqual(inflight, set(), "unsafe tid is dropped from the ledger")


class MarkerHandling(_Base):
    """Marker semantics the core must preserve when it owns delivery."""

    def test_skip_marker_body_still_reaches_the_server(self):
        """[no-send] is posted VERBATIM: the server suppresses user-facing
        delivery and still closes the lease. The core must not 'optimize'
        this into a local-only completion."""
        self._result("[no-send]\ninternal only")
        gw._post_ready_results({TID})
        self.assertEqual(len(self.posted), 1, "the lease still closes via POST")
        self.assertIn("[no-send]", self.posted[0][2]["body"])

    def test_redirect_marker_is_posted_intact(self):
        self._result("[channel: C123ABC]\nthe reply")
        gw._post_ready_results({TID})
        self.assertIn("[channel: C123ABC]", self.posted[0][2]["body"])


    def test_missing_task_provenance_withholds_markers(self):
        """Absence is not owner provenance: with no task file the guard
        resolves guest, routes private owner review, and closes the lease
        without honouring the marker in the shared room."""
        f = gw.RESULTS_DIR / f"{TID}.txt"
        f.write_text("[channel: C123ABC]\nthe reply")
        gw._post_ready_results({TID})
        self.assertEqual(len(self.posted), 1)
        self.assertNotIn("[channel: C123ABC]", self.posted[0][2]["body"])
        self.assertEqual(self.posted[0][2]["body"], "[no-send]")
        self.assertTrue(any("Private result review" in post["body"]
                            for post in self.room_posts))

if __name__ == "__main__":
    unittest.main(verbosity=2)
