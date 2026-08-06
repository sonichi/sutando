#!/usr/bin/env python3
"""A dedup may only be honoured when its holder actually answered.

`[deduped: task-X]` archives the asking task and delivers nothing, on the
premise that task-X carries the reply. When task-X's own delivery was empty,
that premise is false: the ask is archived against a delivery that never
happened, and every retry carrying the same marker is archived the same way.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from local_task_protocol import find_archived_result  # noqa: E402
from result_markers import dedup_holder_delivered, parse_markers  # noqa: E402

HOLDER = "task-22d83e59601f3a1fef"


class HolderDeliveredTest(unittest.TestCase):
    def test_empty_holder_result_is_not_a_delivery(self):
        self.assertFalse(dedup_holder_delivered(""))
        self.assertFalse(dedup_holder_delivered("   \n"))

    def test_unknown_holder_is_not_a_delivery(self):
        self.assertFalse(dedup_holder_delivered(None))

    def test_holder_that_itself_skipped_is_not_a_delivery(self):
        """Chained skips: the holder delivered nothing either."""
        for marker in ("[no-send]", "[REPLIED]", "[deduped: task-other]"):
            with self.subTest(marker=marker):
                self.assertFalse(dedup_holder_delivered(marker))

    def test_real_answer_is_a_delivery(self):
        self.assertTrue(dedup_holder_delivered("AG2Space is a chat workspace."))

    def test_answer_with_an_attachment_marker_is_a_delivery(self):
        self.assertTrue(dedup_holder_delivered("here you go\n[attach: /tmp/sutando-x.txt]"))


class ArchiveLookupTest(unittest.TestCase):
    def test_finds_the_archived_result(self):
        with tempfile.TemporaryDirectory() as td:
            arc = Path(td) / "archive"
            arc.mkdir()
            (arc / f"{HOLDER}-1785976425.txt").write_text("the answer")
            found = find_archived_result(Path(td), HOLDER)
            self.assertIsNotNone(found)
            self.assertEqual(found.read_text(), "the answer")

    def test_missing_archive_is_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(find_archived_result(Path(td), HOLDER))

    def test_newest_archive_wins(self):
        with tempfile.TemporaryDirectory() as td:
            arc = Path(td) / "archive"
            arc.mkdir()
            (arc / f"{HOLDER}-1785976425.txt").write_text("older")
            (arc / f"{HOLDER}-1785999999.txt").write_text("newer")
            self.assertEqual(find_archived_result(Path(td), HOLDER).read_text(), "newer")

    def test_unsafe_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(find_archived_result(Path(td), "../../etc/passwd"))


class DeadEndTest(unittest.TestCase):
    """The reported failure, end to end over the two helpers."""

    def test_dedup_against_an_empty_delivery_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            arc = Path(td) / "archive"
            arc.mkdir()
            # What the live failure left behind: holder archived at 0 bytes.
            (arc / f"{HOLDER}-1785976425.txt").write_text("")

            retry = "[deduped: %s]" % HOLDER
            skip = next(a for a in parse_markers(retry).actions if a.kind == "skip")
            self.assertEqual(skip.value, "deduped")
            self.assertEqual(skip.extra, HOLDER)

            archived = find_archived_result(Path(td), skip.extra)
            text = archived.read_text() if archived else None
            self.assertFalse(
                dedup_holder_delivered(text),
                "dedup honoured against a holder that delivered an empty body — "
                "the ask is archived and every retry is archived the same way",
            )

    def test_dedup_against_a_real_delivery_is_honoured(self):
        with tempfile.TemporaryDirectory() as td:
            arc = Path(td) / "archive"
            arc.mkdir()
            (arc / f"{HOLDER}-1785976425.txt").write_text("the real answer")
            archived = find_archived_result(Path(td), HOLDER)
            self.assertTrue(dedup_holder_delivered(archived.read_text()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
