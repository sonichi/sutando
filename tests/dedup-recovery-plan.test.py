#!/usr/bin/env python3
"""Contract for src/dedup_recovery.plan_dedup_recovery, and adapter delegation.

One owner decides what happens to a `[deduped: <holder>]` result whose holder
never answered; adapters keep only their routing and notification.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from dedup_recovery import REPORT_TEMPLATE, plan_dedup_recovery  # noqa: E402

TID = "task-633325612fbde6e777"
HOLDER = "task-22d83e59601f3a1fef"
ORIG = f"id: {TID}\nsource: gateway\naccess_tier: owner\ntask: What is AG2Space?\n"
NEW = "task-newid00000001"

CONSUMERS = {
    "discord-bridge": REPO / "src" / "discord-bridge.py",
    "slack-bridge": REPO / "src" / "slack-bridge.py",
    "telegram-bridge": REPO / "src" / "telegram-bridge.py",
    "remote_gateway_bridge": (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"
                              / "remote_gateway_bridge.py"),
}


class _Space:
    def __init__(self, td: str):
        self.results = Path(td) / "results"
        self.tasks = Path(td) / "tasks"
        (self.results / "archive").mkdir(parents=True)
        self.tasks.mkdir(parents=True)

    def holder(self, body: str, month: bool = False):
        if month:
            d = self.results / "archive" / "2026-08"
            d.mkdir(exist_ok=True)
            (d / f"{HOLDER}.txt").write_text(body)
        else:
            (self.results / "archive" / f"{HOLDER}-1785976425.txt").write_text(body)

    def orig(self, text: str = ORIG):
        (self.tasks / f"{TID}.txt").write_text(text)

    def plan(self, holder_id=HOLDER):
        return plan_dedup_recovery(self.results, self.tasks, TID, holder_id, "C1", NEW)


class PlanTest(unittest.TestCase):
    def test_holder_answered_is_honoured(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _Space(td); sp.holder("the full answer"); sp.orig()
            self.assertEqual(sp.plan(), ("honour", None))
            self.assertFalse((sp.tasks / f"{NEW}.txt").exists(), "honour must not re-ask")

    def test_empty_holder_is_requeued_and_the_task_is_written(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _Space(td); sp.holder(""); sp.orig()
            action, payload = sp.plan()
            self.assertEqual((action, payload), ("requeue", NEW))
            body = (sp.tasks / f"{NEW}.txt").read_text()
            self.assertIn("What is AG2Space?", body, "re-ask lost the question")
            self.assertIn("delivered nothing", body, "re-ask does not say why")
            self.assertIn("dedup_requeue_count: 1", body, "loop guard missing")

    def test_month_partitioned_holder_is_found(self):
        """The bridges archive as archive/<YYYY-MM>/<id>.txt, not flat."""
        with tempfile.TemporaryDirectory() as td:
            sp = _Space(td); sp.holder("the full answer", month=True); sp.orig()
            self.assertEqual(sp.plan()[0], "honour")

    def test_second_failure_reports(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _Space(td); sp.holder(""); sp.orig(ORIG + "dedup_requeue_count: 1\n")
            action, payload = sp.plan()
            self.assertEqual(action, "report")
            self.assertEqual(payload, REPORT_TEMPLATE.format(holder=HOLDER))
            self.assertFalse((sp.tasks / f"{NEW}.txt").exists(), "reported but still looped")

    def test_unknown_holder_is_requeued(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _Space(td); sp.orig()
            self.assertEqual(sp.plan()[0], "requeue")

    def test_missing_holder_id_is_requeued(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _Space(td); sp.orig()
            self.assertEqual(sp.plan(holder_id=None)[0], "requeue")

    def test_no_original_task_reports_rather_than_re_asking_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _Space(td); sp.holder("")
            self.assertEqual(sp.plan()[0], "report")

    def test_unwritable_tasks_dir_falls_back_to_report(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            sp = _Space(td); sp.holder(""); sp.orig()
            os.chmod(sp.tasks, 0o500)
            try:
                if os.access(sp.tasks, os.W_OK):
                    self.skipTest("still writable (running as root?)")
                self.assertEqual(sp.plan()[0], "report")
            finally:
                os.chmod(sp.tasks, 0o700)

    def test_unsafe_holder_id_cannot_escape_the_archive(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _Space(td); sp.orig()
            self.assertEqual(sp.plan(holder_id="../../etc/passwd")[0], "requeue")


class ArchiveLocatorTest(unittest.TestCase):
    """Branches of find_archived_result the plan depends on."""

    def test_flat_id_txt_in_archive_root_is_found(self):
        from local_task_protocol import find_archived_result
        with tempfile.TemporaryDirectory() as td:
            arc = Path(td) / "archive"
            arc.mkdir()
            (arc / f"{HOLDER}.txt").write_text("legacy flat layout")
            found = find_archived_result(Path(td), HOLDER)
            self.assertIsNotNone(found)
            self.assertEqual(found.read_text(), "legacy flat layout")

    def test_archive_that_is_a_file_is_not_an_error(self):
        """A damaged archive path must read as 'no record', not raise."""
        from local_task_protocol import find_archived_result
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "archive").write_text("not a directory")
            self.assertIsNone(find_archived_result(Path(td), HOLDER))


class DelegationTest(unittest.TestCase):
    def test_every_consumer_uses_the_shared_plan(self):
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                self.assertIn(
                    "plan_dedup_recovery", path.read_text(),
                    f"{name}: honours [deduped:] without checking the holder "
                    f"delivered — delegate to dedup_recovery.plan_dedup_recovery",
                )

    def test_no_consumer_rebuilds_the_decision(self):
        """Adapters route; they must not re-derive requeue-vs-report."""
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                src = path.read_text()
                self.assertNotIn(
                    "dedup_decision(", src,
                    f"{name}: calls dedup_decision directly — the plan owns that",
                )

    def test_sparrow_bundle_matches_src(self):
        pkg = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "dedup_recovery.py"
        self.assertTrue(pkg.exists(), "dedup_recovery.py not bundled into ag2-sparrow")
        self.assertEqual(pkg.read_text(), (REPO / "src" / "dedup_recovery.py").read_text(),
                         "ag2-sparrow copy drifted — run tools/sync_from_src.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
