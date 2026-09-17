#!/usr/bin/env python3
"""Attribution is read from ASSIGNMENT truth, not inferred from completion
residue: the bridge stamps metadata.worker_id from the record the router wrote
when it handed the task out, so a result carries its author even when no
done-flag residue exists at the moment the drain reads it.

The failure this replaces: _worker_of() answers only from
state/workers/<recipient>/done/<tid>.{flag,pending}, which exists only because
the worker got far enough to lay it and survives only as long as nothing
reaps it. Attribution that depends on the completion path cannot describe a
result whose completion path was interrupted — and the whole point of
attribution is to be right about exactly those.

Fixtures are built through the real recorder (pool_attribution.record) rather
than by hand-spelling state/attribution/<tid>: packages/ag2-sparrow is a
standalone PyPI package that cannot import skills/worker-pool/ in production,
so _assigned_worker()'s path literal is the only place the convention is
re-stated. Building from the writer is what makes a future drift fail here
instead of silently returning "" forever (the 2026-09-16 "cores" vs "workers"
drift shipped every result with no worker_id and six tests stayed green,
because the fixture reimplemented the same wrong path).

Run: python3 tests/gateway-assignment-attribution.test.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src" / "remote-gateway-bridge.py"
_POOL_SCRIPTS = _REPO / "skills" / "worker-pool" / "scripts"

sys.path.insert(0, str(_POOL_SCRIPTS))
import pool_attribution  # noqa: E402
import pool_delivery  # noqa: E402

W1 = "02e4302f00844397bac09533fc398248"
W2 = "212e8040d38d48b5aadab0db295dc33a"


def _load():
    spec = importlib.util.spec_from_file_location("_rgb_assign", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rgb_assign"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Captured(Exception):
    """Stops _deliver_result_payload once the payload is built, so assertions
    never depend on delivery-status enum semantics."""


class AssignmentAttribution(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.workspace = tempfile.mkdtemp()
        self.mod._STATE = Path(self.workspace) / "state"
        self.logs = []
        self.mod._log = self.logs.append
        self.seen = {}

        class _Backend:
            def publish(_s, tid, payload):
                self.seen["payload"] = json.loads(payload.decode())
                raise _Captured()

        self.mod._delivery_core = lambda: type("C", (), {"backend": _Backend()})()

    def _assign(self, tid: str, worker: str):
        """Through the router's own recorder, never a hand-spelled path."""
        self.assertTrue(pool_attribution.record(self.workspace, tid, worker))

    def _residue(self, recipient: str, tid: str):
        path = pool_delivery.done_flag(Path(self.workspace), recipient, tid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    def _sentinel(self, recipient: str, tid: str):
        """Through pool_delivery's own path function: the convention the bridge
        re-states is the one the pool writes, or this test cannot catch drift."""
        d = pool_delivery.deliveries_dir(Path(self.workspace), recipient)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}{pool_delivery.PENDING_SUFFIX}").write_text("")

    def _doc(self, tid: str) -> dict:
        self.seen.clear()
        with self.assertRaises(_Captured):
            self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!")
        return self.seen["payload"]

    # --- the case the old path could not answer -------------------------

    def test_assignment_alone_attributes_with_no_residue(self):
        """THE REGRESSION GUARD. No done-flag at all — the state after a reap,
        or before the worker promotes .pending. Residue-only attribution
        returns "" here; assignment truth answers."""
        tid = "task-aa11bb22cc33dd44ee"
        self._assign(tid, W1)
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": W1})

    def test_assignment_wins_over_disagreeing_residue(self):
        """Residue is an inference; assignment is the routing decision itself.
        When they disagree the recorded decision is authoritative."""
        tid = "task-bb22cc33dd44ee55ff"
        self._assign(tid, W1)
        self._residue(W2, tid)
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": W1})

    # --- the transition window must not regress -------------------------

    def test_residue_still_answers_when_unassigned(self):
        """A task routed before assignment records existed keeps working."""
        tid = "task-cc33dd44ee55ff6600"
        self._residue(W2, tid)
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": W2})

    def test_residue_fallback_surfaces_the_anomaly(self):
        """A result whose author was never recorded at routing time is the
        thing the store exists to catch — it must not pass silently."""
        tid = "task-dd44ee55ff66001122"
        self._residue(W2, tid)
        self._doc(tid)
        self.assertTrue(
            any("no assignment record" in m for m in self.logs),
            f"expected an anomaly log, got {self.logs}",
        )

    def test_assigned_task_logs_nothing(self):
        """Control for the assertion above: the anomaly log fires on the
        anomaly, not on every result."""
        tid = "task-ee55ff6600112233"
        self._assign(tid, W1)
        self._doc(tid)
        self.assertEqual(
            [m for m in self.logs if "no assignment record" in m], [])

    def test_neither_source_means_no_stamp(self):
        """A core-handled result carries no worker metadata at all — absent,
        not an empty string a consumer might render."""
        doc = self._doc("task-ff66001122334455")
        self.assertNotIn("metadata", doc)

    # --- a delivered task with no attribution must not pass silently -----

    def test_delivered_without_record_refuses_and_says_so(self):
        """THE GUARD. A delivery sentinel proves this was a worker's task, so
        returning nothing silently would relay it as if the core produced it."""
        tid = "task-5566778899001122"
        self._sentinel(W1, tid)
        doc = self._doc(tid)
        self.assertNotIn("metadata", doc)
        self.assertTrue(
            any("refusing to stamp" in m for m in self.logs),
            f"expected a loud refusal, got {self.logs}",
        )

    def test_core_result_stays_quiet(self):
        """Control: no sentinel, no record, no residue is an ORDINARY core
        result. If this fired, every core reply would log an anomaly."""
        self._doc("task-6677889900112233")
        self.assertEqual(
            [m for m in self.logs if "refusing to stamp" in m], [])

    def test_a_recorded_delivery_does_not_warn(self):
        """Control: sentinel AND record present is the normal worker path."""
        tid = "task-7788990011223344"
        self._assign(tid, W1)
        self._sentinel(W1, tid)
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": W1})
        self.assertEqual(
            [m for m in self.logs if "refusing to stamp" in m], [])

    # --- fails closed ----------------------------------------------------

    def test_directory_is_refused(self):
        """A non-regular record is malformed; the recorder would refuse to
        write it, so the reader refuses to believe it."""
        tid = "task-0011223344556677"
        (Path(self.workspace) / "state" / "attribution" / tid).mkdir(parents=True)
        self.assertEqual(self.mod._assigned_worker(tid), "")

    def test_non_worker_content_is_refused(self):
        """`core` is not a worker id. Recording one would make the bridge
        stamp a worker that never ran."""
        tid = "task-1122334455667788"
        d = Path(self.workspace) / "state" / "attribution"
        d.mkdir(parents=True, exist_ok=True)
        (d / tid).write_text("core")
        self.assertEqual(self.mod._assigned_worker(tid), "")

    def test_unreadable_record_is_refused(self):
        """An OSError from the stat is NO READING, not "never assigned" — the
        distinction fail-closed depends on, so it is exercised, not asserted."""
        tid = "task-3344556677889900"
        self._assign(tid, W1)
        with mock.patch.object(self.mod.os, "lstat",
                               side_effect=PermissionError("denied")):
            self.assertEqual(self.mod._assigned_worker(tid), "")

    def test_unreadable_content_is_refused(self):
        """Same for a record that stats fine and cannot be read."""
        tid = "task-4455667788990011"
        self._assign(tid, W1)
        with mock.patch.object(Path, "read_text",
                               side_effect=PermissionError("denied")):
            self.assertEqual(self.mod._assigned_worker(tid), "")

    def test_traversal_is_refused(self):
        self.assertEqual(self.mod._assigned_worker("../../etc/passwd"), "")

    def test_recorder_and_reader_agree_on_the_path(self):
        """The drift guard: the file the recorder writes is the file the
        reader reads. If either side moves, this fails."""
        tid = "task-2233445566778899"
        self._assign(tid, W1)
        self.assertTrue(
            pool_attribution.attribution_path(self.workspace, tid).is_file())
        self.assertEqual(self.mod._assigned_worker(tid), W1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
