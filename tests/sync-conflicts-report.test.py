#!/usr/bin/env python3
"""`scripts/sync-conflicts-report.py` must report ONLY real unmerged content."""
import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "sync-conflicts-report.py"


def _load_module() -> types.ModuleType:
    """Import the script as a module so the suite runs it IN-PROCESS."""
    spec = importlib.util.spec_from_file_location("sync_conflicts_report", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_module()


class _Result:
    """Same shape the subprocess harness returned, so the cases read the same."""

    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout


class TestSyncConflictsReport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name) / "workspace"
        self.ws.mkdir()
        subprocess.run(["git", "init", "-q", str(self.ws)], check=True)
        self.batch = self.ws / ".git" / "sutando-sync-conflicts" / "20260805T000000Z-origin_host_peer"

    def tearDown(self):
        self._tmp.cleanup()

    def _pair(self, name: str, live: str, saved: str) -> None:
        """Write the live copy and the preserved incoming copy of one file."""
        (self.ws / "memory").mkdir(exist_ok=True)
        (self.ws / "memory" / name).write_text(live)
        d = self.batch / "memory"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(saved)

    def _main(self, *args) -> "_Result":
        """Drive `main()` in-process with a patched argv, capturing stdout."""
        buf = io.StringIO()
        argv = sys.argv
        sys.argv = [str(SCRIPT), *args]
        try:
            with contextlib.redirect_stdout(buf):
                rc = MOD.main()
        finally:
            sys.argv = argv
        return _Result(rc, buf.getvalue())

    def _run(self):
        return self._main(str(self.ws))

    def test_reports_a_file_whose_peer_copy_holds_unmerged_content(self):
        """The case it exists for: the peer appended a section we never got."""
        self._pair("lost.md", "# a\nline1\n",
                   "# a\nline1\n" + "\n".join(f"new{i}" for i in range(10)) + "\n")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("lost.md", r.stdout)

    def test_a_peer_copy_that_is_a_strict_SUBSET_is_NOT_reported(self):
        """A peer copy that is a strict subset carries nothing the live copy lacks."""
        self._pair("subset.md",
                   "# b\n" + "\n".join(f"l{i}" for i in range(40)) + "\n",
                   "# b\nl0\nl1\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("subset.md", r.stdout)

    def test_a_SINGLE_real_added_line_IS_reported(self):
        """A SINGLE added line is reported."""
        self._pair("note.md", "keep\n", "keep\nimportant new fact\n")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("note.md", r.stdout)

    def test_a_pure_REFLOW_is_NOT_reported(self):
        """What the threshold was reaching for, done by content instead."""
        self._pair("wrap.md", "alpha beta gamma delta\n",
                   "alpha beta\ngamma delta\n")
        self._pair("indent.md", "x = 1\n", "    x = 1\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("wrap.md", r.stdout)
        self.assertNotIn("indent.md", r.stdout)

    def test_SWAPPED_SECTIONS_are_reported(self):
        """Swapped sections are reported."""
        self._pair("access.md",
                   "## Allowed\n- Alice\n## Denied\n- Bob\n",
                   "## Allowed\n- Bob\n## Denied\n- Alice\n")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)   # 1 == found unmerged content
        self.assertIn("access.md", r.stdout)

    def test_the_same_KEY_under_DIFFERENT_sections_is_reported(self):
        """The same KEY under DIFFERENT headings is reported."""
        self._pair("cfg.md",
                   "## Prod\nkey: A\n## Dev\nkey: B\n",
                   "## Prod\nkey: B\n## Dev\nkey: A\n")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)   # 1 == found unmerged content
        self.assertIn("cfg.md", r.stdout)

    def test_a_REFLOW_inside_one_section_is_still_NOT_reported(self):
        """The paired negative for the section fix."""
        self._pair("wrapsec.md", "## S\nalpha beta gamma\n", "## S\nalpha beta\ngamma\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("wrapsec.md", r.stdout)

    def test_a_RENAMED_heading_does_not_report_the_whole_body(self):
        """The documented fallback, pinned so it cannot silently change."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("rep", SCRIPT)
        rep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rep)
        got = rep._new_content("## Notes\n- x\n", "## Note\n- x\n")
        self.assertIn("## Notes", got, "the renamed heading itself should report")
        self.assertNotIn("- x", got,
                         "the body under a renamed heading must NOT be reported — "
                         "its text is present in the live copy")

    def test_the_report_SPLITS_absent_from_moved_between_sections(self):
        """Section-scoping necessarily reports a line that merely MOVED headings."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("rep", SCRIPT)
        rep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rep)

        # genuinely new text -> absent
        live = "## A\nkeep\n"
        extra = rep._new_content("## A\nkeep\nbrand new fact\n", live)
        absent, moved = rep._split_by_reason(extra, live)
        self.assertEqual(absent, ["brand new fact"])
        self.assertEqual(moved, [])

        # the swap -> reported, and classed as under-another-heading
        live2 = "## Allowed\n- Alice\n## Denied\n- Bob\n"
        extra2 = rep._new_content("## Allowed\n- Bob\n## Denied\n- Alice\n", live2)
        absent2, moved2 = rep._split_by_reason(extra2, live2)
        self.assertEqual(absent2, [], "a swap loses no TEXT, so nothing is absent")
        self.assertEqual(sorted(moved2), ["- Alice", "- Bob"],
                         "the swap must still be REPORTED, just correctly labelled")

    def test_the_split_is_the_OLD_global_rule_so_the_buckets_are_meaningful(self):
        """The second bucket is the global-haystack rule, which is what makes it
        interpretable: present text whose heading association changed."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("rep", SCRIPT)
        rep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rep)
        live = "## X\nthe peer fact\n## Y\nunrelated\n"
        # present verbatim, different heading -> moved, not absent
        extra = rep._new_content("## Y\nthe peer fact\n", live)
        absent, moved = rep._split_by_reason(extra, live)
        self.assertEqual(absent, [])
        self.assertEqual(moved, ["the peer fact"])

    def test_a_saved_line_that_is_a_PARTIAL_SUBSTRING_of_live_text_still_reports(self):
        """A saved line that is a PARTIAL SUBSTRING of live prose is reported."""
        self._pair("collide.md",
                   "The peer facts are documented elsewhere.\n",
                   "The peer fact\n")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("collide.md", r.stdout)

    def test_live_text_that_gained_TRAILING_PUNCTUATION_is_not_reported(self):
        """Live text that gained TRAILING PUNCTUATION is not reported."""
        self._pair("punct.md",
                   "x match the body. *(Restored 2026-08-04 from a peer copy)*\n",
                   "x match the body\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("punct.md", r.stdout)

    def test_boundary_matching_does_not_break_the_REFLOW_case(self):
        """The paired half: tightening the match must not start reporting re-wrapped or re-indented text, which is what the check exists to."""
        self._pair("wrap2.md", "alpha beta gamma delta\n", "alpha beta\ngamma delta\n")
        self._pair("indent2.md", "x = 1\n", "        x = 1\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("wrap2.md", r.stdout)
        self.assertNotIn("indent2.md", r.stdout)

    def test_mixed_batch_reports_only_the_lossy_file(self):
        """The discrimination, exercised in one run rather than three."""
        self._pair("lost.md", "# a\n", "# a\n" + "\n".join(f"n{i}" for i in range(9)) + "\n")
        self._pair("subset.md", "# b\n" + "\n".join(f"l{i}" for i in range(30)) + "\n", "# b\nl0\n")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("lost.md", r.stdout)
        self.assertNotIn("subset.md", r.stdout)

    def test_a_live_file_deleted_since_the_conflict_is_reported(self):
        """Peer content whose destination no longer exists is the worst case:
        nothing on disk holds it and no diff would surface it."""
        d = self.batch / "memory"
        d.mkdir(parents=True, exist_ok=True)
        (d / "gone.md").write_text("only copy\n")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("gone.md", r.stdout)
        self.assertIn("MISSING", r.stdout)

    def test_no_conflicts_directory_is_clean_not_an_error(self):
        """The overwhelmingly common case. Must be exit 0 and quiet, or a
        30-minute cron turns into a nag."""
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("no unmerged peer content", r.stdout)

    def test_no_argument_uses_the_canonical_resolver_NOT_the_caller_cwd(self):
        """Run from a directory that is not the workspace and not a git repo."""
        elsewhere = Path(self._tmp.name) / "not-the-workspace"
        elsewhere.mkdir()
        r = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(elsewhere),
                           capture_output=True, text=True)
        # Assert WHICH path it reported on, not the exit code: a correctly
        # resolved non-repo workspace legitimately exits 2.
        self.assertNotIn(str(elsewhere), r.stdout,
                         "reported on the caller's cwd instead of the resolver's answer")
        expected = subprocess.run(
            [sys.executable, "-c",
             "import sys,pathlib;sys.path.insert(0,%r);"
             "from workspace_default import resolve_workspace;"
             "print(resolve_workspace(migrate=False))" % str(REPO / "src")],
            capture_output=True, text=True, cwd=str(elsewhere)).stdout.strip()
        self.assertIn(expected, r.stdout,
                      "output does not name the workspace the canonical resolver returns")

    def _retire(self, *targets):
        return self._main(str(self.ws), "--retire", *targets)

    def _second_batch(self, name: str, text: str) -> None:
        d = self.ws / ".git" / "sutando-sync-conflicts" / "batch-b" / "memory"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)

    def test_retiring_one_batch_leaves_ANOTHER_batchs_copy_reporting(self):
        """Retiring one batch leaves another batch's copy reporting."""
        self._pair("note.md", "base\nmerged fact\n", "base\nmerged fact\n")
        self._second_batch("note.md", "base\nNEVER MERGED fact\n")
        self.assertEqual(self._run().returncode, 1, "batch-b should report")

        # `_pair` writes into the fixture's own batch, NOT "batch-a"; a guessed
        # name is refused, which is the fail-closed behaviour.
        r = self._retire(f"{self.batch.name}/memory/note.md")
        self.assertEqual(r.returncode, 0, r.stdout)

        after = self._run()
        self.assertEqual(after.returncode, 1,
                         "retiring batch-a silenced batch-b: " + after.stdout)
        self.assertIn("note.md", after.stdout)

    def test_an_AMBIGUOUS_selector_mutates_nothing(self):
        """Bare basename matches two copies -> refuse, and leave both reporting."""
        self._pair("note.md", "base\nmerged fact\n", "base\nmerged fact\n")
        self._second_batch("note.md", "base\nNEVER MERGED fact\n")
        r = self._retire("note.md")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("no preserved copy matches", r.stdout)
        self.assertIn("did you mean", r.stdout)          # names the qualified options
        self.assertEqual(self._run().returncode, 1, "an ambiguous retire mutated state")

    def test_a_BARE_retire_refuses_rather_than_retiring_everything(self):
        """A bare `--retire` must refuse; empty targets must not mean "match all"."""
        self._pair("note.md", "base\n", "base\nnever merged\n")
        self.assertEqual(self._run().returncode, 1)
        r = self._retire()
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("refusing to retire everything", r.stdout)
        self.assertEqual(self._run().returncode, 1, "bare --retire mutated state")

    def test_merged_then_RETRACTED_content_stays_silent_once_retired(self):
        """Merged-then-retracted content stays silent once retired."""
        self._pair("note.md", "base\npeer fact\n", "base\npeer fact\n")
        self.assertEqual(self._run().returncode, 0, "identical copies should be silent")
        self.assertEqual(self._retire(f"{self.batch.name}/memory/note.md").returncode, 0)
        # the operator now deliberately retracts the previously-merged line
        (self.ws / "memory" / "note.md").write_text("base\n")
        r = self._run()
        self.assertEqual(r.returncode, 0, "retracted content was resurrected: " + r.stdout)
        self.assertNotIn("note.md", r.stdout)

    def test_retire_still_lets_genuinely_NEVER_merged_content_report(self):
        """The paired control. A retire lifecycle that silenced everything would
        trade a permanent false positive for a permanent false negative."""
        self._pair("note.md", "base\npeer fact\n", "base\npeer fact\n")
        self._retire(f"{self.batch.name}/memory/note.md")
        self._pair("other.md", "x\n", "x\nnever merged fact\n")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("other.md", r.stdout)
        self.assertNotIn("note.md", r.stdout)

    def test_retiring_one_copy_does_not_silence_a_LATER_different_one(self):
        """The retire key carries a digest of the preserved copy, so the same
        path conflicting again with NEW peer content is a new entry."""
        self._pair("note.md", "base\npeer fact\n", "base\npeer fact\n")
        self._retire(f"{self.batch.name}/memory/note.md")
        later = self.ws / ".git" / "sutando-sync-conflicts" / "20260806T000000Z-peer" / "memory"
        later.mkdir(parents=True)
        (later / "note.md").write_text("base\na LATER peer fact\n")
        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("note.md", r.stdout)

    def test_retire_works_while_the_entry_is_SILENT_not_only_while_reporting(self):
        """Retire works while the entry is SILENT."""
        self._pair("note.md", "base\npeer fact\n", "base\npeer fact\n")
        out = self._retire(f"{self.batch.name}/memory/note.md").stdout
        self.assertIn("retired 1", out, out)
        self.assertNotIn("nothing matched", out)


    def test_retire_on_a_NON_REPO_propagates_the_validation_error(self):
        """`retire()` reuses `unmerged()`'s workspace validation, so a bad
        target must fail there rather than proceeding to mutate anything."""
        d = Path(self._tmp.name) / "notarepo"
        d.mkdir()
        r = self._main(str(d), "--retire", "anything")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("not a git repo", r.stdout)

    def test_retire_with_NO_conflicts_directory_is_clean_not_an_error(self):
        """A workspace that has never had a conflict: nothing to retire, and
        that is a normal state, not a failure."""
        r = self._main(str(self.ws), "--retire", "whatever")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("nothing matched", r.stdout)

    def test_retiring_the_SAME_copy_twice_is_idempotent(self):
        """Second retire of an already-retired copy must not re-record it, and must report honestly that nothing changed."""
        self._pair("note.md", "base\n", "base\nnew fact\n")
        target = f"{self.batch.name}/memory/note.md"

        first = self._main(str(self.ws), "--retire", target)
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertIn("retired 1", first.stdout)

        second = self._main(str(self.ws), "--retire", target)
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertIn("nothing matched", second.stdout)
        self.assertNotIn("retired 1", second.stdout)

        # and the entry stays retired — idempotence must not un-retire it
        self.assertEqual(self._run().returncode, 0, "the copy came back after a second retire")

    def test_a_non_repo_dir_INSIDE_a_repo_does_not_answer_about_the_ancestor(self):
        """`git rev-parse` searches ANCESTORS, and that is a silent false clean."""
        inner = self.ws / "never-initialised"
        inner.mkdir()
        r = self._main(str(inner))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("not a git repo", r.stdout)
        self.assertIn("ancestor", r.stdout)
        self.assertNotIn("no unmerged peer content", r.stdout)

    def test_a_missing_directory_exits_2_rather_than_claiming_clean(self):
        r = self._main(str(self.ws / "nope"))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("no such directory", r.stdout)

    def test_the_clean_line_NAMES_the_workspace_it_examined(self):
        """The clean line names the workspace it examined, so the verdict is checkable."""
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(str(self.ws), r.stdout)

    def test_a_non_git_directory_exits_2_rather_than_claiming_clean(self):
        """A tool that cannot look must not report 'nothing found' -- that is
        indistinguishable from a real clean result."""
        d = Path(self._tmp.name) / "notarepo"
        d.mkdir()
        r = self._main(str(d))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertNotIn("no unmerged peer content", r.stdout)

    def test_a_live_path_that_became_a_DIRECTORY_stays_in_the_report(self):
        """Non-comparable is not reconciled: dropping the row reads as 'nothing to do'."""
        d = self.batch / "memory"
        d.mkdir(parents=True, exist_ok=True)
        (d / "became-dir.md").write_text("# a\nline1\npeer-only\n")
        (self.ws / "memory").mkdir(exist_ok=True)
        (self.ws / "memory" / "became-dir.md").mkdir()      # live path is now a DIRECTORY

        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("became-dir.md", r.stdout)
        self.assertIn("NOT A FILE", r.stdout)
        self.assertNotIn("no unmerged peer content", r.stdout)

    def test_the_directory_disposition_is_DISTINCT_from_a_missing_live_file(self):
        """Two different states must not print the same line — `None` already means absent."""
        d = self.batch / "memory"
        d.mkdir(parents=True, exist_ok=True)
        (d / "gone.md").write_text("# a\npeer-only\n")
        (d / "isdir.md").write_text("# a\npeer-only\n")
        (self.ws / "memory").mkdir(exist_ok=True)
        (self.ws / "memory" / "isdir.md").mkdir()           # exists, not a file
        # gone.md is deliberately never created live -> the None branch

        r = self._run()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("live file MISSING", r.stdout)
        self.assertIn("NOT A FILE", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
