#!/usr/bin/env python3
"""Contract for check-dedup-targets.py.

`[deduped: X]` asserts "the full reply is in X". If X is `[no-send]` the
assertion is false and the bridge announces the failure INTO THE ROOM, naming an
internal task id the peer cannot resolve. Measured on this host: 68 such pairs
on disk all-time, 12 of which produced a DELIVERED room message.
"""

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "proactive-loop" / "scripts"
_s = importlib.util.spec_from_file_location("cdt", str(SCRIPTS / "check-dedup-targets.py"))
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
        # Taxonomy is the shared owner's, so this condition is named
        # identically here and in scripts/unanswered-tasks.py.
        ws = _ws({"a.txt": "[deduped: task-b]\n", "task-b.txt": "[no-send]\nnothing\n"})
        bad = cdt.check(ws, [ws / "results" / "a.txt"])
        self.assertEqual(len(bad), 1)
        self.assertIn("HOLDER-SKIPPED", bad[0][2])
        self.assertIn("task-b", bad[0][2])

    def test_a_dedup_onto_a_real_reply_is_clean(self):
        # Control: without this the checker could flag every dedup and the test
        # above would pass on a predicate that is always true.
        ws = _ws({"a.txt": "[deduped: task-b]\n", "task-b.txt": "Here is the actual reply.\n"})
        self.assertEqual(cdt.check(ws, [ws / "results" / "a.txt"]), [])

    def test_a_dedup_onto_a_missing_target_is_flagged(self):
        ws = _ws({"a.txt": "[deduped: task-nope]\n"})
        bad = cdt.check(ws, [ws / "results" / "a.txt"])
        self.assertEqual(len(bad), 1)
        self.assertIn("ORPHANED", bad[0][2])
        self.assertIn("task-nope", bad[0][2])

    def test_a_QUARANTINED_target_is_not_mistaken_for_a_delivered_one(self):
        """The regression this extraction exists for.

        `{id}.too-old.<epoch>` is a result that was quarantined — never
        delivered. This file resolved the target with a bare `{id}*` archive
        glob, which matches it, so a dedup pointing at an undelivered reply read
        as CLEAN. `unanswered-tasks.py` already required the `-` separator and
        documented why; only the guard had the loose glob.
        """
        ws = _ws({"a.txt": "[deduped: task-b]\n"},
                 archive={"task-b.too-old.1788000000.txt": "quarantined, never sent\n"})
        bad = cdt.check(ws, [ws / "results" / "a.txt"])
        self.assertEqual(len(bad), 1, "a quarantined target must not read as delivered")
        self.assertIn("task-b", bad[0][2])

    def test_a_delivered_archived_target_is_still_clean(self):
        """Control for the arm above: the `-` separator must not reject the real
        archive shape, or the fix would be 'flag everything' wearing a fix's name."""
        ws = _ws({"a.txt": "[deduped: task-b]\n"},
                 archive={"task-b-1788000001.txt": "the actual reply\n"})
        self.assertEqual(cdt.check(ws, [ws / "results" / "a.txt"]), [])

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

    def _main(self, ws, argv):
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cdt, "workspace", lambda: ws), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = cdt.main(argv)
        return rc, buf.getvalue() + err.getvalue()

    def test_explicit_files_clean_is_0_and_contradiction_is_1(self):
        ws = _ws({"a.txt": "[deduped: task-b]\n", "task-b.txt": "real reply\n"})
        rc, out = self._main(ws, [str(ws / "results" / "a.txt")])
        self.assertEqual(rc, 0)
        self.assertIn("no dedup resolves", out)
        (ws / "results" / "task-b.txt").write_text("[no-send]\n")
        rc, out = self._main(ws, [str(ws / "results" / "a.txt")])
        self.assertEqual(rc, 1)
        self.assertIn("CONTRADICTION a.txt -> [deduped: task-b]", out)
        self.assertIn("use [no-send] on BOTH", out)

    def test_no_argv_audits_results_and_archive(self):
        ws = _ws({"a.txt": "[deduped: task-b]\n"}, archive={"task-b-1.txt": "[no-send]\n"})
        rc, out = self._main(ws, [])
        self.assertEqual(rc, 1)
        self.assertIn("checked 2 result file(s)", out)

    def test_no_results_dir_is_cannot_answer(self):
        rc, out = self._main(Path(tempfile.mkdtemp()), [])
        self.assertEqual(rc, 2)
        self.assertIn("no results/ directory", out)


class Refusals(unittest.TestCase):
    def test_an_unreadable_file_is_skipped_not_fatal(self):
        ws = _ws({})
        self.assertEqual(cdt.check(ws, [ws / "results"]), [])   # a directory: OSError

    def test_without_the_policy_owner_the_checker_refuses(self):
        # No local fallback rule: the owner absent -> raise, never "clean".
        ws = _ws({"a.txt": "[deduped: task-b]\n", "task-b.txt": "reply\n"})
        with mock.patch.object(cdt, "REPO", Path(tempfile.mkdtemp())):
            with self.assertRaises(RuntimeError):
                cdt.check(ws, [ws / "results" / "a.txt"])

    def test_the_CLI_reports_a_missing_owner_as_2_not_1(self):
        """Exit 1 is reserved for a real finding by every checker in the loop, so
        a missing owner exiting 1 would read as 'dedups resolve to nothing'."""
        ws = _ws({"a.txt": "[deduped: task-b]\n"})
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cdt, "REPO", Path(tempfile.mkdtemp())), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = cdt.main([str(ws / "results" / "a.txt")])
        self.assertEqual(rc, 2)
        self.assertIn("not importable", buf.getvalue() + err.getvalue())


class MonthNestedArchive(unittest.TestCase):
    """Results archive into `archive/<YYYY-MM>/` (agent-api.py iterates `archive/*/`),
    but the audit globbed `archive/*.txt` and so audited almost nothing. Measured on a
    live workspace: 943 files seen, 3157 present, 2214 invisible."""

    def _nested(self):
        ws = Path(tempfile.mkdtemp())
        month = ws / "results" / "archive" / "2026-09"
        month.mkdir(parents=True)
        # a dedup onto a holder that delivers nothing — the audit's whole purpose
        (month / "task-a.txt").write_text("[deduped: task-b]\n")
        (month / "task-b.txt").write_text("[no-send]\n")
        return ws

    def test_the_audit_sees_a_month_nested_archive(self):
        # Drive main() with NO argv so the script's own enumeration runs. Building the
        # file list here instead would re-implement the line under test and pass either way.
        ws = self._nested()
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cdt, "workspace", lambda: ws), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = cdt.main([])
        out = buf.getvalue() + err.getvalue()
        self.assertEqual(1, rc, f"the nested dedup onto a [no-send] holder must be found: {out}")
        self.assertIn("task-a.txt", out)
        self.assertIn("checked 2 result file(s)", out)

    def test_a_flat_glob_would_have_seen_nothing(self):
        # Pins WHY this regressed: the old expression is not merely narrower, it is empty.
        ws = self._nested()
        flat = list((ws / "results" / "archive").glob("*.txt"))
        self.assertEqual([], flat, "the pre-fix glob returns nothing on the real layout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
