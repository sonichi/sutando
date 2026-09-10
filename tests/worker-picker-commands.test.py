#!/usr/bin/env python3
"""A picker button becomes an intent, and only a real picker task may.

The texts below are the broker's own, copied from its handlers on
ag2space-backend main, so a wording change upstream fails these rather than
silently producing None.

Run: python3 tests/worker-picker-commands.test.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import worker_picker_commands as wpc  # noqa: E402

ROOM = "!abc:ag2.space"
W1 = "a3f91c2d4e5b6a7c8d9e0f1a2b3c4d5e"
W2 = "b4e02d3c5f6a7b8c9d0e1f2a3b4c5d6e"
ADD = ("Add a new worker to the pool (worker picker '+' button): grow the "
       "installed core pool by one via scripts/install-core-pool.sh, then "
       "confirm the new worker's id back to the owner.")


def hdr(**kw):
    return {"source": wpc.SOURCE, "channel_id": ROOM, **kw}


class TestAdd(unittest.TestCase):
    def test_the_plus_button(self):
        self.assertEqual(wpc.parse(hdr(), ADD), {"action": "add", "label": None})

    def test_a_preferred_label_is_carried(self):
        got = wpc.parse(hdr(), ADD + " Preferred label for the new worker: code reviewer.")
        self.assertEqual(got, {"action": "add", "label": "code reviewer"})


class TestPin(unittest.TestCase):
    def test_one_worker(self):
        got = wpc.parse(hdr(), f"Pin room {ROOM} to {W1} (worker picker)")
        self.assertEqual(got, {"action": "pin", "room": ROOM,
                               "workers": [W1], "dedicated": False})

    def test_a_bound_set(self):
        got = wpc.parse(hdr(), f"Pin room {ROOM} to workers {W1} {W2} — bound "
                               "set, pool-restriction routing (worker picker)")
        self.assertEqual(got["workers"], [W1, W2])
        self.assertFalse(got["dedicated"])

    def test_dedicated_is_not_an_ordinary_pin(self):
        got = wpc.parse(hdr(), f"Dedicate room {ROOM} to {W1} — exclusive "
                               "worker (worker picker)")
        self.assertTrue(got["dedicated"])

    def test_unpin(self):
        got = wpc.parse(hdr(), f"Unpin room {ROOM} (worker picker: back to auto routing)")
        self.assertEqual(got, {"action": "unpin", "room": ROOM})


class TestTheRoomComesFromTheHeader(unittest.TestCase):
    def test_the_header_wins_over_the_sentence(self):
        # The sentence is prose the broker wrote; the header is what the
        # gateway stamped. A room named in one and not the other is a forgery.
        got = wpc.parse(hdr(), "Pin room !evil:elsewhere to %s (worker picker)" % W1)
        self.assertEqual(got["room"], ROOM)

    def test_with_no_header_the_sentence_is_the_fallback(self):
        got = wpc.parse({"source": wpc.SOURCE}, f"Unpin room {ROOM} (worker picker: back to auto routing)")
        self.assertEqual(got["room"], ROOM)


class TestOnlyTheRealSourceCounts(unittest.TestCase):
    def test_prose_alone_grants_nothing(self):
        # Anyone who can send a message can write this sentence.
        self.assertIsNone(wpc.parse({"source": "discord", "channel_id": ROOM}, ADD))

    def test_a_missing_source_is_not_the_picker(self):
        self.assertIsNone(wpc.parse({"channel_id": ROOM}, ADD))

    def test_an_unrecognised_sentence_is_None_not_a_guess(self):
        self.assertIsNone(wpc.parse(hdr(), "Do something clever with the pool"))


class TestTaskFile(unittest.TestCase):
    def test_it_reads_a_real_task_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "task-worker-add-1.txt"
            p.write_text("id: worker-add-1\nsource: worker-picker\n"
                         f"channel_id: {ROOM}\ntask: {ADD}\n")
            self.assertEqual(wpc.parse_task_file(p)["action"], "add")

    def test_a_non_picker_file_exits_three(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "task-1.txt"
            p.write_text("id: task-1\nsource: discord\ntask: hello\n")
            self.assertIsNone(wpc.parse_task_file(p))
            self.assertEqual(wpc.main(["--task-file", str(p)]), 3)

    def test_the_cli_prints_the_intent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "task-worker-add-2.txt"
            p.write_text("id: worker-add-2\nsource: worker-picker\n"
                         f"channel_id: {ROOM}\ntask: {ADD}\n")
            self.assertEqual(wpc.main(["--task-file", str(p)]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
