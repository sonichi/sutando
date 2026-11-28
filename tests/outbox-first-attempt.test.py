#!/usr/bin/env python3
"""The outbox's first-attempt stamp: one time per item, written once, kept, dropped on settle.

`note_first_attempt` stamps beside the item (an item may not exist yet: publish refuses a record
it did not write), keeps the first writer's time against a racing writer, and answers None for a
stamp it cannot read or write, never overwriting one it cannot read.

Run: python3 tests/outbox-first-attempt.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import outbox  # noqa: E402


class FirstAttempt(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="outbox-first-attempt-"))

    def stamp(self, item_id):
        return outbox._first_attempt_path(self.root, item_id)

    def test_stamped_once_and_kept(self):
        self.assertEqual(outbox.note_first_attempt(self.root, "item-1", now=100.0), 100.0)
        self.assertEqual(outbox.note_first_attempt(self.root, "item-1", now=200.0), 100.0)
        self.assertEqual(self.stamp("item-1").parent.name, ".first-attempts")
        self.assertIsNone(outbox.read_item(self.root, "item-1"), "no item record is created")

    def test_the_default_time_is_now(self):
        with mock.patch.object(outbox.time, "time", return_value=4321.0):
            self.assertEqual(outbox.note_first_attempt(self.root, "item-now"), 4321.0)

    def test_forget_drops_it_and_tolerates_absence_and_errors(self):
        outbox.note_first_attempt(self.root, "item-f", now=1.0)
        outbox.forget_first_attempt(self.root, "item-f")
        self.assertFalse(self.stamp("item-f").exists())
        outbox.forget_first_attempt(self.root, "item-f")
        self.stamp("item-dir").mkdir(parents=True)
        outbox.forget_first_attempt(self.root, "item-dir")      # an unlink error is swallowed
        self.assertEqual(outbox.note_first_attempt(self.root, "item-f", now=9.0), 9.0)

    def test_an_unreadable_stamp_is_no_time_and_is_never_overwritten(self):
        for item_id, text in (("bad-json", "{not json"), ("not-time", '{"at": "soon"}'),
                              ("not-finite", '{"at": Infinity}'), ("not-object", "[1, 2]")):
            p = self.stamp(item_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            self.assertIsNone(outbox.note_first_attempt(self.root, item_id, now=5.0), item_id)
            self.assertEqual(p.read_text(), text, item_id)

    def test_an_unwritable_root_is_no_time(self):
        root = self.root / "a-file"
        root.write_text("x")
        self.assertIsNone(outbox.note_first_attempt(root, "item-w", now=1.0))

    def test_a_racing_writer_keeps_its_stamp(self):
        real_link = os.link

        def lose_the_race(src, dst):
            Path(dst).write_text('{"at": 7.0}')
            raise FileExistsError(dst)
        with mock.patch.object(outbox.os, "link", side_effect=lose_the_race):
            self.assertEqual(outbox.note_first_attempt(self.root, "item-r", now=99.0), 7.0)
        self.assertEqual([p.name for p in self.stamp("item-r").parent.iterdir()], [self.stamp("item-r").name],
                         "the staging file is removed")
        self.assertIs(os.link, real_link)


if __name__ == "__main__":
    unittest.main()
