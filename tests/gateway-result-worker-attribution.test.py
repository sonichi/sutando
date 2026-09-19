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
hand-spelling the path: packages/ag2-sparrow is a standalone PyPI package and
cannot import skills/worker-pool/ in production, so both sides bind the same
src/pool_record.py contract (bundled into the package) for the layout, the
recipient grammar and the record predicate. Building the fixture from the real
writer keeps a future drift showing up as a failing test instead of a
silently-always-empty lookup (sonichi/sutando, 2026-09-16: _worker_of
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
import stat
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src" / "remote-gateway-bridge.py"
_POOL_SCRIPTS = _REPO / "skills" / "worker-pool" / "scripts"

sys.path.insert(0, str(_POOL_SCRIPTS))
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests" / "_helpers"))
import pool_delivery  # noqa: E402
import pool_record  # noqa: E402

from deadline import deadline  # noqa: E402


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

    # A ready result is routinely delivered while only `.pending` exists, so
    # reading `.flag` alone loses attribution for that whole window.

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

    def test_a_fifo_at_the_record_name_fails_closed_without_waiting(self):
        """A blocking read-open of a FIFO waits for a writer that never comes,
        so the resolver must classify one without ever waiting on the open."""
        tid = "task-fiforecord0000001"
        pend = pool_delivery.mark_done(Path(self.workspace), "worker-6", tid,
                                       published=False)
        # Control on the SAME path: a regular record there still resolves, so
        # the assertions below are about the file's type and nothing else.
        with deadline(5.0, "_worker_of over a regular record"):
            self.assertEqual(self.mod._worker_of(tid), "worker-6")
        os.unlink(pend)
        os.mkfifo(str(pend))
        self.assertTrue(stat.S_ISFIFO(os.lstat(pend).st_mode), pend)
        with deadline(5.0, "_worker_of over a FIFO record"):
            self.assertEqual(self.mod._worker_of(tid), "")
        with deadline(5.0, "_deliver_result_payload over a FIFO record"):
            self.assertNotIn("metadata", self._doc(tid))
        # The writer's own predicate reads the same record the same way.
        with deadline(5.0, "is_done_flag over a FIFO record"):
            self.assertFalse(pool_delivery.is_done_flag(pend))

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

    def test_an_unreadable_root_abstains_rather_than_reading_nobody(self):
        """The claim root's own OSError is NO READING, not "nobody claimed it".

        FileNotFoundError is caught above this branch and means the absent
        root, so only a non-FileNotFoundError OSError reaches it — the case
        where residue exists and is merely unreachable. Abstaining is what
        keeps an unreadable root from reading as an unattributed result.
        """
        tid = "task-unreadableroot0001"
        self._flag("worker-a", tid)
        # Control first: without the fault the same fixture MUST resolve, or
        # the assertion below would pass for a fixture that never worked.
        self.assertEqual(self.mod._worker_of(tid), "worker-a")
        if os.geteuid() == 0:
            self.skipTest("root ignores the mode bits this case depends on")
        root = pool_record.workers_root(self.mod._STATE)
        mode = root.stat().st_mode
        os.chmod(root, 0o000)
        try:
            self.assertEqual(self.mod._worker_of(tid), "")
        finally:
            os.chmod(root, mode)

    # --- the reader accepts only what the writer would itself write ------

    def test_a_stray_root_entry_does_not_suppress_attribution(self):
        """A non-directory beside the recipient folders is PROVEN not to be a
        recipient, so it must be skipped — not read as an unreadable claimant
        that suppresses the valid one next to it."""
        tid = "task-strayrootentry001"
        self._flag("worker-a", tid)
        self.assertEqual(self.mod._worker_of(tid), "worker-a")   # control
        root = pool_record.workers_root(self.mod._STATE)
        for stray in (".DS_Store", "README.txt", "worker-b"):
            (root / stray).write_text("")
        self.assertEqual(self.mod._worker_of(tid), "worker-a")
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": "worker-a"})

    def test_a_name_the_writer_rejects_never_attributes(self):
        """A recipient id mark_done() refuses with ValueError must not come
        back out of the reader as structured worker metadata."""
        tid = "task-badrecipientid001"
        bad = "Worker-NOT-WRITABLE"
        with self.assertRaises(ValueError):
            pool_delivery.mark_done(Path(self.workspace), bad, tid, published=True)
        d = pool_record.workers_root(self.mod._STATE) / bad / "done"
        d.mkdir(parents=True)
        (d / f"{tid}.flag").write_text("")
        self.assertEqual(self.mod._worker_of(tid), "")
        self.assertNotIn("metadata", self._doc(tid))
        # Control: the same hand-built record under a name the writer accepts
        # DOES resolve, so the assertion above is about the name, not the shape.
        self._flag("worker-a", "task-goodrecipientid01")
        self.assertEqual(self.mod._worker_of("task-goodrecipientid01"), "worker-a")

    def test_a_record_the_writers_predicate_rejects_never_attributes(self):
        """chmod-000: pool_delivery.is_done_flag() raises PermissionError on
        it, so the reader must abstain rather than resolve a worker from a
        record the writer's own predicate will not accept."""
        tid = "task-unreadablerecord1"
        flag = pool_delivery.mark_done(Path(self.workspace), "worker-a", tid,
                                       published=True)
        self.assertEqual(self.mod._worker_of(tid), "worker-a")   # control
        if os.geteuid() == 0:
            self.skipTest("root ignores the mode bits this case depends on")
        os.chmod(flag, 0o000)
        try:
            with self.assertRaises(PermissionError):
                pool_delivery.is_done_flag(flag)
            self.assertEqual(self.mod._worker_of(tid), "")
            self.assertNotIn("metadata", self._doc(tid))
        finally:
            os.chmod(flag, 0o600)



class AnAliasNeverNamesAnotherWorker(unittest.TestCase):
    """Reviewer repro (sonichi/sutando#4306): worker A's recipient dir is a
    symlink to worker B's real dir. If the writer publishes through it and the
    reader skips the alias, B is the unique claimant and the payload names B —
    another worker's identity on A's reply. Neither half may happen."""

    A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    setUp, _doc = WorkerAttribution.setUp, WorkerAttribution._doc

    def _alias(self):
        root = pool_delivery.done_flag(Path(self.workspace), self.B, "task-0alias00000000000").parent.parent.parent
        (root / self.B / "done").mkdir(parents=True)
        (root / self.A).symlink_to(root / self.B)
        return root

    def test_the_writer_refuses_and_the_payload_carries_no_identity(self):
        self._alias()
        tid = "task-0alias0000000000a"
        with self.assertRaises(OSError):
            pool_delivery.mark_done(Path(self.workspace), self.A, tid, published=True)
        self.assertEqual(self.mod._worker_of(tid), "")
        self.assertNotIn("metadata", self._doc(tid))

    def test_even_a_record_already_under_the_alias_is_never_attributed_to_b(self):
        # A record that reached B's folder some other way, with the alias still
        # present: the reader must abstain, never resolve the alias to B.
        root = self._alias()
        tid = "task-0alias0000000000b"
        real = pool_delivery.done_flag(Path(self.workspace), self.B, tid)
        real.write_text("")
        self.assertTrue((root / self.A / "done" / real.name).exists(), "fixture: alias resolves")
        self.assertEqual(self.mod._worker_of(tid), "", "the alias made B look unique")
        doc = self._doc(tid)
        self.assertNotIn("metadata", doc, f"payload named {doc.get('metadata')}")

    def test_control_without_the_alias_b_is_attributed(self):
        tid = "task-0alias0000000000c"
        pool_delivery.mark_done(Path(self.workspace), self.B, tid, published=True)
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": self.B})


class PromotionBetweenProbes(unittest.TestCase):
    """keweichen on #4306: the writer creates `.flag` and only then unlinks
    `.pending`, so probing flag-first can observe NEITHER name if promotion
    lands between the two probes — an unattributed result while the writer
    maintained continuous evidence throughout."""

    def setUp(self):
        self.mod = _load()
        self.workspace = tempfile.mkdtemp()
        self.mod._STATE = Path(self.workspace) / "state"

    def test_promotion_between_probes_still_attributes(self):
        """Drives the REAL writer between the two probes, not a fake: the first
        probe runs, then mark_done(published=True) promotes, then the second."""
        tid = "task-promotionrace0001"
        recipient = "core-7"
        pool_delivery.mark_done(Path(self.workspace), recipient, tid, published=False)

        real_probe = self.mod.pool_record.read_record_state
        state = {"n": 0}

        def racing_probe(path, *a, **k):
            state["n"] += 1
            if state["n"] == 1:
                # after the first probe resolves, let the writer promote
                try:
                    return real_probe(path, *a, **k)
                finally:
                    pool_delivery.mark_done(
                        Path(self.workspace), recipient, tid, published=True)
            return real_probe(path, *a, **k)

        with mock.patch.object(self.mod.pool_record, "read_record_state",
                               side_effect=racing_probe):
            got = self.mod._worker_of(tid)
        flag = pool_delivery.done_flag(Path(self.workspace), recipient, tid)
        pend = pool_delivery.pending_flag(Path(self.workspace), recipient, tid)
        self.assertEqual(
            got, recipient,
            f"lost attribution across promotion: flag_exists={flag.exists()} "
            f"pending_exists={pend.exists()}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
