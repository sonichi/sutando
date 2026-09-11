#!/usr/bin/env python3
"""The result drain stamps the POOL worker that finished the task.

Chain: `_deliver_result_payload` puts {"metadata": {"worker_id": ...}} on the
result POST -> the relay copies it into the event's `space.ag2.worker` content
-> the client draws that worker's name/colour from its roster. A pool worker
writes its done-flag at state/workers/<wid>/done/<tid>.flag (the writer is
src/pool_delivery.done_flag); the pre-pool per-core layout was
state/cores/<core>/done/. Reading only the latter leaves every pool worker's
reply unstamped, so the client cannot say who answered.

Only the worker ID is stamped, never a label: a label is broker-owned intent
and would go stale inside a frozen event.

Run: /usr/bin/python3 packages/ag2-sparrow/tests/test_result_stamp_worker.py
"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import sys
import tempfile
import unittest

_PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
_REPO_ROOT = _PKG_ROOT.parents[1]

# The flag path comes from the real writer, not a second spelling of it here:
# a fixture that re-spells the layout agrees with the bug it should catch.
sys.path.insert(0, str(_REPO_ROOT / "src"))
import pool_delivery  # noqa: E402


def _load(base: pathlib.Path):
    os.environ["AGENT_CONNECT_TASK_DIR"] = str(base / "tasks")
    os.environ["AGENT_CONNECT_RESULT_DIR"] = str(base / "results")
    os.environ["AGENT_CONNECT_STATE_DIR"] = str(base / "state")
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
    os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")
    sys.path.insert(0, str(_PKG_ROOT))
    mod = importlib.import_module("ag2_sparrow.remote_gateway_bridge")
    return importlib.reload(mod)


class _Captured(Exception):
    """Stops the delivery right after the payload is built, so the assertion
    never depends on delivery-status enum semantics."""


class ResultStampWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.mod = _load(self.tmp)
        self.mod._STATE = self.tmp / "state"
        self.logs: list[str] = []
        self.mod._log = self.logs.append
        self.seen: dict = {}

        class _Backend:
            def publish(_s, tid, payload):
                self.seen["payload"] = json.loads(payload.decode())
                raise _Captured()

        self.mod._delivery_core = lambda: type("C", (), {"backend": _Backend()})()

    def _worker_flag(self, wid: str, tid: str):
        # `self.tmp` is the workspace; done_flag appends state/workers/<wid>/done.
        p = pool_delivery.done_flag(self.tmp, wid, tid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
        return p

    def _core_flag(self, core: str, tid: str):
        d = self.tmp / "state" / "cores" / core / "done"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}.flag").write_text("")

    def _doc(self, tid: str) -> dict:
        self.seen.clear()
        with self.assertRaises(_Captured):
            self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!")
        return self.seen["payload"]

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
        tid = "task-1a2b3c4d5e6f708192"
        self._core_flag("core-2", tid)
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": "core-2"})

    def test_no_flag_anywhere_sends_no_metadata(self):
        # Absent must mean absent, never a fabricated default that would
        # misattribute every result on a single-core install.
        self.assertNotIn("metadata", self._doc("task-unflagged0000000"))
        self.assertEqual(self.mod._worker_of("task-unflagged0000000"), "")

    def test_a_flag_under_both_layouts_is_ambiguous_and_logs(self):
        tid = "task-4ambiguous00000a"
        self._worker_flag("worker-1", tid)
        self._core_flag("core-2", tid)
        self.assertNotIn("metadata", self._doc(tid))
        hit = [ln for ln in self.logs if "ambiguous done flags" in ln]
        self.assertEqual(len(hit), 1, f"expected one ambiguity log, got {self.logs}")
        self.assertIn("worker-1", hit[0])
        self.assertIn("core-2", hit[0])

    def test_two_pool_workers_claiming_one_task_is_ambiguous(self):
        tid = "task-7twoworkers00000b"
        self._worker_flag("worker-1", tid)
        self._worker_flag("worker-2", tid)
        self.assertEqual(self.mod._worker_of(tid), "")

    def test_varying_the_worker_varies_the_stamp(self):
        a_id, b_id = "task-aa11bb22cc33dd44ee", "task-bb22cc33dd44ee55ff"
        self._worker_flag("worker-1", a_id)
        self._worker_flag("worker-3", b_id)
        a, b = self._doc(a_id), self._doc(b_id)
        self.assertEqual(a["metadata"]["worker_id"], "worker-1")
        self.assertEqual(b["metadata"]["worker_id"], "worker-3")

    def test_a_bare_id_does_not_attribute(self):
        # Production never passes a bare id; if one ever reaches here it must
        # not resolve, or the prefix contract has silently changed shape.
        self._worker_flag("worker-1", "task-6bareid0000000000")
        self.assertEqual(self.mod._worker_of("6bareid0000000000"), "")

    def test_worker_of_survives_a_missing_state_tree(self):
        self.mod._STATE = self.tmp / "nonexistent"
        self.assertEqual(self.mod._worker_of("task-5missingstate000"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
