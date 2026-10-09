#!/usr/bin/env python3
"""restore() must not clobber a live result that lands inside its own window.

`restore()` promises, in its docstring, to refuse to overwrite a live result.
It checked `target.exists()` and then called `Path.rename`, which on POSIX
silently replaces the destination — so a producer writing the canonical name
between those two statements lost its newer reply to a resurrected older one.
The check and the move have to be one atomic step for the promise to hold.

The interleaving is injected at the production move itself, not simulated by a
reimplementation, so the test exercises the shipped function.
"""
import importlib.util
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "undelivered_quarantine", ROOT / "src" / "undelivered_quarantine.py")
uq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uq)


class ConcurrentProducerAtTheMoveBoundary(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.results = Path(self.tmp.name)
        (self.results / uq.DIRNAME).mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def _quarantine(self, task_id, body, when):
        p = self.results / uq.DIRNAME / uq.quarantine_name(f"task-{task_id}", when)
        p.write_text(body, encoding="utf-8")
        return p

    def test_a_reply_written_inside_the_window_survives(self):
        """A producer lands a newer reply at the canonical name in the instant
        before restore() moves: the live reply wins and the quarantined body
        stays quarantined (the guarantee: never overwrite, never lose)."""
        task = "abc123"
        q = self._quarantine(task, "OLD quarantined body", 1)
        target = self.results / f"task-{task}.txt"
        real = uq.rename_noreplace
        fired = {"n": 0}

        def write_then_move(src, dst, *a, **kw):
            fired["n"] += 1
            Path(dst).write_text("NEW live reply", encoding="utf-8")
            return real(src, dst, *a, **kw)
        with unittest.mock.patch.object(uq, "rename_noreplace", write_then_move):
            outcome, _ = uq.restore(self.results, task)
        self.assertGreater(fired["n"], 0, "the injection never ran — the test proves nothing")
        self.assertEqual(target.read_text(encoding="utf-8"), "NEW live reply",
                         f"restore() overwrote a live result; outcome={outcome}")
        self.assertIs(outcome, uq.RestoreOutcome.LIVE_RESULT_PRESENT)
        self.assertEqual(q.read_text(encoding="utf-8"), "OLD quarantined body",
                         "the refused body must stay quarantined")

    def test_a_producer_retaking_the_name_after_the_install_never_costs_the_body(self):
        """The link-then-unlink schedule: a producer replaces the canonical name
        after the body was installed and before any cleanup. The body must still
        exist somewhere, and RESTORED may only be claimed if it is at the name."""
        task = "race1"
        self._quarantine(task, "OLD quarantined body", 1)
        target = self.results / f"task-{task}.txt"
        real_unlink = os.unlink

        def producer_then_unlink(path, *a, **kw):
            tmp = self.results / "producer.tmp"
            tmp.write_text("NEW live reply", encoding="utf-8")
            os.replace(tmp, target)
            return real_unlink(path, *a, **kw)
        with unittest.mock.patch.object(os, "unlink", producer_then_unlink):
            outcome, _ = uq.restore(self.results, task)
        survivors = [p.read_text(encoding="utf-8") for p in
                     [target, *self.results.joinpath(uq.DIRNAME).glob("*.txt")] if p.exists()]
        self.assertIn("OLD quarantined body", survivors, "restore() lost the body it was installing")
        if outcome is uq.RestoreOutcome.RESTORED:
            self.assertEqual(target.read_text(encoding="utf-8"), "OLD quarantined body")

    def test_without_a_primitive_restore_refuses_and_keeps_the_body(self):
        task = "noprim1"
        q = self._quarantine(task, "OLD quarantined body", 1)
        with unittest.mock.patch.object(uq, "_RENAME", None), \
                unittest.mock.patch.object(uq, "RENAME_PRIMITIVE", "none"):
            outcome, path = uq.restore(self.results, task)
        self.assertIs(outcome, uq.RestoreOutcome.NO_SAFE_MOVE)
        self.assertEqual(path, q)
        self.assertEqual(q.read_text(encoding="utf-8"), "OLD quarantined body")
        self.assertFalse((self.results / f"task-{task}.txt").exists())

    def test_the_ordinary_restore_still_works(self):
        """Negative control: with no concurrent writer the body is restored."""
        task = "plain1"
        self._quarantine(task, "the only body", 1)
        outcome, path = uq.restore(self.results, task)
        self.assertIs(outcome, uq.RestoreOutcome.RESTORED)
        self.assertEqual(path.read_text(encoding="utf-8"), "the only body")

    def test_nothing_quarantined_is_still_distinct(self):
        outcome, path = uq.restore(self.results, "absent")
        self.assertIs(outcome, uq.RestoreOutcome.NOTHING_QUARANTINED)
        self.assertIsNone(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
