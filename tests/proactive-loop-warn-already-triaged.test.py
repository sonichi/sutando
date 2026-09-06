#!/usr/bin/env python3
"""Contract for warn-already-triaged.py, especially --claim.

--claim exists because the warn path structurally cannot see a free-form
assertion: "X is how this system behaves" carries no probe name, so nothing
pipes it in. That is the shape that reached the owner as a duplicate finding
on 2026-09-01, with the answer already filed since 2026-08-28.
"""

import importlib.util
import io
import os
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "proactive-loop" / "scripts"
_s = importlib.util.spec_from_file_location("wat", str(SCRIPTS / "warn-already-triaged.py"))
wat = importlib.util.module_from_spec(_s)
_s.loader.exec_module(wat)


def _files(*texts):
    d = Path(tempfile.mkdtemp())
    out = []
    for i, t in enumerate(texts):
        p = d / f"f{i}.md"
        p.write_text(t)
        out.append(p)
    return out


def _report(name, text, files):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        untriaged = wat.report(name, text, files)
    return untriaged, buf.getvalue()


class Tokens(unittest.TestCase):
    def test_an_empty_name_is_not_emitted_as_a_token(self):
        # "" matches every line: a claim has no probe name, so this is the
        # difference between a search and a rubber stamp.
        self.assertNotIn("", wat.tokens("", "some `thing.py` here"))

    def test_a_probe_name_is_still_the_first_token(self):
        self.assertEqual(wat.tokens("memory-index", "text")[0], "memory-index")


class SnakeCaseIdentifiers(unittest.TestCase):
    """A claim whose only distinctive noun is snake_case used to yield ZERO
    tokens, so the tool refused (exit 2) instead of searching. Most claims about
    this codebase name a snake_case symbol -- `dev_dag2_dspace`, `PARK_REASONS`,
    `notify_discord_dm` -- so the refusal covered the common case, not an edge.
    """

    def test_a_snake_case_identifier_is_extracted(self):
        self.assertIn("dev_dag2_dspace",
                      wat.tokens("", "the dev_dag2_dspace lane stopped writing"))

    def test_an_uppercase_constant_is_extracted(self):
        self.assertIn("PARK_REASONS",
                      wat.tokens("", "PARK_REASONS is referenced only by the reader"))

    def test_a_leading_underscore_identifier_is_extracted(self):
        self.assertIn("_quarantine_undelivered",
                      wat.tokens("", "_quarantine_undelivered drops its why argument"))

    def test_a_long_snake_case_identifier_is_extracted(self):
        # A component cap made the tokenizer drop the LONGEST identifiers -- the
        # most distinctive ones -- so the refusal survived the fix it motivated.
        self.assertIn(
            "this_is_a_long_snake_case_identifier",
            wat.tokens("", "this_is_a_long_snake_case_identifier failed"))

    def test_a_long_hyphenated_identifier_is_not_truncated(self):
        # The hyphen alternative failed OPEN where snake_case failed closed: it
        # returned a prefix, so the search ran against a name nobody asked about.
        self.assertEqual(
            ["one-two-three-four-five-six"],
            wat.tokens("", "one-two-three-four-five-six failed"))

    def test_a_snake_case_claim_reaches_parked_material(self):
        # The end-to-end shape: before the fix this returned "no_tokens" and
        # nothing was searched, so parked material stayed invisible.
        f = _files("# notes\n## the dev_dag2_dspace lane is a lock flip, not a bug\nbody\n")
        verdict, out = _report("", "the dev_dag2_dspace lane stopped writing", f)
        self.assertEqual(verdict, "parked")

    def test_a_snake_case_claim_that_is_genuinely_absent_still_reports_untriaged(self):
        # Widening the tokenizer must not make every claim look parked; this is
        # the half that fails if the new alternative matches too much.
        f = _files("# notes\n## something else entirely\nbody\n")
        verdict, _ = _report("", "the dev_dag2_dspace lane stopped writing", f)
        self.assertEqual(verdict, "untriaged")

    def test_ordinary_prose_still_yields_no_searchable_tokens(self):
        # The refusal branch must survive: a sentence naming no identifier is
        # still unanswerable, and saying so is the whole point of exit 2.
        self.assertEqual(
            wat.tokens("", "The owner asked me to review the pull request and merge it"), [])


class PullRequestNumbers(unittest.TestCase):
    """A claim whose subject is a PR or issue number used to yield ZERO tokens,
    so the gate refused (exit 2) on the commonest subject in the parking files.
    """

    def test_a_pr_number_is_extracted(self):
        self.assertIn("#1893", wat.tokens("", "PR #1893 is open, conflicted and stale"))

    def test_it_finds_material_parked_under_the_number(self):
        f = _files("# notes\n## #1893 the Agent-SDK core — owner's call\nbody\n")
        verdict, out = _report("", "PR #1893 is open, conflicted and stale", f)
        self.assertEqual(verdict, "parked")

    def test_a_number_written_nowhere_still_reports_none_found(self):
        # Negative control: without it the arm above passes on a checker that
        # never says untriaged.
        f = _files("# notes\n## #4242 something else\n")
        verdict, out = _report("", "PR #1893 is open, conflicted and stale", f)
        self.assertEqual(verdict, "untriaged")

    def test_short_and_long_runs_are_not_numbers(self):
        # Bounded like the sibling recall-check.py (#\d{3,5}) so a price or a
        # long digit run is not searched as a PR.
        self.assertEqual(wat.tokens("", "it costs #12 and ref #123456"), [])

    def test_a_bare_number_without_the_hash_is_not_a_token(self):
        self.assertEqual(wat.tokens("", "a bare number 1893 with no hash"), [])


class ClaimFindsParkedMaterial(unittest.TestCase):
    def test_a_heading_hit_reports_candidates_and_is_not_untriaged(self):
        f = _files("# notes\n## the `widget-cache.py` decision\nbody\n")
        verdict, out = _report("", "the `widget-cache.py` needs rework", f)
        self.assertEqual(verdict, "parked")
        self.assertIn("CANDIDATES", out)

    def test_a_body_only_hit_still_refuses_to_say_untriaged(self):
        # "No heading" is not "nothing written" — the material is usually parked
        # in a body under a neighbouring heading.
        f = _files("# notes\n## unrelated\nwe measured `widget-cache.py` already\n")
        verdict, out = _report("", "the `widget-cache.py` needs rework", f)
        self.assertEqual(verdict, "parked")
        self.assertIn("NO HEADING", out)

    def test_genuinely_absent_material_reports_none_found(self):
        # Negative control: without this, every assertion above passes on a
        # checker that never says "untriaged".
        f = _files("# notes\n## something else entirely\nnothing relevant\n")
        verdict, out = _report("", "the `flux-capacitor.py` needs a `jigawatt-threshold`", f)
        self.assertEqual(verdict, "untriaged")
        self.assertIn("NONE FOUND", out)

    def test_the_verdict_depends_on_the_file_CONTENTS(self):
        # The mutation that matters: redact the subject and the verdict must flip.
        # A partial redaction (one file of two) leaves it firing.
        present = _files("## the `widget-cache.py` decision\n")
        absent = _files("## nothing to do with it\n")
        self.assertEqual(_report("", "about `widget-cache.py`", present)[0], "parked")
        self.assertEqual(_report("", "about `widget-cache.py`", absent)[0], "untriaged")


class Refusals(unittest.TestCase):
    def test_main_refuses_when_the_parking_files_cannot_be_found(self):
        # report() cannot tell "searched, found nothing" from "searched nothing":
        # both are an empty hit list, so the guard belongs in main().
        import sys
        from unittest.mock import patch
        buf = io.StringIO()
        with patch.object(wat, "parking_files", lambda: []), \
                patch.object(sys, "argv", ["wat", "--claim", "anything at all"]), \
                contextlib.redirect_stdout(buf):
            rc = wat.main()
        self.assertEqual(rc, 2)
        self.assertIn("PARKING FILES NOT FOUND", buf.getvalue())

    def test_a_claim_with_no_searchable_nouns_cannot_answer(self):
        # An ordinary sentence yields no tokens, so both search loops never run and
        # "no hits" is produced by construction: that must be cannot-answer.
        import sys
        from unittest.mock import patch
        claim = "the live checkout is 71 commits behind origin and merged fixes are not running"
        self.assertEqual(wat.tokens("", claim), [], "premise: this claim yields no tokens")
        f = _files("## the `widget-cache.py` decision\n")
        self.assertEqual(_report("", claim, f)[0], "no_tokens")
        buf = io.StringIO()
        with patch.object(wat, "parking_files", lambda: f), \
                patch.object(sys, "argv", ["wat", "--claim", claim]), \
                contextlib.redirect_stdout(buf):
            rc = wat.main()
        self.assertEqual(rc, 2, "a claim that searched nothing must be cannot-answer, not clear")
        self.assertIn("NO NOUNS", buf.getvalue())

    def test_a_claim_WITH_nouns_can_still_come_back_clear(self):
        # Control for the above: the fix must not turn every verdict into 2.
        import sys
        from unittest.mock import patch
        f = _files("## unrelated heading\n")
        with patch.object(wat, "parking_files", lambda: f), \
                patch.object(sys, "argv", ["wat", "--claim", "the `zzz-absent-probe.py` misfires"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wat.main(), 0)

    def test_an_empty_claim_is_refused_rather_than_passed(self):
        import sys
        from unittest.mock import patch
        buf = io.StringIO()
        with patch.object(sys, "argv", ["wat", "--claim", "   "]), \
                contextlib.redirect_stdout(buf):
            rc = wat.main()
        self.assertEqual(rc, 2)


class WarnPath(unittest.TestCase):
    """The stdin path: health-check warns piped in, one verdict per warn."""

    WARNS = ("\u26a0 widget-cache warn `widget-cache.py` is stale\n"
             "\u267b other-probe stale nothing named here at all\n")

    def _main(self, stdin, files):
        import sys
        from unittest.mock import patch
        buf = io.StringIO()
        with patch.object(wat, "parking_files", lambda: files), \
                patch.object(sys, "argv", ["wat"]), \
                patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(buf):
            rc = wat.main()
        return rc, buf.getvalue()

    def test_no_warns_on_stdin_is_cannot_answer(self):
        rc, out = self._main("all green\n", _files("## x\n"))
        self.assertEqual(rc, 2)
        self.assertIn("no warns on stdin", out)

    def test_missing_parking_files_is_cannot_answer(self):
        rc, out = self._main(self.WARNS, [])
        self.assertEqual(rc, 2)
        self.assertIn("PARKING FILES NOT FOUND", out)

    def test_each_warn_gets_a_verdict_and_the_summary_names_the_untriaged(self):
        rc, out = self._main(self.WARNS, _files("## the `widget-cache.py` decision\n"))
        self.assertEqual(rc, 0)
        self.assertIn("searching 1 parking file(s) for 2 warn(s)", out)
        self.assertIn("1 with candidate parkings, 1 with none: other-probe", out)



class ParkingFiles(unittest.TestCase):
    def test_resolves_through_the_repo_resolver_and_returns_only_existing_files(self):
        files = wat.parking_files()
        self.assertIsInstance(files, list)
        self.assertTrue(all(p.exists() for p in files))


class MemoryIsPartOfTheCorpus(unittest.TestCase):
    """A settled decision is written to core memory, not to a file that waits on
    a human. Omitting memory reported those warns as untriaged — the one verdict
    that invites re-deriving a decision already taken."""

    def _corpus(self, **files):
        d = Path(tempfile.mkdtemp())
        mem = d / "memory"
        mem.mkdir()
        for n, t in files.items():
            (mem / n).write_text(t)
        return d, mem

    def test_memory_files_join_the_parking_corpus(self):
        d, mem = self._corpus(**{"a-decision.md": "## zzz-probe is expected\n"})
        with mock.patch.dict(os.environ, {"SUTANDO_MEMORY_DIR": str(mem)}):
            names = [p.name for p in wat.parking_files()]
        self.assertIn("a-decision.md", names)

    def test_a_warn_parked_only_in_memory_is_not_reported_untriaged(self):
        d, mem = self._corpus(**{"a-decision.md": "## zzz-probe is expected here\n"})
        files = sorted(mem.glob("*.md"))
        verdict, out = _report("zzz-probe", "condition persists", files)
        self.assertEqual(verdict, "parked")
        self.assertNotIn("NONE FOUND", out)

    def test_a_memory_hit_is_labelled_so_it_is_not_read_as_a_pending_question(self):
        # A pending question waits on a human; a memory records a decision
        # already taken. Same word, opposite next action.
        d, mem = self._corpus(**{"a-decision.md": "## zzz-probe is expected here\n"})
        _, out = _report("zzz-probe", "condition persists", sorted(mem.glob("*.md")))
        self.assertIn("memory/a-decision.md", out)

    def test_a_non_memory_file_is_not_labelled_as_memory(self):
        (f,) = _files("## zzz-probe is expected here\n")
        _, out = _report("zzz-probe", "condition persists", [f])
        self.assertNotIn("memory/", out)

    def test_absent_memory_dir_degrades_to_the_per_host_files(self):
        # Must not raise on a checkout that has no memory dir.
        with mock.patch.dict(os.environ, {"SUTANDO_MEMORY_DIR": "/nonexistent-corpus-xyz"}):
            files = wat.parking_files()
        self.assertTrue(all(p.exists() for p in files))

    def test_genuinely_absent_material_still_reports_untriaged_with_memory_present(self):
        # Green-on-purpose: widening the corpus must not make everything parked.
        d, mem = self._corpus(**{"a-decision.md": "## something entirely else\n"})
        verdict, out = _report("zzz-probe", "condition persists", sorted(mem.glob("*.md")))
        self.assertEqual(verdict, "untriaged")
        self.assertIn("NONE FOUND", out)

    def test_each_file_is_read_once_however_many_tokens_are_searched(self):
        (f,) = _files("## nothing matching here\n")
        wat._LINES.clear()
        reads = []
        real = Path.read_text
        def counting(self_, *a, **k):
            reads.append(str(self_))
            return real(self_, *a, **k)
        with mock.patch.object(Path, "read_text", counting):
            _report("zzz-probe", "a `token.py` and another-token and a third_token", [f])
        self.assertEqual(reads.count(str(f)), 1, f"read {reads.count(str(f))}x")



if __name__ == "__main__":
    unittest.main(verbosity=2)
