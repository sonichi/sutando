#!/usr/bin/env python3
"""runtime-health persists two files other processes poll; `open(path, "w")`
truncates before it writes, so a reader landing in that window sees an empty
file. That is the #3156 shape: a zero-length read of shared state is not
"absent", it is mid-write, and a consumer cannot tell the two apart.

The module already had the correct pattern in `_write_station_cache` (tmp +
os.replace) and did not use it for `runtime-health.json` / `core-verdict.json`.

Run: python3 tests/runtime-health-atomic-writes.test.py
"""
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("rh", REPO / "src" / "runtime-health.py")
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)

SRC = (REPO / "src" / "runtime-health.py").read_text(encoding="utf-8")


class AtomicSharedStateWrites(unittest.TestCase):
    def test_writer_never_exposes_a_truncated_destination(self):
        """The production writer, not a recipe copied into this test."""
        d = pathlib.Path(tempfile.mkdtemp())
        dest = d / "core-verdict.json"
        dest.write_text(json.dumps({"health": "ok", "severity": 0}))
        before = dest.read_text()

        rh._write_json_atomic(str(dest), {"health": "degraded", "severity": 2})

        # The destination is either the old content or the new one -- never empty.
        self.assertNotEqual(dest.read_text(), "")
        self.assertEqual(json.loads(dest.read_text())["health"], "degraded")
        self.assertNotEqual(before, dest.read_text())

    def test_no_temp_file_is_left_behind(self):
        d = pathlib.Path(tempfile.mkdtemp())
        dest = d / "runtime-health.json"
        rh._write_json_atomic(str(dest), {"health": "ok"})
        self.assertEqual(
            [p.name for p in d.iterdir()], ["runtime-health.json"],
            "the temp file must be renamed onto the destination, not left beside it")

    def test_the_two_shared_files_do_not_take_a_truncating_write(self):
        """Guards the regression rather than the helper: a future edit that
        inlines `open(..., "w")` for either path reintroduces the window."""
        for name in ("runtime-health.json", "core-verdict.json"):
            for line in SRC.splitlines():
                if name in line and 'open(' in line and '"w"' in line:
                    self.fail(f"{name} is written with a truncating open(): {line.strip()}")

    def test_staging_names_are_unique_per_writer(self):
        """A fixed `<path>.tmp` is itself shared state: two concurrent callers
        truncate the same staging inode and race os.replace(), publishing
        interleaved bytes or raising ENOENT once the path has moved. Recorded
        deterministically rather than by timing, so it cannot flake."""
        d = pathlib.Path(tempfile.mkdtemp())
        dest = d / "core-verdict.json"
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(src)
            return real_replace(src, dst)

        os.replace = spy
        try:
            for i in range(5):
                rh._write_json_atomic(str(dest), {"n": i})
        finally:
            os.replace = real_replace

        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5,
                         f"staging names must differ per writer, got {sorted(set(seen))}")
        self.assertEqual(json.loads(dest.read_text())["n"], 4)

    def test_a_failed_replace_leaves_no_staging_file(self):
        """Cleanup on every failure path -- otherwise a raising replace strands
        a temp file beside the destination on each attempt."""
        d = pathlib.Path(tempfile.mkdtemp())
        dest = d / "runtime-health.json"
        real_replace = os.replace

        def boom(src, dst):
            raise OSError("injected replace failure")

        os.replace = boom
        try:
            with self.assertRaises(OSError):
                rh._write_json_atomic(str(dest), {"health": "ok"})
        finally:
            os.replace = real_replace

        self.assertEqual(list(d.iterdir()), [],
                         f"staging file left behind: {[x.name for x in d.iterdir()]}")

    def test_a_failing_cleanup_does_not_mask_the_original_error(self):
        """If unlink ALSO fails, the caller must still see the write's error --
        not the cleanup's. Swallowing the unlink is only correct because the
        original exception is re-raised; a bare `raise` inside the inner except
        would surface the wrong one."""
        d = pathlib.Path(tempfile.mkdtemp())
        dest = d / "core-verdict.json"
        real_replace, real_unlink = os.replace, os.unlink

        def boom_replace(src, dst):
            raise OSError("ORIGINAL: replace failed")

        def boom_unlink(p):
            raise OSError("CLEANUP: unlink failed")

        os.replace, os.unlink = boom_replace, boom_unlink
        try:
            with self.assertRaises(OSError) as ctx:
                rh._write_json_atomic(str(dest), {"health": "ok"})
        finally:
            os.replace, os.unlink = real_replace, real_unlink

        self.assertIn("ORIGINAL", str(ctx.exception),
                      f"cleanup error masked the write error: {ctx.exception}")
        self.assertNotIn("CLEANUP", str(ctx.exception))

    def test_control_the_truncating_shape_really_does_expose_an_empty_file(self):
        """Without this, the assertions above could pass for the wrong reason."""
        d = pathlib.Path(tempfile.mkdtemp())
        p = d / "x.json"
        p.write_text('{"health": "ok"}')
        fh = open(p, "w")           # the shape this test exists to forbid
        try:
            self.assertEqual(p.read_text(), "", "control failed: expected truncation")
        finally:
            fh.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
