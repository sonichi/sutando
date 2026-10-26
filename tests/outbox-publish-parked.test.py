#!/usr/bin/env python3
"""outbox.publish_parked: an item born PARKED in one locked write, never READY first.

A body whose completeness cannot be confirmed is held for an operator; a READY moment would let a
drain claim and send it, and a READY-then-park pair leaves that window open across a crash. Pinned
on the src copy and the vendored copy the gateway imports.

Run: python3 tests/outbox-publish-parked.test.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

import outbox as src_outbox  # noqa: E402
from ag2_sparrow import outbox as vendored_outbox  # noqa: E402

MODULES = (src_outbox, vendored_outbox)
PAYLOAD = '{"body": "held", "room_id": "!r:example"}'


class PublishParked(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def test_the_record_is_written_once_and_only_ever_parked(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                root, written = self.base / mod.__name__ / "a", []
                real = mod._write_item

                def watch(r, item_id, data):
                    written.append(data.get("status"))
                    return real(r, item_id, data)
                with mock.patch.object(mod, "_write_item", watch):
                    self.assertIs(mod.publish_parked(root, "p~1", PAYLOAD, "held", source_ready_sha256="ab"),
                                  mod.BornParked.CREATED)
                self.assertEqual(written, ["PARKED"], "never READY, one write")
                d = mod.read_item(root, "p~1")
                self.assertEqual((d["status"], d["payload"], d["reason"]), ("PARKED", PAYLOAD, "held"))
                self.assertEqual(mod.receipt(root, "p~1"), ("PARKED", 0))

    def test_a_retry_is_already_parked_and_anything_else_is_left_alone(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                root = self.base / mod.__name__ / "b"
                mod.publish_parked(root, "p~1", PAYLOAD, "held")
                self.assertIs(mod.publish_parked(root, "p~1", PAYLOAD, "held"), mod.BornParked.ALREADY_PARKED)
                self.assertIs(mod.publish_parked(root, "p~1", "other", "held"), mod.BornParked.EXISTS)
                mod._write_item(root, "p~2", {"item_id": "p~2", "status": "READY", "payload": PAYLOAD})
                self.assertIs(mod.publish_parked(root, "p~2", PAYLOAD, "held"), mod.BornParked.EXISTS)
                self.assertEqual(mod.receipt(root, "p~2")[0], "READY", "an existing item is never touched")

    def test_the_operator_requeue_is_its_only_way_out(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                root = self.base / mod.__name__ / "c"
                mod.publish_parked(root, "p~1", PAYLOAD, "held", source_ready_sha256="ab")
                self.assertEqual(mod.requeue_item_with_epoch(root, "p~1")[:2], (mod.RequeueOutcome.REQUEUED, 1))
                self.assertEqual(mod.receipt(root, "p~1"), ("QUEUED", 1))

    def test_racing_writers_converge_on_one_created_record(self):
        for mod in MODULES:
            with self.subTest(mod=mod.__name__):
                for round_ in range(20):
                    root = self.base / mod.__name__ / f"race{round_}"
                    gate, outcomes = threading.Barrier(8), []

                    def writer():
                        gate.wait()
                        outcomes.append(mod.publish_parked(root, "p~1", PAYLOAD, "held"))
                    threads = [threading.Thread(target=writer) for _ in range(8)]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join()
                    self.assertEqual(outcomes.count(mod.BornParked.CREATED), 1)
                    self.assertEqual(outcomes.count(mod.BornParked.ALREADY_PARKED), 7)


if __name__ == "__main__":
    unittest.main(verbosity=1)
