#!/usr/bin/env python3
"""Contract for warn-already-triaged.py, especially --claim.

--claim exists because the warn path structurally cannot see a free-form
assertion: "X is how this system behaves" carries no probe name, so nothing
pipes it in. That is the shape that reached the owner as a duplicate finding
on 2026-09-01, with the answer already filed since 2026-08-28.
"""

import importlib.util
import io
import contextlib
import tempfile
import unittest
from pathlib import Path

_s = importlib.util.spec_from_file_location(
    "wat", str(Path(__file__).resolve().parent / "warn-already-triaged.py"))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
