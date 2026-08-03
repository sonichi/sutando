#!/usr/bin/env python3
"""Regression: a divider that markup has MASKED must warn, not fail silently.

2026-08-03, measured on one host: a single unbalanced backtick opened an inline
span that never closed, so mask_markup() blanked everything downstream and the
`# Resolved` divider 1,971 lines later became unfindable. active_region() then
returned the whole file, the audit trail was served as live, and retired entries
re-surfaced as pending. Nothing errored; the file renders fine on GitHub.

The guard WARNS and leaves the return value alone on purpose — falling back to the
raw match would cut at a `# Resolved` inside a fenced example, hiding live
questions, which is the dangerous direction. Over-counting is noisy but safe.

Run: python3 tests/pending-questions-masked-divider-warns.test.py
"""
from __future__ import annotations
import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from pending_questions_md import active_region  # noqa: E402

TICK = chr(96)


def _run(text: str):
    err = io.StringIO()
    with redirect_stderr(err):
        out = active_region(text)
    return out, err.getvalue()


class TestMaskedDividerWarns(unittest.TestCase):
    def test_unbalanced_backtick_masking_the_divider_warns(self):
        # THE REAL SHAPE, and the detail that matters: a dangling backtick is
        # inert on its own — it only does damage by pairing with the NEXT backtick
        # further down the file, masking everything between the two. On the host
        # where this was found, those two ticks sat 1,971 lines apart with the
        # divider in the middle. A fixture without the closing tick does NOT
        # reproduce it, which is why this comment exists.
        text = (
            "## live question\n"
            f"a stray {TICK}tick{TICK} and one more {TICK} here\n"
            "\n"
            "# Resolved\n"
            "\n"
            f"## archived question mentioning {TICK}code{TICK}\n"
        )
        out, err = _run(text)
        self.assertIn("MASKED", err, "a masked divider must be announced, not swallowed")
        self.assertIn("sonichi/sutando#2557", err, "point the reader at the diagnosis")
        self.assertEqual(out, text, "must NOT cut — hiding live questions is the worse failure")

    def test_healthy_file_still_cuts_and_stays_quiet(self):
        text = "## live\nbody\n\n# Resolved\n\n## archived\n"
        out, err = _run(text)
        self.assertEqual(err, "", "a healthy file must not warn")
        self.assertNotIn("archived", out, "the divider must still cut")
        self.assertIn("live", out)

    def test_divider_inside_a_BOUNDED_fence_stays_quiet(self):
        # Masking is CORRECT here: the `# Resolved` is a documentation example, and
        # live text follows the closed fence. Warning would be a false alarm, and a
        # false alarm is how this warning gets ignored.
        fence = TICK * 3
        text = (
            "## live question\n"
            f"{fence}\n"
            "# Resolved\n"
            f"{fence}\n"
            "\n"
            "## another live question\n"
        )
        out, err = _run(text)
        self.assertEqual(err, "", "a bounded fenced example must not warn")
        self.assertEqual(out, text, "no real divider exists, so nothing is cut")

    def test_bullet_only_archive_also_warns(self):
        # REVIEW FIX (qingyun-wu + john-the-dev, #2558). The first version of the
        # discriminator tested `## ` headings ONLY, so a bullet-only archive under a
        # masked divider returned the whole file with an EMPTY stderr — retired
        # bullets served as live while the guard implied all-clear.
        #
        # This is the shape the module header itself calls out: "real
        # pending-questions.md carries 0 `## ` headings, only bullets". A guard that
        # covers one of two reader-recognized populations is worse than no guard,
        # because it reads as comprehensive.
        text = (
            f"intro stray {TICK}\n"
            "# Resolved\n"
            "\n"
            "- **[RETIRED, 2026-08-03]** archived bullet should not be live\n"
            f"closing {TICK}\n"
        )
        out, err = _run(text)
        self.assertIn("MASKED", err, "a bullet-only archive must warn too")
        self.assertEqual(out, text, "still warn-only — never cut")

    def test_bounded_fence_containing_a_BULLET_stays_quiet(self):
        # The control that keeps the widened predicate honest: widening to bullets
        # must not start crying wolf on a fenced example that happens to contain one.
        fence = TICK * 3
        text = (
            "## live question\n"
            f"{fence}\n"
            "# Resolved\n"
            "- **[EXAMPLE]** this is documentation, not an archive\n"
            f"{fence}\n"
            "\n"
            "## another live question\n"
        )
        out, err = _run(text)
        self.assertEqual(err, "", "a bounded fenced example must stay quiet")
        self.assertEqual(out, text)

    def test_quoted_banner_before_a_damaged_divider_reports_BOTH_not_just_the_first(self):
        # REVIEW FIX 2 (qingyun-wu, #2558). The warning used the FIRST raw divider
        # match, so a line-initial QUOTED banner — masked on purpose — was announced
        # as the damage while the real divider further down went unmentioned.
        #
        # A banner and a real divider can BOTH survive comment/fence masking and be
        # swallowed by the SAME runaway span, so nothing can single out "the real
        # one". Guessing sends the reader to harmless markup; listing every
        # candidate is honest and still actionable, since one unbalanced backtick
        # explains all of them.
        text = (
            f"intro: writers must never append below the {TICK}\n"
            f"# Resolved{TICK} heading. (quoted banner)\n"
            f"stray {TICK}\n"
            "\n"
            "# Resolved\n"
            "\n"
            "- **[RETIRED]** archived\n"
            f"end {TICK}\n"
        )
        out, err = _run(text)
        self.assertIn("line 2", err, "the quoted banner is a candidate and must be listed")
        self.assertIn("line 5", err, "the REAL divider must not be dropped in favour of the banner")
        self.assertEqual(out, text, "still warn-only")

    def test_label_comes_from_the_MATCH_not_a_hardcoded_string(self):
        # Same review: the label was hard-coded '# Resolved', so friction-detector's
        # DIVIDER_OR_DONE_RE reported a real `# Done` divider under a name that does
        # not appear anywhere in the file — sending the reader to search for a
        # heading that is not there.
        from pending_questions_md import DIVIDER_OR_DONE_RE
        text = (
            f"intro stray {TICK}\n"
            "# Done\n"
            "\n"
            "- **[RETIRED]** y\n"
            f"end {TICK}\n"
        )
        err = io.StringIO()
        with redirect_stderr(err):
            out = active_region(text, DIVIDER_OR_DONE_RE)
        self.assertIn("'# Done'", err.getvalue(), "label must be the matched text")
        self.assertNotIn("Resolved", err.getvalue(), "must not name a divider absent from the file")
        self.assertEqual(out, text)

    def test_no_divider_at_all_is_silent(self):
        text = "## only live questions\nbody\n"
        out, err = _run(text)
        self.assertEqual(err, "")
        self.assertEqual(out, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
