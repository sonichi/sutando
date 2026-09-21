#!/usr/bin/env python3
"""Contract for src/delivery/readiness.py and delegation by every delivery consumer.

Readiness of a task-result file has one owner. Each consumer binds its own
results directory and keeps only provider-specific delivery.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import delivery.readiness as result_ready  # noqa: E402
from delivery.readiness import (  # noqa: E402
    alloc_task_id, is_ready_body, read_ready_result,
    read_ready_result_for_delivery, stamp_result_file,
)

# Every consumer that decides "is this result ready to deliver?".
CONSUMERS = {
    "discord-bridge": REPO / "src" / "discord-bridge.py",
    "slack-bridge": REPO / "src" / "slack-bridge.py",
    "telegram-bridge": REPO / "src" / "telegram-bridge.py",
    "remote_gateway_bridge": (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"
                              / "remote_gateway_bridge.py"),
}


class ContractTest(unittest.TestCase):
    # `proactive-` is stamp-exempt, so these cases test the readiness contract
    # ALONE; a `task-` fixture would assert "no ID added" as a silent side effect.
    def _write(self, td: str, text: str | None, name: str = "proactive-abc.txt") -> Path:
        p = Path(td) / name
        if text is not None:
            p.write_text(text)
        return p

    def test_missing_file_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(read_ready_result(self._write(td, None)))

    def test_zero_byte_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(read_ready_result(self._write(td, "")))

    def test_whitespace_only_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(read_ready_result(self._write(td, "\n \t\n")))

    def test_directory_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "adir"
            d.mkdir()
            self.assertIsNone(read_ready_result(d))

    def test_invalid_utf8_is_not_ready(self):
        """A partial write can land mid-character; decoding must not raise."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "task-abc.txt"
            p.write_bytes(b"answer \xff\xfe")
            self.assertIsNone(read_ready_result(p))

    def test_body_is_returned_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_ready_result(self._write(td, "  hi \n")), "hi")

    def test_task_file_is_stamped_exactly_once_under_concurrency(self):
        """Two consumers reading one result must mint ONE id, not two.

        This is the defect the review named: read, allocate and persist were
        three separate steps, so both readers saw the unstamped body, both
        allocated, and the id delivered on the wire disagreed with the one left
        in the archive — while burning two counts for one completion.

        Fails without the single-lock transaction: the two threads return
        different ids and the counter advances twice.

        The interleaving is FORCED, not hoped for. Simply starting two threads
        on a barrier does not reproduce it — measured: that version passed
        against the pre-fix code, because one thread ran to completion before
        the other read. So both threads are held at the decide-to-stamp point
        until each has seen the unstamped body, which is exactly the window the
        old read-then-allocate-then-write shape left open.
        """
        import json
        import threading
        import result_ready
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / "results"
            results.mkdir()
            p = results / "task-race.txt"
            p.write_text("the answer")
            out = []
            both_have_read = threading.Barrier(2, timeout=10)
            real_needs = result_ready.needs_task_stamp

            waited = set()
            waited_lock = threading.Lock()

            def gated(name, body):
                verdict = real_needs(name, body)
                # Once per THREAD: the second, in-transaction check must not gate,
                # or a thread blocks on a barrier its partner already cleared.
                with waited_lock:
                    first = threading.get_ident() not in waited
                    waited.add(threading.get_ident())
                if verdict and first:
                    both_have_read.wait()   # neither proceeds until both have read
                return verdict

            result_ready.needs_task_stamp = gated
            self.addCleanup(setattr, result_ready, "needs_task_stamp", real_needs)

            def go():
                out.append(read_ready_result_for_delivery(p))

            ts = [threading.Thread(target=go) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()

            ids = {re.match(r"\[task (\d{8}-\d{3})\]", b or "").group(1) for b in out}
            self.assertEqual(len(ids), 1, f"two ids minted for one result: {out}")
            counter = json.loads((Path(td) / "state" / "task-counter.json").read_text())
            self.assertEqual(counter["count"], 1, "counter advanced more than once")
            # And the file on disk carries the same id that was delivered.
            self.assertTrue(p.read_text().startswith(f"[task {ids.pop()}]"))

    def test_a_result_that_cannot_be_stamped_is_not_delivered(self):
        """Fail CLOSED. An unstampable result must read as not-ready, not send
        unstamped: the file survives and is retried, which is recoverable —
        a reply already sent without an id is not."""
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / "results"
            results.mkdir()
            p = results / "task-nostamp.txt"
            p.write_text("the answer")
            # state/ occupied by a regular file → mkdir and the counter both fail
            (Path(td) / "state").write_text("not a directory")
            self.assertIsNone(read_ready_result_for_delivery(p),
                              "delivered without an id")
            self.assertEqual(p.read_text(), "the answer", "file must survive for retry")

    def test_marker_only_body_is_ready(self):
        """[no-send] is a real body — marker handling belongs to result_markers."""
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_ready_result(self._write(td, "[no-send]")), "[no-send]")

    def test_reading_does_not_consume_the_file(self):
        """Not-ready must be retryable: the file survives for the next pass."""
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "")
            self.assertIsNone(read_ready_result(p))
            self.assertTrue(p.exists(), "an unready result file was consumed")
            p.write_text("the answer")
            self.assertEqual(read_ready_result(p), "the answer")

    def test_is_ready_body(self):
        for value in ("", "   ", "\n", None):
            self.assertFalse(is_ready_body(value), repr(value))
        self.assertTrue(is_ready_body("x"))


class DelegationTest(unittest.TestCase):
    """No consumer may re-implement the readiness check."""

    def test_every_consumer_imports_the_owner(self):
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                self.assertTrue(path.exists(), f"{name}: missing at {path}")
                self.assertRegex(
                    path.read_text(),
                    r"from (?:delivery\.readiness|\.result_ready) import read_ready_result",
                    f"{name}: does not import read_ready_result from the shared owner",
                )

    def test_no_consumer_hand_rolls_the_result_guard(self):
        """Catches a copy reintroduced under any local variable name."""
        pat = re.compile(
            r"(\w+)\s*=\s*\w*(?:result_file|rfile)\w*\.read_text\(\)\.strip\(\)",
        )
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                hits = pat.findall(path.read_text())
                self.assertEqual(
                    hits, [],
                    f"{name}: reads a result file directly ({hits}) — readiness "
                    f"belongs to src/delivery/readiness.read_ready_result",
                )

    def test_sparrow_bundle_matches_src(self):
        pkg = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "result_ready.py")
        self.assertTrue(pkg.exists(), "result_ready.py not bundled into ag2-sparrow")
        self.assertEqual(
            pkg.read_text(), (REPO / "src" / "delivery" / "readiness.py").read_text(),
            "ag2-sparrow copy drifted from src/ — run tools/sync_from_src.py",
        )


class FailClosedBranches(unittest.TestCase):
    """The allocate/stamp paths return None rather than raise, on every failure.

    These branches are the whole safety argument: a result that cannot be given a
    durable ID stays on disk and is retried, instead of being delivered unstamped.
    A path that only ever runs in the happy case is not evidence of that.
    """

    def _ws(self, td):
        ws = Path(td)
        (ws / "results").mkdir()
        (ws / "state").mkdir()
        return ws

    def test_a_non_integer_count_falls_back_to_the_history_floor(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            # TODAY's date, or the stale-day branch replaces the dict before the
            # int() is ever reached and this stops exercising the fallback.
            today = __import__("datetime").date.today().strftime("%Y%m%d")
            (ws / "state" / "task-counter.json").write_text(
                json.dumps({"date": today, "count": {"not": "an int"}}))
            got = alloc_task_id(ws / "results")
            self.assertRegex(got or "", r"^\d{8}-001$")

    def test_an_unwritable_counter_yields_None_not_an_exception(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            # The atomic temp target is a DIRECTORY, so write_text raises inside
            # the allocation. Deterministic, and independent of uid/permissions.
            (ws / "state" / "task-counter.json.tmp").mkdir()
            self.assertIsNone(alloc_task_id(ws / "results"))

    def test_a_state_path_that_is_a_file_yields_None(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "results").mkdir()
            (ws / "state").write_text("not a directory")
            self.assertIsNone(alloc_task_id(ws / "results"))

    def test_an_unlock_failure_does_not_lose_the_allocated_id(self):
        """The finally-block swallows a close/unlock error: the ID was already
        persisted, so raising here would discard a counter value that is spent."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            real = result_ready.fcntl

            class Shim:
                LOCK_EX, LOCK_UN = real.LOCK_EX, real.LOCK_UN

                @staticmethod
                def flock(f, op):
                    if op == real.LOCK_UN:
                        raise OSError("unlock failed")
                    return real.flock(f, op)

            result_ready.fcntl = Shim
            try:
                got = alloc_task_id(ws / "results")
            finally:
                result_ready.fcntl = real
            self.assertRegex(got or "", r"^\d{8}-001$")

    def test_stamp_returns_None_when_the_file_cannot_be_re_read(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            target = ws / "results" / "task-x.txt"
            target.mkdir()  # read_text raises IsADirectoryError under the lock
            self.assertIsNone(stamp_result_file(target))

    def test_stamp_returns_None_when_the_body_emptied_under_the_lock(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            target = ws / "results" / "task-x.txt"
            target.write_text("   \n")
            self.assertIsNone(stamp_result_file(target))

    def test_stamp_returns_None_when_no_id_could_be_allocated(self):
        """Fail CLOSED: no durable ID means the result is not ready, never an
        unstamped send."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            target = ws / "results" / "task-x.txt"
            target.write_text("the answer")
            real = result_ready._alloc_locked
            result_ready._alloc_locked = lambda state: None
            try:
                self.assertIsNone(stamp_result_file(target))
            finally:
                result_ready._alloc_locked = real
            self.assertEqual(target.read_text(), "the answer", "must not rewrite on failure")

    def test_stamp_survives_an_unlock_failure_after_persisting(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            target = ws / "results" / "task-x.txt"
            target.write_text("the answer")
            real = result_ready.fcntl

            class Shim:
                LOCK_EX, LOCK_UN = real.LOCK_EX, real.LOCK_UN

                @staticmethod
                def flock(f, op):
                    if op == real.LOCK_UN:
                        raise OSError("unlock failed")
                    return real.flock(f, op)

            result_ready.fcntl = Shim
            try:
                got = stamp_result_file(target)
            finally:
                result_ready.fcntl = real
            self.assertTrue((got or "").endswith("the answer"), got)
            self.assertTrue(target.read_text().startswith("[task "), target.read_text())

    def test_a_persist_failure_then_retry_spends_exactly_one_id(self):
        """The count commits before the body is written. An attempt that dies in
        that window must resume on its reserved ID, not spend a second one."""
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            target = ws / "results" / "task-1.txt"
            target.write_text("the one and only completion\n")

            # Force the persist to fail AFTER the count commits: the atomic temp
            # target is a directory, so the body write raises.
            blocker = target.with_name(target.name + ".stamp.tmp")
            blocker.mkdir()
            self.assertIsNone(stamp_result_file(target), "must fail closed")
            blocker.rmdir()

            second = stamp_result_file(target)
            counter = json.loads((ws / "state" / "task-counter.json").read_text())
            history = json.loads((ws / "state" / "task-completions-daily.json").read_text())
            today = __import__("datetime").date.today().strftime("%Y%m%d")

            self.assertEqual(counter["count"], 1, "one completion must spend one id")
            self.assertEqual(history.get(today), 1, "history must not double-count it")
            self.assertTrue((second or "").startswith(f"[task {today}-001]"),
                            f"retry must resume on the reserved id, got {second!r}")
            self.assertNotIn("pending", counter, "reservation must clear once persisted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
