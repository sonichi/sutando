#!/usr/bin/env python3
"""A result POST carries which pool worker produced it, as structured
metadata: {"metadata": {"worker_id": "core-2"}} -> broker -> the Matrix
event's content["space.ag2.worker"].id (ag2space-backend#882).

The worker is read from the per-core done-flag the pool already writes
(state/cores/<core>/done/task-<id>.flag), NOT from the "- core-N" signature
in the body: that line is for humans, and reformatting it must not silently
change routing or attribution. A seat with no done-flag (a cloud seat, or the
home seat outside a pool) attributes to its OWN worker id — the seat that
POSTs the result answered it — so no result ever leaves unattributed.

Run: python3 tests/gateway-result-worker-attribution.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "remote-gateway-bridge.py"
_SEAT_ENV = ("SUTANDO_WORKER_ID", "SUTANDO_WORKER_SEAT", "SUTANDO_CORE_ID",
             "SUTANDO_WORKER_LOCATION")


def _load(**seat: str):
    # The seat identity is read at import; a fresh env per load keeps the
    # cases independent of each other and of the host's own seat.
    for k in _SEAT_ENV:
        os.environ.pop(k, None)
    os.environ.update(seat)
    spec = importlib.util.spec_from_file_location("_rgb_worker", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rgb_worker"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Captured(Exception):
    """Stops _deliver_result_payload right after the payload is built, so the
    assertion never depends on delivery-status enum semantics."""


class WorkerAttribution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.seen = {}
        self._seat()

    def _seat(self, **seat: str):
        self.mod = _load(**seat)
        self.mod._STATE = Path(self.tmp)
        seen = self.seen

        class _Backend:
            def publish(_s, tid, payload):
                seen["payload"] = json.loads(payload.decode())
                raise _Captured()

        self.mod._delivery_core = lambda: type("C", (), {"backend": _Backend()})()

    def _flag(self, core: str, tid: str):
        # Named exactly as finish_task writes it: the full result stem, prefix
        # included. A bare-id fixture agrees with a prefix bug and hides it.
        d = Path(self.tmp) / "cores" / core / "done"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}.flag").write_text("")

    def _doc(self, tid: str) -> dict:
        self.seen.clear()
        with self.assertRaises(_Captured):
            self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!")
        return self.seen["payload"]

    def test_worker_rides_the_payload(self):
        self._flag("core-2", "task-0023dacce4b1f0a9c7")
        doc = self._doc("task-0023dacce4b1f0a9c7")
        self.assertEqual(doc["metadata"], {"worker_id": "core-2", "location": "local"})
        # Attribution must not leak into the text the user reads.
        self.assertEqual(doc["body"], "done!")

    def test_varying_the_worker_varies_the_payload(self):
        self._flag("core-1", "task-dev~task-07c59a1b2d3e4f5061")
        self._flag("core-3", "task-9f81c02de5a6b7c8d9")
        a, b = self._doc("task-dev~task-07c59a1b2d3e4f5061"), self._doc("task-9f81c02de5a6b7c8d9")
        self.assertEqual(a["metadata"]["worker_id"], "core-1")
        self.assertEqual(b["metadata"]["worker_id"], "core-3")
        self.assertNotEqual(a["metadata"], b["metadata"])

    def test_no_flag_attributes_to_this_seat(self):
        # Single-seat installs write no per-core flag; the seat that POSTs the
        # result answered it, so its own id is the attribution of last resort.
        self.assertEqual(self._doc("task-unflagged00000000")["metadata"],
                         {"worker_id": "home", "location": "local"})

    def test_cloud_seat_attributes_to_itself(self):
        # Measured defect: a cloud seat has no done-flag, and its results left
        # with no metadata at all (worker_attribution_missing = 2 of 2).
        self._seat(SUTANDO_WORKER_ID="cloud-1", SUTANDO_WORKER_LOCATION="cloud")
        self.assertEqual((self.mod.WORKER_ID, self.mod.WORKER_LOCATION),
                         ("cloud-1", "cloud"))
        doc = self._doc("task-cloudseat0000000")
        self.assertEqual(doc["metadata"], {"worker_id": "cloud-1", "location": "cloud"})
        self.assertEqual(doc["body"], "done!")

    def test_done_flag_outranks_the_seat(self):
        # A pool worker's done-flag is the answering seat even when the lead
        # (worker-1) is the one POSTing: pool attribution is unchanged.
        self._seat(SUTANDO_WORKER_ID="worker-1")
        self._flag("worker-2", "task-flagwins000000000")
        self.assertEqual(self._doc("task-flagwins000000000")["metadata"]["worker_id"],
                         "worker-2")
        self.assertEqual(self._doc("task-noflag0000000000")["metadata"]["worker_id"],
                         "worker-1")

    def test_control_ambiguous_flags_fall_back_to_this_seat(self):
        # Two flags name no single worker; the POSTing seat is the only fact.
        self._seat(SUTANDO_WORKER_ID="worker-1")
        self._flag("core-1", "task-4ambiguous000000a")
        self._flag("core-2", "task-4ambiguous000000a")
        self.assertEqual(self._doc("task-4ambiguous000000a")["metadata"]["worker_id"],
                         "worker-1")

    def test_control_a_bare_id_does_not_attribute(self):
        # Production never passes a bare id; if one ever reaches here it must
        # not resolve, or the prefix contract has silently changed shape.
        self._flag("core-2", "task-6bareid00000000000")
        self.assertEqual(self.mod._worker_of("6bareid00000000000"), "")

    def test_worker_of_survives_a_missing_state_tree(self):
        self.mod._STATE = Path(self.tmp) / "nonexistent"
        self.assertEqual(self.mod._worker_of("task-5missingstate0000"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
