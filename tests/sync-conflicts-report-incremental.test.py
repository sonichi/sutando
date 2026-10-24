#!/usr/bin/env python3
"""#3824: the incremental scan equals a full scan, and the cron runs it at most daily."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "sync-conflicts-report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("sync_conflicts_report_incr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_module()


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name) / "workspace"
        self.ws.mkdir()
        subprocess.run(["git", "init", "-q", str(self.ws)], check=True)
        self.conflicts = self.ws / ".git" / "sutando-sync-conflicts"
        self.batch = self.conflicts / "20260805T000000Z-origin_host_peer"

    def tearDown(self):
        self._tmp.cleanup()

    def _live(self, rel: str, text: str) -> Path:
        p = self.ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def _saved(self, rel: str, text: str, batch: "Path | None" = None) -> Path:
        p = (batch or self.batch) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def _main(self, *args):
        buf = io.StringIO()
        argv = sys.argv
        sys.argv = [str(SCRIPT), *args]
        try:
            with contextlib.redirect_stdout(buf):
                rc = MOD.main()
        finally:
            sys.argv = argv
        return rc, buf.getvalue()

    def _count_scans(self):
        calls = []
        real = MOD._scan_one

        def counting(*a):
            calls.append(a[1])
            return real(*a)
        MOD._scan_one = counting
        self.addCleanup(setattr, MOD, "_scan_one", real)
        return calls


class TestIncrementalEqualsFull(_Base):
    def _assert_same(self, step: str):
        incremental = self._main(str(self.ws))
        full = self._main(str(self.ws), "--full")
        self.assertEqual(incremental, full, f"incremental != full after: {step}")
        return incremental

    def test_equivalence_across_edits_adds_deletes_and_retires(self):
        self._live("memory/a.md", "base\n")
        self._saved("memory/a.md", "base\npeer fact a\n")
        self._live("memory/b.md", "keep\npeer fact b\n")
        self._saved("memory/b.md", "keep\npeer fact b\n")
        self._saved("memory/gone.md", "only copy\n")
        rc, out = self._assert_same("initial")
        self.assertEqual(rc, 1)
        self.assertIn("a.md", out)
        self.assertNotIn("b.md", out)

        self._live("memory/a.md", "base\npeer fact a\n")          # merged back
        rc, out = self._assert_same("live edit that merges")
        self.assertNotIn("a.md", out)

        self._live("memory/b.md", "keep\n")                       # retracted
        self.assertIn("b.md", self._assert_same("live edit that drops a line")[1])

        self._saved("notes/new.md", "fresh peer line\n")          # new copy, no live
        self._live("notes/new.md", "unrelated\n")
        self.assertIn("new.md", self._assert_same("added copy")[1])

        (self.ws / "memory" / "b.md").unlink()                    # live deleted
        self.assertIn("MISSING", self._assert_same("live deleted")[1])

        (self.ws / "memory" / "b.md").mkdir()                     # live now a dir
        self.assertIn("NOT A FILE", self._assert_same("live became a dir")[1])

        (self.batch / "memory" / "gone.md").unlink()              # saved deleted
        self.assertNotIn("gone.md", self._assert_same("saved deleted")[1])

        other = self.conflicts / "batch-b"
        self._saved("memory/a.md", "base\nsecond peer\n", other)  # added batch
        self._assert_same("added batch")

        self.assertEqual(self._main(str(self.ws), "--retire",
                                    f"{self.batch.name}/notes/new.md")[0], 0)
        self.assertNotIn("new.md", self._assert_same("retired")[1])

        shutil.rmtree(other)                                      # batch deleted
        self._assert_same("batch deleted")
        cache = json.loads((self.ws / "state" / MOD.CACHE_NAME).read_text())
        self.assertFalse(any(k.startswith("batch-b/") for k in cache["entries"]),
                         "a deleted copy stayed in the cache")

    def test_a_changed_live_file_is_rescanned_not_served_from_cache(self):
        live = self._live("memory/n.md", "base\nXXXX\n")
        self._saved("memory/n.md", "base\nfact\n")
        self.assertEqual(self._main(str(self.ws))[0], 1)
        st = live.stat()
        # Same size AND the old mtime restored: only the cache key can tell.
        live.write_text("base\nfact\n")
        os.utime(live, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(live.stat().st_size, st.st_size)
        rc, out = self._main(str(self.ws))
        self.assertEqual(rc, 0, "cache served a stale row for a changed file: " + out)

    def test_a_changed_saved_copy_is_rescanned(self):
        self._live("memory/n.md", "base\n")
        saved = self._saved("memory/n.md", "base\n")
        self.assertEqual(self._main(str(self.ws))[0], 0)
        saved.write_text("base\nnew peer line\n")
        self.assertEqual(self._main(str(self.ws))[0], 1)

    def test_unchanged_files_are_not_reread_and_one_edit_rescans_one(self):
        for i in range(5):
            self._live(f"memory/{i}.md", "base\n")
            self._saved(f"memory/{i}.md", f"base\npeer {i}\n")
        calls = self._count_scans()
        self._main(str(self.ws))
        self.assertEqual(len(calls), 5)
        calls.clear()
        self._main(str(self.ws))
        self.assertEqual(calls, [], "unchanged copies were re-read")
        self._live("memory/3.md", "base\npeer 3\n")
        self._main(str(self.ws))
        self.assertEqual([str(r) for r in calls], ["memory/3.md"])
        calls.clear()
        self._main(str(self.ws), "--full")
        self.assertEqual(len(calls), 5, "--full must ignore the cache")

    def test_a_cache_from_another_scanner_version_is_ignored(self):
        self._live("memory/n.md", "base\n")
        self._saved("memory/n.md", "base\npeer\n")
        self._main(str(self.ws))
        path = self.ws / "state" / MOD.CACHE_NAME
        data = json.loads(path.read_text())
        data["version"] = "older"
        for e in data["entries"].values():
            e["row"] = "clean"
        path.write_text(json.dumps(data))
        self.assertEqual(self._main(str(self.ws))[0], 1)


class TestDailyGate(_Base):
    def setUp(self):
        super().setUp()
        self._live("memory/n.md", "base\n")
        self._saved("memory/n.md", "base\npeer\n")
        self.scans = []
        real = MOD.unmerged

        def counting(ws, full=False):
            self.scans.append(full)
            return real(ws, full=full)
        MOD.unmerged = counting
        self.addCleanup(setattr, MOD, "unmerged", real)

    def test_second_daily_call_within_24h_does_not_run_and_force_does(self):
        self.assertEqual(self._main("--daily", str(self.ws))[0], 1)
        self.assertEqual(len(self.scans), 1)
        rc, out = self._main("--daily", str(self.ws))
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("sync-conflicts: skipped"), out)
        self.assertEqual(len(self.scans), 1, "a gated call still scanned")
        self.assertEqual(self._main("--daily", "--force", str(self.ws))[0], 1)
        self.assertEqual(len(self.scans), 2)
        self.assertEqual(self._main(str(self.ws))[0], 1, "a manual run must not be gated")
        self.assertEqual(len(self.scans), 3)

    def test_a_stamp_older_than_24h_runs(self):
        stamp = self.ws / "state" / MOD.LAST_RUN_NAME
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps({"ts": int(time.time()) - 25 * 3600}))
        self.assertEqual(self._main("--daily", str(self.ws))[0], 1)
        self.assertEqual(len(self.scans), 1)

    def test_a_failed_scan_leaves_no_stamp(self):
        missing = Path(self._tmp.name) / "nope"
        self.assertEqual(self._main("--daily", str(missing))[0], 2)
        self.assertFalse((missing / "state" / MOD.LAST_RUN_NAME).exists())


class TestTiming(_Base):
    def test_a_rerun_on_an_unchanged_tree_is_much_faster(self):
        body = "".join(f"live line {j} with some words in it\n" for j in range(1500))
        for i in range(40):
            self._live(f"notes/{i}.md", body)
            self._saved(f"notes/{i}.md",
                        "".join(f"peer only line {i} {j}\n" for j in range(150)))
        t0 = time.perf_counter()
        first = self._main(str(self.ws))
        t1 = time.perf_counter()
        second = self._main(str(self.ws))
        t2 = time.perf_counter()
        self.assertEqual(first, second)
        cold, warm = t1 - t0, t2 - t1
        print(f"\ncold {cold:.3f}s  warm {warm:.3f}s  ratio {cold / warm:.0f}x",
              file=sys.stderr)
        self.assertLess(warm * 5, cold, f"cold {cold:.3f}s warm {warm:.3f}s")


if __name__ == "__main__":
    unittest.main()
