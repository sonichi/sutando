#!/usr/bin/env python3
"""A quarantined body that declares a skip had nothing to deliver.

`results/undelivered/` preserves a body a transport refused. But a body whose
content is `[no-send]` was never going to be delivered, so quarantining it
produced an entry that can never drain — inflating a warn whose stated action is
"a human reads it and decides", with files where there is nothing to decide.

Measured on a live host: 36 entries, 10 of them `[no-send]`.

`check_orphaned_results` already applies this rule and deliberately KEEPS
`[deduped:]`, because that marker PROMISES delivery elsewhere — a stranded one
is a lost reply. This probe now matches, including that asymmetry.

Run: python3 tests/health-check-quarantine-skip-markers.test.py
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
        "hc_quarantine_markers", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


class QuarantineSkipMarkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quarantine-markers-"))
        self._env = dict(os.environ)
        (self.tmp / "results" / "undelivered").mkdir(parents=True)
        self.mod = _load(self.tmp)
        self.mod.WORKSPACE_DIR = self.tmp

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _quarantined(self, name: str, body: str, age_s: int = 3600) -> Path:
        p = self.tmp / "results" / "undelivered" / name
        p.write_text(body)
        old = time.time() - age_s
        os.utime(p, (old, old))
        return p

    def _run(self):
        return self.mod.check_proactive_quarantine()

    # The control first: without it, every assertion below passes on a probe
    # that reports nothing at all.
    def test_POSITIVE_CONTROL_a_real_body_is_still_reported(self) -> None:
        self._quarantined("proactive-1.txt", "The briefing you asked for.")
        v = self._run()
        self.assertEqual(v["status"], "warn", v)
        self.assertIn("1 proactive message(s)", v["detail"])

    def test_no_send_is_not_quarantine_backlog(self) -> None:
        self._quarantined("task-ws-1.txt", "[no-send]\nWorkstream grouping applied.")
        v = self._run()
        self.assertEqual(v["status"], "ok", v)

    def test_REPLIED_is_not_quarantine_backlog(self) -> None:
        self._quarantined("task-r-1.txt", "[REPLIED]\nalready sent via another path")
        v = self._run()
        self.assertEqual(v["status"], "ok", v)

    # The asymmetry is the point, not an oversight — mirror the sibling.
    def test_deduped_IS_still_reported(self) -> None:
        self._quarantined("task-d-1.txt", "[deduped: task-1787526347213]")
        v = self._run()
        self.assertEqual(v["status"], "warn", v)
        self.assertIn("1 proactive message(s)", v["detail"])

    def test_a_mixed_directory_counts_only_the_deliverable(self) -> None:
        self._quarantined("proactive-real.txt", "a real body")
        self._quarantined("task-ws-2.txt", "[no-send]\nbookkeeping")
        self._quarantined("task-ws-3.txt", "[no-send]")
        self._quarantined("task-d-2.txt", "[deduped: task-x]")
        v = self._run()
        self.assertEqual(v["status"], "warn", v)
        # real + deduped == 2; the two no-sends drop out
        self.assertIn("2 proactive message(s)", v["detail"])

    def test_an_unreadable_entry_is_judged_as_before(self) -> None:
        """Fail-safe: a body we cannot read must not be silently cleared."""
        p = self._quarantined("proactive-locked.txt", "content")
        os.chmod(p, 0o000)
        try:
            v = self._run()
            self.assertEqual(v["status"], "warn", v)
        finally:
            os.chmod(p, 0o644)


if __name__ == "__main__":
    unittest.main(verbosity=2)
