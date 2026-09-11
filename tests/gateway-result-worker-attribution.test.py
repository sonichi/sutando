#!/usr/bin/env python3
"""A result POST carries which worker produced it, as structured metadata:
{"metadata": {"worker_id": "worker-1"}} -> broker -> the Matrix event's
content["space.ag2.worker"].id (ag2space-backend#882) -> the client draws that
worker's name and colour from its roster.

The worker is read from the done-flag its finisher writes, NOT from the
"- core-N" signature in the body: that line is for humans, and reformatting it
must not silently change routing or attribution. Two layouts are read — the
pool's `state/workers/<id>/done/` (path owned by src/pool_delivery.done_flag)
and the pre-pool `state/cores/<id>/done/`.

The layout is the HOST's, so it reaches the packaged adapter by injection:
src/remote-gateway-bridge.py calls set_claimant_resolver(resolve_claimant).
This suite drives that shipped loader, so the seam is exercised, not mocked.

Attribution is fail-closed. Ambiguous or unreadable state stamps nothing: a
wrong worker id is drawn with the same confidence as a right one, so it is
strictly worse than an unattributed reply.

Run: python3 tests/gateway-result-worker-attribution.test.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src" / "remote-gateway-bridge.py"

# The flag path comes from the real writer, not a second spelling of it here:
# a fixture that re-spells the layout agrees with the bug it should catch.
sys.path.insert(0, str(_REPO / "src"))
import pool_delivery  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("_rgb_worker", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rgb_worker"] = mod
    spec.loader.exec_module(mod)
    return mod


@contextlib.contextmanager
def _unreadable(d: Path):
    mode = d.stat().st_mode
    d.chmod(0o000)
    try:
        yield
    finally:
        d.chmod(mode)


class _Captured(Exception):
    """Stops _deliver_result_payload right after the payload is built, so the
    assertion never depends on delivery-status enum semantics."""


class WorkerAttribution(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.tmp = Path(tempfile.mkdtemp())
        self.mod._STATE = self.tmp / "state"
        self.logs: list[str] = []
        self.mod._log = self.logs.append
        self.seen = {}

        class _Backend:
            def publish(_s, tid, payload):
                self.seen["payload"] = json.loads(payload.decode())
                raise _Captured()

        self.mod._delivery_core = lambda: type("C", (), {"backend": _Backend()})()

    def _worker_flag(self, wid: str, tid: str, published: bool = True) -> Path:
        # The production writer, not a fixture spelling of it: the reader and the
        # finisher must agree by construction, not by two matching guesses.
        return pool_delivery.mark_done(self.tmp, wid, tid, published=published)

    def _core_flag(self, core: str, tid: str) -> Path:
        # Named exactly as finish_task writes it: the full result stem, prefix
        # included. A bare-id fixture agrees with a prefix bug and hides it.
        d = self.tmp / "state" / "cores" / core / "done"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{tid}.flag"
        p.write_text("")
        return p

    def _doc(self, tid: str) -> dict:
        self.seen.clear()
        with self.assertRaises(_Captured):
            self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!")
        return self.seen["payload"]

    def _abstained(self) -> list[str]:
        return [ln for ln in self.logs if "not stamping a worker" in ln]

    def test_a_pool_workers_done_flag_stamps_that_worker(self):
        tid = "task-9c1f0a7b2e3d4c5a6b"
        flag = self._worker_flag("worker-1", tid)
        self.assertTrue(str(flag).endswith(f"state/workers/worker-1/done/{tid}.flag"),
                        f"writer moved: {flag}")
        doc = self._doc(tid)
        self.assertEqual(doc["metadata"], {"worker_id": "worker-1"})
        # Only the id — a label would freeze broker-owned intent into the event.
        self.assertEqual(list(doc["metadata"]), ["worker_id"])
        # Attribution must not leak into the text the user reads.
        self.assertEqual(doc["body"], "done!")

    def test_a_legacy_core_done_flag_still_stamps_that_core(self):
        tid = "task-0023dacce4b1f0a9c7"
        self._core_flag("core-2", tid)
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": "core-2"})

    def test_varying_the_worker_varies_the_payload(self):
        a_id, b_id = "task-dev~task-07c59a1b2d3e4f5061", "task-9f81c02de5a6b7c8d9"
        self._worker_flag("worker-1", a_id)
        self._core_flag("core-3", b_id)
        a, b = self._doc(a_id), self._doc(b_id)
        self.assertEqual(a["metadata"]["worker_id"], "worker-1")
        self.assertEqual(b["metadata"]["worker_id"], "core-3")
        self.assertNotEqual(a["metadata"], b["metadata"])

    def test_control_no_flag_sends_no_metadata(self):
        # Single-worker installs write no flag; absent must mean absent, never
        # a fabricated default that would misattribute every result.
        self.assertNotIn("metadata", self._doc("task-unflagged00000000"))
        self.assertEqual(self.mod._worker_of("task-unflagged00000000"), "")
        self.assertEqual(self._abstained(), [])

    def test_control_a_flag_under_both_layouts_is_ambiguous_and_logs(self):
        tid = "task-4ambiguous000000a"
        self._worker_flag("worker-1", tid)
        self._core_flag("core-2", tid)
        self.assertNotIn("metadata", self._doc(tid))
        hit = self._abstained()
        self.assertEqual(len(hit), 1, f"expected one abstention log, got {self.logs}")
        self.assertIn("ambiguous done flags", hit[0])
        self.assertIn("worker-1", hit[0])
        self.assertIn("core-2", hit[0])

    def test_control_a_directory_at_the_flag_path_is_not_a_finish(self):
        # A non-regular object at the flag's own name is malformed state. The
        # reader used to stat it and take any hit, emitting a confident id.
        tid = "task-1notafile000000f"
        d = pool_delivery.done_flag(self.tmp, "worker-1", tid)
        d.mkdir(parents=True)
        self.assertFalse(pool_delivery.is_done_flag(d), "the pool contract must reject it")
        self.assertNotIn("metadata", self._doc(tid))
        hit = self._abstained()
        self.assertEqual(len(hit), 1, f"expected one abstention log, got {self.logs}")
        self.assertIn("not a regular file", hit[0])

    def test_a_pool_workers_pending_record_stamps_that_worker(self):
        # The stage a worker is in while its handler publishes: the drain can
        # meet the result before the promote, and must already have the name.
        tid = "task-4pendingstage000e"
        self._worker_flag("worker-1", tid, published=False)
        self.assertEqual(self._doc(tid)["metadata"]["worker_id"], "worker-1")

    def test_control_a_malformed_pool_flag_does_not_let_the_legacy_core_win(self):
        # Mixed layout: dropping the malformed one on the floor would leave the
        # `cores` flag as the single hit, and stamp core-2 with full confidence.
        tid = "task-0mixedlayout0001a"
        pool_delivery.done_flag(self.tmp, "worker-1", tid).mkdir(parents=True)
        self._core_flag("core-2", tid)
        doc = self._doc(tid)
        self.assertNotIn("metadata", doc)
        self.assertEqual(self.mod._worker_of(tid), "")
        self.assertIn("not a regular file", self._abstained()[0])

    def test_control_two_pool_workers_claiming_one_task_is_ambiguous(self):
        tid = "task-7twoworkers00000b"
        self._worker_flag("worker-1", tid)
        self._worker_flag("worker-2", tid)
        self.assertEqual(self.mod._worker_of(tid), "")

    @unittest.skipIf(os.geteuid() == 0, "root reads a 0o000 directory anyway")
    def test_control_an_unreadable_claim_tree_abstains_rather_than_naming_the_other(self):
        # Path.glob swallows PermissionError, so a hidden claimant used to read
        # as an absence and the OTHER tree's flag was stamped with confidence.
        tid = "task-3unreadable0000c"
        self._worker_flag("worker-1", tid)
        self._core_flag("core-2", tid)
        with _unreadable(self.tmp / "state" / "workers"):
            doc = self._doc(tid)
            hit = self._abstained()
        self.assertNotIn("metadata", doc)
        self.assertEqual(len(hit), 1, f"expected one abstention log, got {self.logs}")
        self.assertIn("claim tree unreadable", hit[0])

    def test_control_a_bare_id_does_not_attribute(self):
        # Production never passes a bare id; if one ever reaches here it must
        # not resolve, or the prefix contract has silently changed shape.
        self._worker_flag("worker-1", "task-6bareid00000000000")
        self.assertEqual(self.mod._worker_of("6bareid00000000000"), "")

    def test_worker_of_survives_a_missing_state_tree(self):
        self.mod._STATE = self.tmp / "nonexistent"
        self.assertEqual(self.mod._worker_of("task-5missingstate0000"), "")

    def test_the_package_attributes_nothing_until_a_host_injects_the_layout(self):
        # ag2-sparrow ships standalone: with no resolver injected it must know
        # no state layout at all, rather than guess one.
        self._worker_flag("worker-1", "task-8noresolver00000d")
        self.mod._CLAIMANT_RESOLVER = None
        self.assertNotIn("metadata", self._doc("task-8noresolver00000d"))

    def test_a_raising_resolver_abstains_and_logs(self):
        self.mod._CLAIMANT_RESOLVER = lambda *_a: (_ for _ in ()).throw(
            RuntimeError("resolver exploded"))
        self.assertNotIn("metadata", self._doc("task-2raiser00000000e"))
        hit = self._abstained()
        self.assertEqual(len(hit), 1, f"expected one abstention log, got {self.logs}")
        self.assertIn("resolver exploded", hit[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
