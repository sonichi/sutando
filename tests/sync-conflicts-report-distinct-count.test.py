#!/usr/bin/env python3
"""Regression test for #3133: headline counts paths, not saved snapshots."""
import contextlib
import importlib.util
import io
import pathlib
import sys
import types
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "sync-conflicts-report.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("sync_conflicts_report_distinct_count", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_module()


class TestDistinctConflictPathCount(unittest.TestCase):
    def test_headline_deduplicates_paths_but_detail_rows_stay_per_snapshot(self):
        same = pathlib.Path("memory/same.md")
        rows = [
            ("batch-a", same, (1, 1, 0)),
            ("batch-b", same, (2, 2, 0)),
            ("batch-c", pathlib.Path("memory/other.md"), (1, 1, 0)),
        ]
        original_unmerged = MOD.unmerged
        original_argv = sys.argv
        buf = io.StringIO()
        MOD.unmerged = lambda _workspace: (rows, None)
        sys.argv = [str(SCRIPT), "/unused"]
        try:
            with contextlib.redirect_stdout(buf):
                rc = MOD.main()
        finally:
            MOD.unmerged = original_unmerged
            sys.argv = original_argv

        output = buf.getvalue().splitlines()
        self.assertEqual(rc, 1)
        self.assertEqual(
            output[0],
            "sync-conflicts: 2 file(s) hold peer content not in the live copy",
        )
        self.assertEqual(sum("memory/same.md" in line for line in output[1:]), 2)
        self.assertEqual(sum("memory/other.md" in line for line in output[1:]), 1)


if __name__ == "__main__":
    unittest.main()
