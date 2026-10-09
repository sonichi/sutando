"""The gateway archives a live result only when the body the outbox confirmed
is that file's body. A different live body under an id that already holds a
delivered or parked record, or a restored body under an id whose outbox still
queues the earlier one, is quarantined with the refusal cause, never archived
and never sent."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "ag2-sparrow"))

from ag2_sparrow import outbox, remote_gateway_bridge as gw
from ag2_sparrow.delivery_core import DeliveryCore, DesignAClaimBackend, RetryPolicy
from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceResultProvider

ROOM = "!same:ag2.space"
TID = "task-holder"


class Gateway:
    def __init__(self):
        self.calls = []
        self.accepted = {}
        self.refuse = False

    def request(self, method, path, payload):
        self.calls.append(dict(payload))
        if self.refuse:
            return {"ok": False, "error": "refused"}
        duplicate = payload["id"] in self.accepted
        if not duplicate:
            self.accepted[payload["id"]] = dict(payload)
        return {"ok": True, "duplicate": duplicate}


class ArchiveOnlyTheConfirmedBody(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server = Gateway()
        observer = patch.object(outbox, "_activity_completed", return_value=None)
        observer.start()
        self.addCleanup(observer.stop)
        self.results = self.root / "results"
        self.tasks = self.root / "tasks"
        self.results.mkdir()
        self.tasks.mkdir()
        self.outbox = self.results / ".outbox"
        self.core = DeliveryCore(
            DesignAClaimBackend(self.outbox, retry_schedule=outbox.RetrySchedule(),
                                republish_delivered=False),
            AG2SpaceResultProvider(self.server.request),
            RetryPolicy(max_attempts=5, defer_idempotent_resend=True))
        values = dict(RESULTS_DIR=self.results, ARCHIVE_RESULTS_DIR=self.results / "archive",
                      UNDELIVERABLE_RESULTS_DIR=self.results / "undelivered", TASKS_DIR=self.tasks,
                      _STATE=self.root / "state", DEDUP_ALIAS_FILE=self.root / "state" / "aliases.json",
                      TASK_ROOMS_FILE=self.root / "state" / "rooms.json",
                      TASK_MEDIA_FILE=self.root / "state" / "media.json",
                      INFLIGHT_FILE=self.root / "state" / "inflight.json",
                      GATEWAY_INSTANCE="", _INST_SUFFIX="", _DELIVERY_CORE=self.core,
                      _req=self.server.request)
        stack = __import__("contextlib").ExitStack()
        for name, value in values.items():
            stack.enter_context(patch.object(gw, name, value))
        self.addCleanup(stack.close)
        (self.tasks / f"{TID}.txt").write_text(
            f"id: {TID}\nsource: ag2space\nchannel_id: {ROOM}\nuser_id: owner\n"
            "access_tier: owner\ntask: Same question\n")
        gw._record_task_room(TID, ROOM)

    def _quarantined(self):
        return sorted(p.read_text() for p in (self.results / "undelivered").glob(f"{TID}*"))

    def _archived(self):
        return sorted(p.read_text() for p in (self.results / "archive").glob(f"{TID}*"))

    def test_an_old_delivered_record_does_not_archive_a_different_live_body(self):
        """The outbox already delivered OLDER under this id; a different live
        result arrives. publish() refuses it (no republish of a delivered id),
        deliver_one reports the old record TERMINAL: the file must not be archived."""
        self.core.backend.publish(TID, b"OLDER")
        outbox.record_delivered(self.outbox, TID, provider="p", destination="d")
        (self.results / f"{TID}.txt").write_text("LIVE-REFUSED")
        gw._post_ready_results({TID})
        self.assertEqual(self._archived(), [], "a body the broker never saw was archived")
        self.assertEqual(self._quarantined(), ["LIVE-REFUSED"])
        self.assertEqual(len(self.server.calls), 0, "nothing was POSTed for the refused body")
        self.assertFalse((self.results / f"{TID}.txt").exists())

    def test_a_queued_earlier_body_is_what_gets_sent_and_the_live_one_stays_visible(self):
        """The outbox queues A (an earlier publish never sent); quarantine
        restores B to the live name. deliver_one sends the stored A: B must be
        quarantined with the cause, not archived as delivered."""
        self.core.backend.publish(TID, json.dumps({"id": TID, "body": "A"}).encode())
        (self.results / f"{TID}.txt").write_text("B")
        gw._post_ready_results({TID})
        self.assertEqual([c.get("body") for c in self.server.calls][-1:], ["A"],
                         "the outbox sent its stored body")
        self.assertEqual(outbox.read_item(self.outbox, TID).get("status"), "DELIVERED")
        self.assertEqual(self._archived(), [], "B was never sent, so it is not archived")
        self.assertEqual(self._quarantined(), ["B"])

    def test_the_confirmed_body_is_archived_as_before(self):
        (self.results / f"{TID}.txt").write_text("Fresh answer")
        gw._post_ready_results({TID})
        self.assertEqual(self._quarantined(), [])
        self.assertEqual(len(self._archived()), 1)
        self.assertEqual(self.server.calls[-1].get("body"), "Fresh answer")

    def test_the_stored_text_is_the_proof(self):
        self.assertTrue(gw._record_holds_payload({"payload": "same"}, b"same"))
        self.assertFalse(gw._record_holds_payload({"payload": "same"}, b"other"))
        self.assertFalse(gw._record_holds_payload({"payload": None}, b"same"))
        self.assertFalse(gw._record_holds_payload({}, b"same"))

    def test_a_refused_body_without_a_result_file_is_logged_not_retried(self):
        """The direct-call path has no file to quarantine: both refusals log
        the cause and return False so nothing is archived."""
        lines = []
        with patch.object(gw, "_log", lines.append):
            self.core.backend.publish(TID, b"OLDER")
            outbox.record_delivered(self.outbox, TID, provider="p", destination="d")
            self.assertFalse(gw._deliver_result_payload(TID, TID, "LIVE-REFUSED"))
            self.assertIn("a different body was delivered under this id", lines[-1])
            other = f"{TID}-queued"
            self.core.backend.publish(other, json.dumps({"id": other, "body": "A"}).encode())
            self.assertFalse(gw._deliver_result_payload(other, other, "B"))
            self.assertIn("the outbox sent its stored body, not this one", lines[-1])
        self.assertEqual([c.get("body") for c in self.server.calls], ["A"])
        self.assertEqual(self._archived(), [])

    def _park(self, body: bytes, reason: str = "permanent-refusal", **extra):
        self.core.backend.publish(TID, body)
        outbox.park_item(self.outbox, TID, reason=reason)
        if extra:
            rec = outbox.read_item(self.outbox, TID)
            rec.update(extra)
            outbox._write_item(self.outbox, TID, rec)

    def _post_with_log(self):
        lines = []
        with patch.object(gw, "_log", lines.append):
            gw._post_ready_results({TID})
        return [ln for ln in lines if TID in ln]

    def test_a_parked_id_quarantines_a_later_different_body_naming_the_cause(self):
        self._park(b"PARKED")
        (self.results / f"{TID}.txt").write_text("LATER")
        lines = self._post_with_log()
        named = [ln for ln in lines if "a later, different result for a parked outbox id is refused" in ln]
        self.assertEqual(len(named), 1, lines)
        self.assertIn("permanent-refusal", named[0])
        self.assertIn("quarantined to undelivered/", named[0])
        self.assertIn("requeue", named[0])
        self.assertEqual(self._quarantined(), ["LATER"])
        self.assertEqual(self.server.calls, [], "a refused body is never sent")
        self.assertEqual(outbox.read_item(self.outbox, TID)["status"], "PARKED")

    def test_the_parked_body_itself_is_refused_without_a_resend(self):
        self.server.refuse = True
        (self.results / f"{TID}.txt").write_text("PARKED")
        gw._post_ready_results({TID})
        self.assertEqual(outbox.read_item(self.outbox, TID)["status"], "PARKED")
        self.assertEqual(len(self.server.calls), 1)
        self.server.calls.clear()
        for f in (self.results / "undelivered").glob(f"{TID}*"):
            f.unlink()
        (self.results / f"{TID}.txt").write_text("PARKED")
        lines = self._post_with_log()
        self.assertTrue(any("outbox item is terminal: permanent-refusal" in ln for ln in lines), lines)
        self.assertFalse(any("different result" in ln for ln in lines), lines)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self._quarantined(), ["PARKED"])

    def test_no_attempt_counters_or_park_reason_reopen_a_parked_id(self):
        """Whatever an older engine left on the record — clean counters, an
        ambiguous or a definite park — a later body is refused, never sent."""
        shapes = [dict(reason="permanent-refusal", attempts_started=0, attempts_classified=0),
                  dict(reason="permanent-refusal", attempts_started=1, attempts_classified=1),
                  dict(reason="outcome-unknown"), dict(reason="retry-window-exhausted", attempts=5)]
        for shape in shapes:
            with self.subTest(**{k: str(v) for k, v in shape.items()}):
                for f in list(self.outbox.rglob("*")) + list((self.results / "undelivered").glob("*")):
                    if f.is_file():
                        f.unlink()
                self.server.calls.clear()
                reason = shape.pop("reason")
                self._park(b"A", reason=reason, **shape)
                (self.results / f"{TID}.txt").write_text("B")
                gw._post_ready_results({TID})
                self.assertEqual(self.server.calls, [])
                self.assertFalse(self.core.backend.publish(TID, b"B"))
                self.assertEqual(outbox.read_item(self.outbox, TID)["status"], "PARKED")
                self.assertEqual(self._quarantined(), ["B"])


if __name__ == "__main__":
    unittest.main()
