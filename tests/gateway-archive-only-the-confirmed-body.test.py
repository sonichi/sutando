"""The gateway archives a live result only when the body the outbox confirmed
is that file's body. A different live body under an id that already holds a
delivered record, or a restored body under an id whose outbox still queues
the earlier one, is quarantined with the refusal cause, never archived."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "ag2-sparrow"))

from ag2_sparrow import outbox, remote_gateway_bridge as gw  # noqa: E402
from ag2_sparrow.delivery_core import DeliveryCore, DesignAClaimBackend, RetryPolicy  # noqa: E402
from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceResultProvider  # noqa: E402

ROOM = "!same:ag2.space"
TID = "task-holder"


class Gateway:
    def __init__(self):
        self.calls = []
        self.accepted = {}

    def request(self, method, path, payload):
        self.calls.append(dict(payload))
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


if __name__ == "__main__":
    unittest.main()
