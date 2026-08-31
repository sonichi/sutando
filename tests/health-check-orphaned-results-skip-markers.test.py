#!/usr/bin/env python3
"""A result that declares a skip marker is not a stranded reply.

Run: python3 tests/health-check-orphaned-results-skip-markers.test.py
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(workspace: Path):
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"
    spec = importlib.util.spec_from_file_location(
        "hc_orphan_markers", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


class OrphanSkipMarkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="orphan-markers-"))
        self._env = dict(os.environ)
        (self.tmp / "results").mkdir(parents=True)
        (self.tmp / "tasks").mkdir(parents=True)
        self.mod = _load(self.tmp)
        self.mod.WORKSPACE_DIR = self.tmp

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _result(self, task_id: str, body: str, age_s: int = 3600) -> Path:
        """A result with NO matching task — the orphan precondition."""
        p = self.tmp / "results" / f"{task_id}.txt"
        p.write_text(body)
        old = time.time() - age_s
        os.utime(p, (old, old))
        return p

    def _run(self):
        return self.mod.check_orphaned_results()

    # --- the control, first: without it a pass proves only that nothing is
    #     ever reported.
    def test_a_plain_undelivered_result_is_still_an_orphan(self) -> None:
        self._result("task-plain-1", "Here is the answer you asked for.")
        r = self._run()
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("task-plain-1", r["detail"])

    # --- the fix ------------------------------------------------------------
    def test_no_send_is_not_an_orphan(self) -> None:
        self._result("task-nosend-1", "[no-send]\nWorkstream grouping: 15 assigned.")
        r = self._run()
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertNotIn("task-nosend-1", r["detail"])

    def test_replied_is_not_an_orphan(self) -> None:
        self._result("task-replied-1", "[REPLIED]\nalready sent via the other path")
        r = self._run()
        self.assertEqual(r["status"], "ok", r["detail"])

    # --- the deliberate NON-exemption ---------------------------------------
    def test_deduped_is_still_reported(self) -> None:
        """`[deduped:]` promises delivery elsewhere; a broken promise is a lost reply."""
        self._result("task-dedup-1", "[deduped: task-other-9]")
        r = self._run()
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("task-dedup-1", r["detail"])

    # --- the exemption must not swallow a real one beside it ----------------
    def test_a_skip_does_not_hide_a_real_orphan_in_the_same_scan(self) -> None:
        self._result("task-nosend-2", "[no-send]\nbookkeeping")
        self._result("task-plain-2", "a real undelivered reply")
        r = self._run()
        self.assertEqual(r["status"], "warn", r["detail"])
        self.assertIn("task-plain-2", r["detail"])
        self.assertNotIn("task-nosend-2", r["detail"])

    # --- unreadable must not silently clear ---------------------------------
    def test_unreadable_body_is_judged_as_before(self) -> None:
        """A body we cannot read is not evidence of a skip marker."""
        p = self._result("task-unreadable-1", "whatever")
        os.chmod(p, 0o000)
        try:
            r = self._run()
            self.assertEqual(r["status"], "warn", r["detail"])
        finally:
            os.chmod(p, 0o644)

    # --- the marker grammar is not re-declared here --------------------------
    def test_probe_does_not_declare_the_marker_grammar_itself(self) -> None:
        """CLAUDE.md: consumers must obtain grammar from result_markers."""
        src = (REPO / "src" / "health-check.py").read_text()
        i = src.index("def check_orphaned_results")
        seg = src[i:i + 8000]
        self.assertIn("parse_markers", seg)
        for literal in ('r"\\[no-send\\]"', "[REPLIED]\\", "re.compile(r\"^\\s*\\[no-send"):
            self.assertNotIn(literal, seg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
