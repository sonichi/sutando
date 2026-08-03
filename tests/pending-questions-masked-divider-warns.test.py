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
FENCE = TICK * 3


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
        # UPDATED 2026-08-03: this fixture does NOT satisfy its own premise. Measured
        # span ranges on it: [43,56) covers lines 1-2 (the banner) and [88,133) covers
        # lines 3-8 (the real divider AND the swallowed `- **[RETIRED]**` bullet). They
        # are TWO different spans, so the banner's span swallows no question and is a
        # deliberate, balanced quote — exactly the false alarm qingyun-wu flagged at
        # 446de6c7. The anti-under-reporting guarantee this test was added for is
        # preserved below in the same-span fixture, where the premise actually holds.
        out, err = _run(text)
        self.assertIn("line 5", err, "the REAL divider must not be dropped in favour of the banner")
        self.assertNotIn("line 2", err,
                         "a balanced quote hidden by a DIFFERENT span swallows nothing "
                         "and must not be listed")
        self.assertEqual(out, text, "still warn-only")

    def test_ONE_span_hiding_TWO_dividers_lists_BOTH(self):
        # The original intent of the test above, under a fixture that actually meets
        # its premise: a single runaway span swallows a banner, a live question AND
        # the real divider. Nothing can single out "the real one" here, so listing
        # every candidate is honest and still actionable — one unbalanced backtick
        # explains all of them.
        text = (
            f"intro: never append below the {TICK}\n"
            "# Resolved\n"
            "## a live question this span swallowed\n"
            "# Resolved\n"
            "archived body\n"
            f"end {TICK}\n"
        )
        out, err = _run(text)
        self.assertIn("line 2", err, "first candidate in the SAME span must be listed")
        self.assertIn("line 4", err, "second candidate in the SAME span must be listed")
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

    def test_an_UNCLOSED_FENCE_also_warns(self):
        # SELF-REVIEW, not a reviewer finding. Three review rounds had all been
        # "an input the guard did not enumerate", so I enumerated the rest myself —
        # and the warning's own text ("an unclosed backtick span or code fence")
        # was over-claiming: an unclosed FENCE produced identical damage (whole file
        # served as live, retired entry visible) and the guard said NOTHING, because
        # the helper masked fences before looking.
        fence = TICK * 3
        text = (
            "## live question\n"
            "body\n"
            f"{fence}\n"
            "example content that never closes\n"
            "\n"
            "# Resolved\n"
            "\n"
            "- **[RETIRED]** archived question served as LIVE\n"
        )
        out, err = _run(text)
        self.assertIn("MASKED", err, "an unclosed fence is damage too")
        self.assertEqual(out, text, "still warn-only")

    def test_a_BOUNDED_fence_example_stays_quiet_after_the_widening(self):
        # The control that keeps the widening honest. An unclosed fence masks to END
        # OF DOCUMENT; a closed one stops at its closer. That signature is what
        # separates damage from a documentation example, so a quoted divider inside
        # a properly closed fence must remain silent.
        fence = TICK * 3
        text = f"## live question\n{fence}\n# Resolved\n{fence}\n\n## another live question\n"
        out, err = _run(text)
        self.assertEqual(err, "", "a closed fence is deliberate quoting, not damage")
        self.assertEqual(out, text)

    def test_a_CLOSED_fence_at_EOF_does_not_warn(self):
        # john-the-dev, review of 94850b40. The previous discriminator asked "is
        # everything after the divider masked?", which is TRUE for a closed fence
        # whose closer is the last line — masking blanks the closer too. The old
        # bounded-fence control passed only because it happened to add live text
        # after the closer, so the guard looked correct while false-alarming on the
        # simplest possible documentation snippet.
        fence = TICK * 3
        text = f"## live\n{fence}\n# Resolved\n{fence}\n"
        out, err = _run(text)
        self.assertEqual(err, "", "a CLOSED fence is deliberate, even with nothing after it")
        self.assertEqual(out, text)

    def test_a_BALANCED_multiline_span_quoting_a_divider_does_not_warn(self):
        # qingyun-wu, same round. An UNPAIRED backtick run masks nothing at all
        # (_mask_nonfence_spans only blanks a run that finds an equal-length
        # partner), so the real-world damage was never "unbalanced markup" — it was
        # a span pairing legitimately across ~1,900 lines. That is structurally
        # identical to this deliberate two-line quote; only what it SWALLOWS differs.
        text = (
            "## live\n"
            f"prose opens {TICK}\n"
            "# Resolved\n"
            f"{TICK} closing prose\n"
            "\n"
            "## still live\n"
        )
        out, err = _run(text)
        self.assertEqual(err, "", "a balanced quote swallows no question — not damage")
        self.assertEqual(out, text)

    def test_a_FENCED_example_elsewhere_does_not_make_a_balanced_span_look_damaged(self):
        # The exact repro john-the-dev, qingyun-wu and bassilkhilo-ag2 each
        # reproduced at 06f3dfc4. The span branch compared RAW text against the
        # fully `mask_markup`-ed text, so a legitimately FENCED `## ` heading
        # anywhere in the document — masked by the FENCE pass, not the span pass —
        # counted as "a question was swallowed". A separate, perfectly balanced
        # span quoting the divider then warned on healthy markup.
        #
        # Both halves are required: drop the fenced heading and the old code was
        # already quiet (that is the test above), so this is the combination, not
        # either piece.
        text = (
            "## live\n"
            f"{FENCE}\n"
            "## heading inside a legitimate fenced example\n"
            f"{FENCE}\n"
            f"prose opens {TICK}\n"
            "# Resolved\n"
            f"{TICK} closing prose\n"
            "## still live\n"
        )
        out, err = _run(text)
        self.assertEqual(err, "",
                         "a fenced example elsewhere is not span damage — the damage test "
                         "must be scoped to the pass that hid the divider")
        self.assertEqual(out, text)

    def test_a_FENCED_BULLET_elsewhere_also_does_not_trigger_it(self):
        # Same defect via the other reader-recognised shape. `_question_entry_masked`
        # accepts `## ` OR `- **[label]**`, so fixing only the heading half would
        # leave the bullet half live.
        text = (
            "## live\n"
            f"{FENCE}\n"
            "- **[example, 2026-01-01]** a bullet inside a legitimate fenced block\n"
            f"{FENCE}\n"
            f"prose opens {TICK}\n"
            "# Resolved\n"
            f"{TICK} closing prose\n"
            "## still live\n"
        )
        out, err = _run(text)
        self.assertEqual(err, "", "a fenced bullet example is not span damage either")
        self.assertEqual(out, text)

    def test_a_span_that_swallows_a_REAL_question_still_warns(self):
        # Over-trigger control for the scoping change. Narrowing the comparison
        # must not blind the detector to the case it exists for: a span that pairs
        # legitimately and takes a LIVE question with it on the way.
        text = (
            "## live one\n"
            f"prose opens {TICK}\n"
            "## a live question the span swallowed\n"
            "# Resolved\n"
            "archived body\n"
            f"{TICK} closes\n"
        )
        out, err = _run(text)
        self.assertIn("MASKED by markup", err,
                      "scoping the damage test must not silence real span damage")

    def test_TWO_INDEPENDENT_balanced_spans_do_not_implicate_each_other(self):
        # qingyun-wu at 446de6c7. Scoping the damage test to the span PASS was not
        # enough — it still ran across the whole document, so a question swallowed
        # by ONE deliberate span counted as damage for a divider hidden by a
        # DIFFERENT, unrelated span. Both quotes here are intentional and balanced;
        # nothing live is lost, so nothing may warn.
        #
        # This is the control that pins the relation as LOCAL: the swallowed
        # question must belong to the same span that hid the divider.
        text = (
            "## live one\n"
            f"prose opens {TICK}\n"
            "## an example heading inside a deliberate quote\n"
            f"{TICK} closes the first span\n"
            "\n"
            f"prose opens {TICK}\n"
            "# Resolved\n"
            f"{TICK} closes the second span\n"
            "## still live\n"
        )
        out, err = _run(text)
        self.assertEqual(err, "",
                         "a question swallowed by an UNRELATED span must not implicate "
                         "a divider hidden by a different one")
        self.assertEqual(out, text)

    def test_the_swallowing_span_still_warns_when_it_is_the_SAME_span(self):
        # Over-trigger control for the scoping. Narrowing to the hiding span must
        # not blind the detector when that very span takes a live question with it.
        text = (
            "## live one\n"
            f"prose opens {TICK}\n"
            "## a live question this same span swallowed\n"
            "# Resolved\n"
            "archived body\n"
            f"{TICK} closes\n"
        )
        out, err = _run(text)
        self.assertIn("MASKED by markup", err,
                      "same-span damage must still warn after the scoping")

    def test_a_span_opening_AFTER_the_heading_prefix_is_not_damage(self):
        # qingyun-wu at 777ee325. The range-local relation was right but the test
        # inside it was still "raw looks like a question AND the line changed".
        # A balanced span can open AFTER the `## `, quote the divider and close
        # normally — the heading stays fully visible to every reader, so nothing
        # was swallowed. Only the disappearance of the RECOGNISED PREFIX counts.
        text = (
            f"## live question opens {TICK}\n"
            "# Resolved\n"
            f"{TICK} closing quote\n"
            "## still live\n"
        )
        out, err = _run(text)
        self.assertEqual(err, "",
                         "the heading survives masking — no question was swallowed")
        self.assertEqual(out, text)

    def test_a_span_opening_AFTER_a_bullet_label_is_not_damage_either(self):
        # Same defect via the other reader-recognised shape; fixing only the
        # heading half would leave the bullet half live.
        text = (
            f"- **[label, 2026-01-01]** opens {TICK}\n"
            "# Resolved\n"
            f"{TICK} closes\n"
            "## still live\n"
        )
        out, err = _run(text)
        self.assertEqual(err, "", "the bullet label survives masking — nothing swallowed")
        self.assertEqual(out, text)

    def test_no_divider_at_all_is_silent(self):
        text = "## only live questions\nbody\n"
        out, err = _run(text)
        self.assertEqual(err, "")
        self.assertEqual(out, text)

    def test_an_UNRELATED_later_unclosed_fence_is_not_damage(self):
        # qingyun-wu P1 on 8ad855ac. The fence branch asked a DOCUMENT-WIDE
        # question — "is any fence in this file unclosed" — which cannot answer
        # the local one, "did an unclosed fence hide THIS divider".
        #
        # Here the divider sits inside a CLOSED fenced example. It hid nothing
        # and served no archived content as live. A later, entirely unrelated
        # runaway fence then made it warn as damage. The span branch had already
        # been made range-local in an earlier round; this branch was the half
        # that never got it, so the same defect survived in the sibling.
        text = (
            "## live\n"
            "\n"
            f"{FENCE}\n"
            "# Resolved\n"
            f"{FENCE}\n"
            "\n"
            "## still live\n"
            "\n"
            f"{FENCE}\n"
            "unclosed later\n"
        )
        out, err = _run(text)
        self.assertEqual(err, "", "a divider inside a CLOSED fence is not damage")
        self.assertEqual(out, text)

    def test_the_SAME_document_without_the_later_fence_is_also_silent(self):
        # The control that isolates the cause: identical except for the runaway
        # fence. Both must be silent, and before the fix only this one was —
        # which is what proves the later fence was the thing implicating an
        # unrelated divider.
        text = (
            "## live\n"
            "\n"
            f"{FENCE}\n"
            "# Resolved\n"
            f"{FENCE}\n"
            "\n"
            "## still live\n"
        )
        out, err = _run(text)
        self.assertEqual(err, "")
        self.assertEqual(out, text)

    def test_a_divider_INSIDE_the_unclosed_fence_still_warns(self):
        # Over-trigger control. Range-scoping must not silence the real case:
        # here the runaway fence genuinely swallows the divider, so everything
        # after it is masked and archived content would be served as live. This
        # is the failure the whole check exists to catch.
        text = (
            "## live\n"
            "\n"
            f"{FENCE}\n"
            "stuff\n"
            "# Resolved\n"
            "more\n"
        )
        out, err = _run(text)
        self.assertNotEqual(err, "", "a divider hidden by the UNCLOSED fence is real damage")
        self.assertIn("MASKED", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
