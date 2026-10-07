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
import sys
import unittest
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
        """The exact production-writer interleaving, driven through restore()."""
        task = "abc123"
        self._quarantine(task, "OLD quarantined body", 1)
        target = self.results / f"task-{task}.txt"

        # The concurrent producer: land a newer reply at the canonical name
        # after restore() has looked, in the instant before it moves.
        _os = getattr(uq, "os", None)
        real_link = getattr(_os, "link", None) if _os is not None else None
        real_rename = Path.rename
        fired = {"n": 0}

        def _write_then_delegate(path_self, dst, *a, **kw):
            fired["n"] += 1
            Path(dst).write_text("NEW live reply", encoding="utf-8")
            return real_rename(path_self, dst, *a, **kw)

        def _write_then_link(src, dst, *a, **kw):
            fired["n"] += 1
            Path(dst).write_text("NEW live reply", encoding="utf-8")
            return real_link(src, dst, *a, **kw)

        Path.rename = _write_then_delegate
        if real_link is not None:
            _os.link = _write_then_link
        try:
            outcome, _ = uq.restore(self.results, task)
        finally:
            Path.rename = real_rename
            if real_link is not None:
                _os.link = real_link

        self.assertGreater(fired["n"], 0,
                           "the injection never ran — the test proves nothing")
        self.assertEqual(
            target.read_text(encoding="utf-8"), "NEW live reply",
            "restore() overwrote a live result that landed inside its window; "
            f"outcome={outcome}")
        self.assertIs(outcome, uq.RestoreOutcome.LIVE_RESULT_PRESENT)

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
