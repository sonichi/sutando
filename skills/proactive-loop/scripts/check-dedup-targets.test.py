#!/usr/bin/env python3
"""Contract for check-dedup-targets.py.

`[deduped: X]` asserts "the full reply is in X". If X is `[no-send]` the
assertion is false and the bridge announces the failure INTO THE ROOM, naming an
internal task id the peer cannot resolve. Measured on this host: 68 such pairs
on disk all-time, 12 of which produced a DELIVERED room message.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

_s = importlib.util.spec_from_file_location(
    "cdt", str(Path(__file__).resolve().parent / "check-dedup-targets.py"))
cdt = importlib.util.module_from_spec(_s)
_s.loader.exec_module(cdt)


def _ws(results: dict, archive: dict = None):
    ws = Path(tempfile.mkdtemp())
    (ws / "results" / "archive").mkdir(parents=True)
    for n, b in results.items():
        (ws / "results" / n).write_text(b)
    for n, b in (archive or {}).items():
        (ws / "results" / "archive" / n).write_text(b)
    return ws


class Contradictions(unittest.TestCase):
    def test_a_dedup_onto_a_no_send_target_is_flagged(self):
        ws = _ws({"a.txt": "[deduped: task-b]\n", "task-b.txt": "[no-send]\nnothing\n"})
        bad = cdt.check(ws, [ws / "results" / "a.txt"])
        self.assertEqual(len(bad), 1)
        self.assertIn("delivered nothing", bad[0][2])
        self.assertIn("[no-send]", bad[0][2])   # quoted from the target, for the reader

    def test_a_dedup_onto_a_real_reply_is_clean(self):
        # Control: without this the checker could flag every dedup and the test
        # above would pass on a predicate that is always true.
        ws = _ws({"a.txt": "[deduped: task-b]\n", "task-b.txt": "Here is the actual reply.\n"})
        self.assertEqual(cdt.check(ws, [ws / "results" / "a.txt"]), [])

    def test_a_dedup_onto_a_missing_target_is_flagged(self):
        ws = _ws({"a.txt": "[deduped: task-nope]\n"})
        bad = cdt.check(ws, [ws / "results" / "a.txt"])
        self.assertEqual(len(bad), 1)
        self.assertIn("does not exist", bad[0][2])

    def test_the_target_is_resolved_from_the_archive_too(self):
        # Results are archived on delivery, so a same-pass check would otherwise
        # report every already-delivered target as missing.
        ws = _ws({"a.txt": "[deduped: task-b]\n"},
                 archive={"task-b-1788000000.txt": "the real reply\n"})
        self.assertEqual(cdt.check(ws, [ws / "results" / "a.txt"]), [])

    def test_a_file_with_no_dedup_marker_is_ignored(self):
        ws = _ws({"a.txt": "[no-send]\nplain\n"})
        self.assertEqual(cdt.check(ws, [ws / "results" / "a.txt"]), [])

    def test_the_marker_must_be_at_the_start_of_a_line(self):
        # Prose quoting the marker is not a marker; matching anywhere would flag
        # every write-up that discusses this defect, including this repo's own.
        ws = _ws({"a.txt": "I explained that [deduped: task-b] means the reply is elsewhere.\n"})
        self.assertEqual(cdt.check(ws, [ws / "results" / "a.txt"]), [])



class Chains(unittest.TestCase):
    """`[deduped: A]` where A is itself `[deduped: B]` resolves to no reply.

    Found by @yixuan-ag2 against their own tree (2 chains vs 3 plain [no-send]).
    A checker that tests only for the [no-send] marker misses it entirely, and it
    fails the same silent way on the writing side.
    """

    def test_a_chain_ending_in_no_send_is_flagged(self):
        ws = _ws({"a.txt": "[deduped: task-b]\n",
                  "task-b.txt": "[deduped: task-c]\n",
                  "task-c.txt": "[no-send]\n"})
        bad = cdt.check(ws, [ws / "results" / "a.txt"])
        self.assertEqual(len(bad), 1)
        # bad[i] is (file, FIRST target, why); the reason names the NEXT hop, so
        # together they render the whole path: "task-b: chain via task-c: [no-send]".
        self.assertEqual(bad[0][1], "task-b")
        self.assertIn("chained holder", bad[0][2])

    def test_a_chain_is_flagged_EVEN_IF_it_ends_in_a_real_reply(self):
        # Matches production: [deduped:] is itself a skip action, so the bridge's
        # dedup_decision requeues a chained holder rather than walking the chain.
        ws = _ws({"a.txt": "[deduped: task-b]\n",
                  "task-b.txt": "[deduped: task-c]\n",
                  "task-c.txt": "the actual reply\n"})
        bad = cdt.check(ws, [ws / "results" / "a.txt"])
        self.assertEqual(len(bad), 1)
        self.assertIn("chained holder", bad[0][2])

    def test_a_chain_to_a_missing_target_is_flagged_at_the_first_hop(self):
        ws = _ws({"a.txt": "[deduped: task-b]\n", "task-b.txt": "[deduped: task-gone]\n"})
        bad = cdt.check(ws, [ws / "results" / "a.txt"])
        self.assertIn("chained holder", bad[0][2])

    def test_a_cycle_cannot_arise_because_the_chain_is_never_walked(self):
        # The cycle guard I wrote became unreachable once the walk was removed.
        # Keeping the case documents WHY there is no recursion to protect.
        ws = _ws({"a.txt": "[deduped: task-b]\n",
                  "task-b.txt": "[deduped: task-c]\n",
                  "task-c.txt": "[deduped: task-b]\n"})
        bad = cdt.check(ws, [ws / "results" / "a.txt"])
        self.assertEqual(len(bad), 1)
        self.assertIn("chained holder", bad[0][2])


class ExitCodes(unittest.TestCase):
    def test_cannot_answer_is_2_not_0(self):
        rc = cdt.main([str(Path(tempfile.mkdtemp()) / "nope.txt")])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
