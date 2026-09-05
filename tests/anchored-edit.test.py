#!/usr/bin/env python3
"""An edit that changes nothing must not exit 0.

Measured 2026-09-04, three times across two agents in one session: a patch
script printed "second copy updated" against an anchor the file did not
contain; a build_log append lost its redirect and rendered to the terminal;
two task closures were narrated with no result file written. In all three the
success message was generated INDEPENDENTLY of the operation, so it was never
evidence about the operation at all.

Run: python3 tests/anchored-edit.test.py
"""
import contextlib
import importlib.util
import io
import subprocess
import sys
import time
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "scripts" / "anchored-edit.py"
spec = importlib.util.spec_from_file_location("ae", TOOL)
ae = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ae)


class AnchoredEdit(unittest.TestCase):
    def test_a_real_edit_applies(self):
        """Control: without this, refusing everything would pass every other test."""
        out, n = ae.apply_edit("alpha beta", "beta", "gamma")
        self.assertEqual((out, n), ("alpha gamma", 1))

    def test_a_drifted_anchor_refuses_instead_of_no_op(self):
        with self.assertRaises(ValueError) as cm:
            ae.apply_edit("alpha beta", "delta", "gamma")
        self.assertIn("absent", str(cm.exception))

    def test_an_ambiguous_anchor_refuses_unless_deliberate(self):
        with self.assertRaises(ValueError):
            ae.apply_edit("x x", "x", "y")
        self.assertEqual(ae.apply_edit("x x", "x", "y", allow_multi=True), ("y y", 2))

    def test_old_equals_new_is_a_no_op_and_refuses(self):
        with self.assertRaises(ValueError):
            ae.apply_edit("alpha", "alpha", "alpha")

    def test_an_empty_anchor_refuses(self):
        """It matches at every position, so it is never the edit anyone meant."""
        with self.assertRaises(ValueError):
            ae.apply_edit("alpha", "", "x")

    def _run(self, text, *argv):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "f.txt"
            f.write_text(text)
            p = subprocess.run([sys.executable, str(TOOL), str(f), *argv],
                               capture_output=True, text=True)
            return p.returncode, p.stdout, p.stderr, f.read_text()

    def test_the_cli_exits_2_and_writes_nothing_when_the_anchor_drifted(self):
        rc, out, err, text = self._run("alpha", "--old", "delta", "--new", "x")
        self.assertEqual(rc, 2)
        self.assertEqual(text, "alpha", "a refused edit must leave the file alone")
        self.assertIn("REFUSED", err)
        self.assertEqual(out, "", "a refusal must print no success receipt")

    def test_the_cli_receipt_is_read_back_from_the_file(self):
        rc, out, err, text = self._run("alpha beta", "--old", "beta", "--new", "gamma")
        self.assertEqual(rc, 0)
        self.assertEqual(text, "alpha gamma")
        self.assertIn("replacement present 1x", out)

    def test_count_mismatch_refuses(self):
        rc, out, err, text = self._run("x x", "--old", "x", "--new", "y",
                                       "--allow-multi", "--count", "3")
        self.assertEqual(rc, 2)
        self.assertEqual(text, "x x")


class AtomicReplacement(unittest.TestCase):
    """qingyun-wu, 2026-09-04: write_text() truncates the live target, and a
    read-back compared against `before` passes on ANY different content --
    including a concurrent writer's. Both halves are tested here."""

    def _tmp(self, body):
        d = Path(tempfile.mkdtemp())
        f = d / "target.txt"
        f.write_text(body)
        return f

    def test_a_failed_write_retains_the_original_and_prints_no_receipt(self):
        f = self._tmp("alpha beta")
        with unittest.mock.patch.object(ae, "_atomic_write",
                                        side_effect=OSError("ENOSPC")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = ae.main([str(f), "--old", "beta", "--new", "gamma"])
        self.assertEqual(rc, 2)
        self.assertEqual(f.read_text(), "alpha beta",
                         "a failed write must leave the original intact")
        self.assertEqual(buf.getvalue(), "", "no success receipt on a failed write")

    def test_content_that_is_merely_DIFFERENT_does_not_satisfy_the_receipt(self):
        """The measured hole: comparing the re-read against `before` accepts a
        concurrent writer's bytes as proof that MY edit landed."""
        f = self._tmp("alpha beta")

        def _clobber(path, text, expect):
            path.write_text("something another writer put here")

        with unittest.mock.patch.object(ae, "_atomic_write", side_effect=_clobber):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = ae.main([str(f), "--old", "beta", "--new", "gamma"])
        self.assertEqual(rc, 2, "different-from-before is not proof the edit landed")
        self.assertEqual(buf.getvalue(), "")

    def test_the_target_is_never_truncated_in_place(self):
        """No window exists in which the target is empty: the temp sibling
        carries the new bytes and os.replace swaps them in one step."""
        f = self._tmp("alpha beta")
        seen = []
        real = ae.os.replace

        def _spy(src, dst):
            seen.append(Path(dst).read_text())   # the target, just before the swap
            return real(src, dst)

        with unittest.mock.patch.object(ae.os, "replace", side_effect=_spy):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = ae.main([str(f), "--old", "beta", "--new", "gamma"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["alpha beta"],
                         "the target still held its ORIGINAL bytes at swap time")
        self.assertEqual(f.read_text(), "alpha gamma")

    def test_mode_is_preserved_across_the_replace(self):
        f = self._tmp("alpha beta")
        f.chmod(0o640)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = ae.main([str(f), "--old", "beta", "--new", "gamma"])
        self.assertEqual(rc, 0)
        self.assertEqual(f.stat().st_mode & 0o777, 0o640)

    def test_no_temp_sibling_survives_a_failure(self):
        f = self._tmp("alpha beta")
        with unittest.mock.patch.object(ae.os, "replace", side_effect=OSError("boom")):
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                rc = ae.main([str(f), "--old", "beta", "--new", "gamma"])
        self.assertEqual(rc, 2)
        self.assertEqual(f.read_text(), "alpha beta")
        # The lock sidecar is durable and expected; a .tmp is the leak. Listing
        # every name would now pass for the wrong reason, so name the leak.
        leaked = [q.name for q in f.parent.iterdir() if q.name.endswith(".tmp")]
        self.assertEqual(leaked, [], "a failed replace must not leave a temp file behind")
        self.assertEqual(sorted(q.name for q in f.parent.iterdir()),
                         ["target.txt", "target.txt" + ae.LOCK_SUFFIX],
                         "nothing but the target and its lock may remain")


class InProcessPaths(unittest.TestCase):
    """The CLI cases run under subprocess, so coverage cannot see the lines they
    execute. These drive the same branches in-process, which is also the only way
    `_atomic_write` itself gets executed rather than mocked."""

    def _tmp(self, body="alpha beta"):
        f = Path(tempfile.mkdtemp()) / "t.txt"
        f.write_text(body)
        return f

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ae.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_old_file_and_new_file_are_read_from_disk(self):
        f = self._tmp()
        d = f.parent
        (d / "o").write_text("beta")
        (d / "n").write_text("gamma")
        rc, out, _ = self._main([str(f), "--old-file", str(d / "o"),
                                 "--new-file", str(d / "n")])
        self.assertEqual(rc, 0)
        self.assertEqual(f.read_text(), "alpha gamma")

    def test_missing_target_refuses(self):
        rc, out, err = self._main(["/nonexistent/nope.txt", "--old", "a", "--new", "b"])
        self.assertEqual(rc, 2)
        self.assertIn("no such file", err)
        self.assertEqual(out, "")

    def test_neither_old_nor_old_file_refuses(self):
        rc, out, err = self._main([str(self._tmp())])
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")

    def test_drifted_anchor_refuses_in_process(self):
        f = self._tmp()
        rc, out, err = self._main([str(f), "--old", "delta", "--new", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("REFUSED", err)
        self.assertEqual(f.read_text(), "alpha beta")

    def test_count_mismatch_refuses_in_process(self):
        f = self._tmp("x x")
        rc, out, err = self._main([str(f), "--old", "x", "--new", "y",
                                   "--allow-multi", "--count", "3"])
        self.assertEqual(rc, 2)
        self.assertEqual(f.read_text(), "x x")


class AtomicWriteDirect(unittest.TestCase):
    """`_atomic_write` executed for real. Every other test mocks it, so without
    this the helper the review asked for is the one thing never run."""

    def test_it_replaces_content_and_preserves_mode(self):
        f = Path(tempfile.mkdtemp()) / "t.txt"
        f.write_text("old")
        f.chmod(0o600)
        ae._atomic_write(f, "new", "old")
        self.assertEqual(f.read_text(), "new")
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_a_failure_mid_write_leaves_no_temp_and_keeps_the_original(self):
        f = Path(tempfile.mkdtemp()) / "t.txt"
        f.write_text("original")
        with unittest.mock.patch.object(ae.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                ae._atomic_write(f, "replacement", "original")
        self.assertEqual(f.read_text(), "original")
        self.assertEqual([q.name for q in f.parent.iterdir()], ["t.txt"])


class IntegrityContract(unittest.TestCase):
    """qingyun-wu, 2026-09-04, both reproduced with controls: os.replace swaps a
    SYMLINK entry rather than editing its target, and the edit computed from
    `before` was written unconditionally, so a writer landing between the read
    and the replace was silently lost."""

    def _dir(self):
        return Path(tempfile.mkdtemp())

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ae.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_a_symlink_target_is_refused_not_retargeted(self):
        d = self._dir()
        real = d / "real.txt"
        real.write_text("alpha beta")
        link = d / "link.txt"
        link.symlink_to(real)
        rc, out, err = self._main([str(link), "--old", "beta", "--new", "gamma"])
        self.assertEqual(rc, 2)
        self.assertIn("symlink", err)
        self.assertEqual(out, "", "a refusal must print no receipt")
        self.assertEqual(real.read_text(), "alpha beta", "the target must be untouched")
        self.assertTrue(link.is_symlink(), "the link must still be a link, not a regular file")

    def test_a_write_landing_after_the_read_is_refused_not_clobbered(self):
        d = self._dir()
        f = d / "t.txt"
        f.write_text("alpha beta")
        real_copymode = ae.shutil.copymode

        def _race(src, dst):
            # copymode(src=p, dst=tmp): SRC is the live target, and this runs
            # inside the one window the precondition can actually observe.
            Path(src).write_text("third-party")
            return real_copymode(src, dst)

        with unittest.mock.patch.object(ae.shutil, "copymode", side_effect=_race):
            rc, out, err = self._main([str(f), "--old", "beta", "--new", "gamma"])
        self.assertEqual(rc, 2, "a changed target must refuse, not overwrite")
        self.assertEqual(f.read_text(), "third-party", "the concurrent update must survive")
        self.assertEqual(out, "")

    def test_the_unchanged_case_still_succeeds(self):
        """Negative control: the precondition must not refuse an ordinary edit."""
        d = self._dir()
        f = d / "t.txt"
        f.write_text("alpha beta")
        rc, out, err = self._main([str(f), "--old", "beta", "--new", "gamma"])
        self.assertEqual(rc, 0)
        self.assertEqual(f.read_text(), "alpha gamma")

    def test_the_lock_is_held_across_the_replace_not_only_the_precondition(self):
        """The window the precondition CANNOT close: after the reread, at replace.

        qingyun-wu reproduced a clobber by injecting exactly there. A cooperating
        writer is now excluded for that whole span, and this proves it by trying
        to take the lock at the moment of replacement.
        """
        d = self._dir()
        f = d / "t.txt"
        f.write_text("alpha beta")
        seen = {}
        real_replace = ae.os.replace

        def _at_replace(src, dst):
            lock = Path(str(f) + ae.LOCK_SUFFIX)
            fd = ae.os.open(str(lock), ae.os.O_CREAT | ae.os.O_RDWR, 0o644)
            try:
                ae.fcntl.flock(fd, ae.fcntl.LOCK_EX | ae.fcntl.LOCK_NB)
                seen["held"] = False          # got in -> the window is OPEN
                ae.fcntl.flock(fd, ae.fcntl.LOCK_UN)
            except BlockingIOError:
                seen["held"] = True           # refused -> the window is CLOSED
            finally:
                ae.os.close(fd)
            return real_replace(src, dst)

        with unittest.mock.patch.object(ae.os, "replace", side_effect=_at_replace):
            rc, out, err = self._main([str(f), "--old", "beta", "--new", "gamma"])
        self.assertEqual(rc, 0)
        self.assertTrue(seen.get("held"),
                        "a cooperating writer got the lock DURING the replace — "
                        "the read-to-replace span is not actually locked")
        self.assertEqual(f.read_text(), "alpha gamma")

    def test_a_second_process_holding_the_lock_blocks_the_edit(self):
        """End-to-end: the lock is a real cross-process exclusion, not bookkeeping."""
        import subprocess
        import textwrap
        d = self._dir()
        f = d / "t.txt"
        f.write_text("alpha beta")
        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import fcntl, os, sys, time
                fd = os.open({str(f) + ".anchored-lock"!r}, os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX)
                sys.stdout.write("held"); sys.stdout.flush()
                time.sleep(1.5)
            """)], stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.read(4), "held")
        started = time.monotonic()
        rc, out, err = self._main([str(f), "--old", "beta", "--new", "gamma"])
        waited = time.monotonic() - started
        holder.wait()
        self.assertEqual(rc, 0)
        self.assertGreater(waited, 0.5,
                           "the edit did not wait for the lock — exclusion is not real")
        self.assertEqual(f.read_text(), "alpha gamma")

    def test_the_lock_does_not_leak_into_the_edited_content(self):
        """Negative control: the sidecar is beside the file, never inside it."""
        d = self._dir()
        f = d / "t.txt"
        f.write_text("alpha beta")
        rc, out, err = self._main([str(f), "--old", "beta", "--new", "gamma"])
        self.assertEqual(rc, 0)
        self.assertEqual(f.read_text(), "alpha gamma")
        self.assertNotIn(ae.LOCK_SUFFIX, f.read_text())
        self.assertTrue((d / ("t.txt" + ae.LOCK_SUFFIX)).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
