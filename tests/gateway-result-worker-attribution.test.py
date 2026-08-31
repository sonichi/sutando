#!/usr/bin/env python3
"""A result POST carries which pool worker produced it, as structured
metadata: {"metadata": {"worker_id": "core-2"}} -> broker -> the Matrix
event's content["space.ag2.worker"].id (ag2space-backend#882).

The worker is read from the per-core done-flag the pool already writes
(state/cores/<core>/done/task-<id>.flag), NOT from the "- core-N" signature
in the body: that line is for humans, and reformatting it must not silently
change routing or attribution.

Run: python3 tests/gateway-result-worker-attribution.test.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "remote-gateway-bridge.py"


def _load():
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
        self.mod = _load()
        self.tmp = tempfile.mkdtemp()
        self.mod._STATE = Path(self.tmp)

        self.seen = {}

        class _Backend:
            def publish(_s, tid, payload):
                self.seen["payload"] = json.loads(payload.decode())
                raise _Captured()

        self.mod._delivery_core = lambda: type("C", (), {"backend": _Backend()})()

    def _flag(self, core: str, tid: str):
        d = Path(self.tmp) / "cores" / core / "done"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"task-{tid}.flag").write_text("")

    def _doc(self, tid: str) -> dict:
        self.seen.clear()
        with self.assertRaises(_Captured):
            self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!")
        return self.seen["payload"]

    def test_worker_rides_the_payload(self):
        self._flag("core-2", "t1")
        doc = self._doc("t1")
        self.assertEqual(doc["metadata"], {"worker_id": "core-2"})
        # Attribution must not leak into the text the user reads.
        self.assertEqual(doc["body"], "done!")

    def test_varying_the_worker_varies_the_payload(self):
        self._flag("core-1", "t2")
        self._flag("core-3", "t3")
        a, b = self._doc("t2"), self._doc("t3")
        self.assertEqual(a["metadata"]["worker_id"], "core-1")
        self.assertEqual(b["metadata"]["worker_id"], "core-3")
        self.assertNotEqual(a["metadata"], b["metadata"])

    def test_control_no_flag_sends_no_metadata(self):
        # Single-core installs write no per-core flag; absent must mean absent,
        # never a fabricated default that would misattribute every result.
        self.assertNotIn("metadata", self._doc("t-unflagged"))

    def test_control_ambiguous_flags_send_no_metadata(self):
        self._flag("core-1", "t4")
        self._flag("core-2", "t4")
        self.assertNotIn("metadata", self._doc("t4"))

    def test_worker_of_survives_a_missing_state_tree(self):
        self.mod._STATE = Path(self.tmp) / "nonexistent"
        self.assertEqual(self.mod._worker_of("t5"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
