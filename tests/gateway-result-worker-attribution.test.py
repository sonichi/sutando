#!/usr/bin/env python3
"""A result POST carries which pool worker produced it, as structured
metadata: {"metadata": {"worker_id": "core-2"}} -> broker -> the Matrix
event's content["space.ag2.worker"].id (ag2space-backend#882).

The worker is read from the per-worker done-flag the pool already writes
(state/workers/<recipient>/done/task-<id>.flag — the ONE writer is
skills/worker-pool/scripts/pool_delivery.py::mark_done/done_flag), NOT from
the "- core-N" signature in the body: that line is for humans, and
reformatting it must not silently change routing or attribution.

The fixture writes through pool_delivery.done_flag() rather than
hand-spelling the path: packages/ag2-sparrow is a standalone PyPI package
and cannot import skills/worker-pool/ in production, so _worker_of()'s own
path literal in remote_gateway_bridge.py is the only place the convention is
re-stated — building the fixture from the real writer's path function is
what makes a future drift between the two show up as a failing test instead
of a silently-always-empty lookup (sonichi/sutando, 2026-09-16: _worker_of
globbed "cores" while mark_done wrote "workers", so every result shipped
with no worker_id and the six tests here never caught it, because the old
_flag() fixture reimplemented the SAME wrong "cores" path instead of calling
the real writer).

Run: python3 tests/gateway-result-worker-attribution.test.py
"""
from __future__ import annotations

import importlib.util
import os
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src" / "remote-gateway-bridge.py"
_POOL_SCRIPTS = _REPO / "skills" / "worker-pool" / "scripts"

sys.path.insert(0, str(_POOL_SCRIPTS))
import pool_delivery  # noqa: E402


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
        # self.workspace is the WORKSPACE root pool_delivery expects; _STATE
        # is production's own state_dir() == <workspace>/state.
        self.workspace = tempfile.mkdtemp()
        self.mod._STATE = Path(self.workspace) / "state"

        self.seen = {}

        class _Backend:
            def publish(_s, tid, payload):
                self.seen["payload"] = json.loads(payload.decode())
                raise _Captured()

        self.mod._delivery_core = lambda: type("C", (), {"backend": _Backend()})()

    def _flag(self, core: str, tid: str):
        # Named exactly as finish_task writes it: the full result stem, prefix
        # included. A bare-id fixture agrees with a prefix bug and hides it.
        path = pool_delivery.done_flag(Path(self.workspace), core, tid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    def _doc(self, tid: str) -> dict:
        self.seen.clear()
        with self.assertRaises(_Captured):
            self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!")
        return self.seen["payload"]

    def test_worker_rides_the_payload(self):
        self._flag("core-2", "task-0023dacce4b1f0a9c7")
        doc = self._doc("task-0023dacce4b1f0a9c7")
        self.assertEqual(doc["metadata"], {"worker_id": "core-2"})
        # Attribution must not leak into the text the user reads.
        self.assertEqual(doc["body"], "done!")

    def test_varying_the_worker_varies_the_payload(self):
        self._flag("core-1", "task-dev~task-07c59a1b2d3e4f5061")
        self._flag("core-3", "task-9f81c02de5a6b7c8d9")
        a, b = self._doc("task-dev~task-07c59a1b2d3e4f5061"), self._doc("task-9f81c02de5a6b7c8d9")
        self.assertEqual(a["metadata"]["worker_id"], "core-1")
        self.assertEqual(b["metadata"]["worker_id"], "core-3")
        self.assertNotEqual(a["metadata"], b["metadata"])

    def test_control_no_flag_sends_no_metadata(self):
        # Single-core installs write no per-core flag; absent must mean absent,
        # never a fabricated default that would misattribute every result.
        self.assertNotIn("metadata", self._doc("task-unflagged00000000"))

    def test_control_ambiguous_flags_send_no_metadata(self):
        self._flag("core-1", "task-4ambiguous000000a")
        self._flag("core-2", "task-4ambiguous000000a")
        self.assertNotIn("metadata", self._doc("task-4ambiguous000000a"))

    def test_control_a_bare_id_does_not_attribute(self):
        # Production never passes a bare id; if one ever reaches here it must
        # not resolve, or the prefix contract has silently changed shape.
        self._flag("core-2", "task-6bareid00000000000")
        self.assertEqual(self.mod._worker_of("6bareid00000000000"), "")

    def test_worker_of_survives_a_missing_state_tree(self):
        self.mod._STATE = Path(self.workspace) / "nonexistent"
        self.assertEqual(self.mod._worker_of("task-5missingstate0000"), "")

    # --- the writer's PENDING stage (keweichen, #4302) -------------------
    # mark_done(published=False) lays `.pending` BEFORE the handler publishes
    # the result and promotes to `.flag` only after it returns, so a ready
    # result is routinely delivered while only `.pending` exists.

    def test_pending_alone_still_attributes(self):
        tid = "task-pendingwindow00001"
        pend = pool_delivery.mark_done(Path(self.workspace), "worker-1", tid, published=False)
        self.assertTrue(str(pend).endswith(".pending"), pend)
        self.assertEqual(self.mod._worker_of(tid), "worker-1")

    def test_the_delivered_payload_is_attributed_during_the_pending_window(self):
        # The end-to-end shape of the defect: publish-then-promote, with the
        # POST built in between. Before the fix this payload had no metadata.
        tid = "task-pendingwindow00002"
        pool_delivery.mark_done(Path(self.workspace), "worker-2", tid, published=False)
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": "worker-2"})
        pool_delivery.mark_done(Path(self.workspace), "worker-2", tid, published=True)
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": "worker-2"})

    def test_promotion_does_not_double_count_its_own_worker(self):
        # Belt and braces: if a `.pending` ever outlived its `.flag`, one
        # worker holding both stages is still ONE claimant, not ambiguity.
        tid = "task-bothstages00000001"
        d = pool_delivery.done_flag(Path(self.workspace), "worker-3", tid).parent
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}.flag").write_text("")
        (d / f"{tid}.pending").write_text("")
        self.assertEqual(self.mod._worker_of(tid), "worker-3")

    # --- fail closed rather than stamp the wrong worker ------------------

    def test_a_directory_at_the_record_name_is_refused(self):
        # The writer's own predicate (pool_delivery.is_done_flag) accepts only
        # a regular file; anything else is malformed state, not a finish.
        tid = "task-dirrecord000000001"
        pool_delivery.done_flag(Path(self.workspace), "worker-4", tid).mkdir(parents=True)
        self.assertEqual(self.mod._worker_of(tid), "")

    def test_a_symlink_at_the_record_name_is_refused(self):
        tid = "task-symlinkrecord00001"
        real = pool_delivery.done_flag(Path(self.workspace), "worker-5", tid)
        real.parent.mkdir(parents=True, exist_ok=True)
        target = real.parent / "elsewhere"
        target.write_text("")
        real.symlink_to(target)
        self.assertEqual(self.mod._worker_of(tid), "")

    def test_an_unreadable_claim_tree_abstains_instead_of_naming_the_other(self):
        # The Path.glob trap: an unreadable subtree reads as "absent", which
        # would hand the answer to the only claimant it could still see.
        tid = "task-unreadable00000001"
        a = pool_delivery.done_flag(Path(self.workspace), "worker-a", tid)
        a.parent.mkdir(parents=True, exist_ok=True)
        a.write_text("")
        b = pool_delivery.done_flag(Path(self.workspace), "worker-b", tid)
        b.parent.mkdir(parents=True, exist_ok=True)
        b.write_text("")
        if os.geteuid() == 0:
            self.skipTest("root ignores the mode bits this case depends on")
        mode = b.parent.stat().st_mode
        os.chmod(b.parent, 0o000)
        try:
            self.assertEqual(self.mod._worker_of(tid), "")
        finally:
            os.chmod(b.parent, mode)


if __name__ == "__main__":
    unittest.main(verbosity=2)
