#!/usr/bin/env python3
"""`write_task_file` publishes the task in one step.

Its return is defined as the Durable Work Model's `pending` transition, so that
is the moment that must be atomic. A plain `write_text` (O_TRUNC then write) is
observable mid-write by anything sweeping the directory — and `task:` is the LAST
header by contract, so a truncation after the headers still parses as a VALID
task: right source, right tier, right priority, ask short or empty. Nothing
downstream can tell.

⚠ A TIMING TEST DOES NOT DISCRIMINATE HERE. Driving the writer while polling in
another thread passes even with atomicity removed, because the write finishes
inside one sample gap. So the observer runs BETWEEN the two halves of the body
write and the verdict is deterministic.

Run: python3 tests/local-task-protocol-atomic-write.test.py
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_s = importlib.util.spec_from_file_location("ltp", str(REPO / "src" / "local_task_protocol.py"))
ltp = importlib.util.module_from_spec(_s)
sys.modules["ltp"] = ltp
_s.loader.exec_module(ltp)

TID = "task-1788000000000"
HEADERS = [("id", TID), ("source", "ag2space"), ("access_tier", "owner"), ("priority", "urgent")]
BODY = "Call the pharmacy and confirm the refill is ready before 6pm, and if it is "\
       "not, ask them to transfer it to the Gilbert branch."


class _SplitWriter:
    """Real file object whose first write() is cut in half, with a hook between."""

    def __init__(self, real, observe):
        self._real, self._observe = real, observe
        self._fired = False

    def write(self, s):
        if self._fired:
            return self._real.write(s)
        self._fired = True
        cut = max(1, len(s) // 2)
        n = self._real.write(s[:cut])
        self._real.flush()
        self._observe()
        return n + self._real.write(s[cut:])

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._real, name)


def observe_mid_write(writer, tasks_dir: Path):
    """(what a `*.txt` sweep would claim mid-write, how many times we looked)."""
    seen, calls = [], []

    def observe():
        calls.append(1)
        seen.append(sorted(p.name for p in tasks_dir.glob("*.txt")))

    real_fdopen, real_open = ltp.os.fdopen, open
    ltp.os.fdopen = lambda *a, **k: _SplitWriter(real_fdopen(*a, **k), observe)
    ltp.open = lambda *a, **k: _SplitWriter(real_open(*a, **k), observe)
    try:
        writer()
    finally:
        ltp.os.fdopen = real_fdopen
        del ltp.open
    return (seen[0] if seen else "OBSERVER-NEVER-RAN"), len(calls)


class AtomicPublish(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.addCleanup(self._d.cleanup)
        self.dir = Path(self._d.name)

    def test_a_sweep_never_sees_the_task_mid_write(self):
        seen, calls = observe_mid_write(
            lambda: ltp.write_task_file(self.dir, TID, HEADERS, BODY), self.dir)
        self.assertEqual(calls, 1, "interposition did not fire — the verdict would be vacuous")
        self.assertEqual(seen, [], f"a sweep could claim a partial task: {seen}")

    def test_the_naive_writer_DOES_expose_it(self):
        """Positive control. Without it the arm above passes for any writer the
        observer never catches — which is exactly how a timing test passes."""
        def naive():
            with ltp.open(self.dir / f"{TID}.txt", "w", encoding="utf-8") as f:
                f.write("id: x\ntask: y\n")
        seen, calls = observe_mid_write(naive, self.dir)
        self.assertEqual(calls, 1)
        self.assertEqual(seen, [f"{TID}.txt"], "control did not reproduce the defect")

    def test_the_published_task_is_complete_and_parses(self):
        p = ltp.write_task_file(self.dir, TID, HEADERS, BODY)
        parsed = ltp.parse_task_headers(p.read_text())
        self.assertEqual(parsed.get("source"), "ag2space")
        self.assertEqual(parsed.get("access_tier"), "owner")
        self.assertEqual(parsed.body.strip(), BODY)

    def test_no_temp_is_left_behind(self):
        ltp.write_task_file(self.dir, TID, HEADERS, BODY)
        self.assertEqual([p.name for p in self.dir.iterdir()], [f"{TID}.txt"])

    def test_the_temp_cannot_be_claimed_by_a_txt_sweep(self):
        """The dot is not the protection — `pathlib.glob('*')` matches dotfiles,
        and the watcher's own sweep is `"$TASKS_DIR"/*.txt`. The missing `.txt`
        is what keeps every sweep off it."""
        captured = []
        real_mkstemp = ltp.tempfile.mkstemp

        def spy(*a, **k):
            fd, p = real_mkstemp(*a, **k)
            captured.append(Path(p))
            return fd, p

        ltp.tempfile.mkstemp = spy
        try:
            ltp.write_task_file(self.dir, TID, HEADERS, BODY)
        finally:
            ltp.tempfile.mkstemp = real_mkstemp
        tmp = captured[0]
        self.assertFalse(tmp.name.endswith(".txt"), f"temp {tmp.name} is claimable")
        self.assertEqual(tmp.parent, self.dir, "os.replace is atomic only within one filesystem")

    def test_a_failed_write_leaves_no_task_and_no_temp(self):
        class Boom(Exception):
            pass

        real_fdopen = ltp.os.fdopen

        def exploding(*a, **k):
            fh = real_fdopen(*a, **k)

            class F:
                def __enter__(self_):
                    fh.__enter__()
                    return self_

                def __exit__(self_, *e):
                    return fh.__exit__(*e)

                def write(self_, s):
                    fh.write(s[: max(1, len(s) // 2)])  # real bytes land, THEN fail
                    fh.flush()
                    raise Boom()

                def __getattr__(self_, n):
                    return getattr(fh, n)

            return F()

        ltp.os.fdopen = exploding
        try:
            with self.assertRaises(Boom):
                ltp.write_task_file(self.dir, TID, HEADERS, BODY)
        finally:
            ltp.os.fdopen = real_fdopen
        self.assertEqual(list(self.dir.iterdir()), [],
                         "a failed write must leave neither a task nor its temp")

    def test_two_writers_of_the_SAME_id_do_not_share_a_temp(self):
        """A fixed temp name is a narrower version of the bug being fixed: two
        processes staging the same task id would write the same path, so one can
        truncate the other's temp and `os.replace` publishes a mix. The gateway's
        own staged write (`_stage_durable`) already carries pid+uuid for this."""
        names = []
        real_mkstemp = ltp.tempfile.mkstemp

        def spy(*a, **k):
            fd, p = real_mkstemp(*a, **k)
            names.append(Path(p).name)
            return fd, p

        ltp.tempfile.mkstemp = spy
        try:
            ltp.write_task_file(self.dir, TID, HEADERS, "body A")
            ltp.write_task_file(self.dir, TID, HEADERS, "body B")
        finally:
            ltp.tempfile.mkstemp = real_mkstemp
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0], names[1], "two writers of one id shared a temp path")
        for n in names:
            self.assertTrue(n.startswith(f".{TID}."), n)
            self.assertTrue(n.endswith(".tmp"), n)

    def test_a_failure_BEFORE_the_temp_exists_does_not_mask_the_original_error(self):
        """`open` itself failing leaves no temp, so cleanup's unlink raises too.
        The caller must still see the real cause, not a FileNotFoundError from
        the cleanup — swallowing only the cleanup error is what preserves it."""
        class Boom(Exception):
            pass

        def vanish(fd, *a, **k):
            # Close before removal so this fault injection has the same shape
            # on Windows, which does not unlink open files.
            os.close(fd)
            for p in list(self.dir.iterdir()):
                p.unlink()
            raise Boom()

        real_fdopen = ltp.os.fdopen
        ltp.os.fdopen = vanish
        try:
            with self.assertRaises(Boom):
                ltp.write_task_file(self.dir, TID, HEADERS, BODY)
        finally:
            ltp.os.fdopen = real_fdopen
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_the_stamper_still_applies(self):
        """The stamper is a provider-neutral seam applied just before persist;
        moving the write must not move it out of the persisted bytes."""
        ltp.set_task_stamper(lambda t: "envelope_hmac: v1:deadbeef\n" + t)
        try:
            p = ltp.write_task_file(self.dir, TID, HEADERS, BODY)
        finally:
            ltp.set_task_stamper(None)
        self.assertIn("envelope_hmac: v1:deadbeef", p.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
