#!/usr/bin/env python3
"""A phrase the design has retracted must not be ASSERTED again anywhere in it.
A line narrating its own retraction is exempt, or this flags the fix itself.
"""
import pathlib
import unittest

DOC = pathlib.Path(__file__).resolve().parents[1] / "docs" / "worker-pool-design.md"

# A line carrying one of these is describing the retraction, not asserting it.
HISTORICAL = (
    "an earlier revision", "an earlier draft", "used to", "no longer",
    "still called", "still said", "still asserted", "retracted", "was left",
)

RETRACTED = [
    ("same capacity/busy rule",
     "the event path bounds EXECUTION, not ADMISSION: queue_handler_task claims "
     "and marks pending before draining, so there is no admission bound to "
     "inherit. The ticker carries its own."),
    ("per-heartbeat re-list",
     "the reconciliation is owned by the WATCHER; core_heartbeat.py has no "
     "execution path to dispatch_task."),
    ("only periodic things in a pool are",
     "there are three once the ticker exists, and the third is pool-gated."),
    ("and the only one left",
     "two staged PRs remain, not one: the membership prerequisite lands ahead "
     "of the reconciliation ticker."),
]


def live_hits(text, phrase):
    """Lines asserting `phrase`, excluding those narrating its retraction."""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if phrase not in line:
            continue
        if any(h in line.lower() for h in HISTORICAL):
            continue
        out.append(i)
    return out


# The phrase check cannot see an obsolete MODEL left beside its replacement:
# nothing is re-worded, so nothing trips it. Pin both sides of the contract.
CONTRACT = [
    # (must be present, must NOT be asserted, why)
    ("downstream executor",
     "the admission test belongs there",
     "queue_handler_task is a downstream executor; putting the primitive inside it "
     "left all four direct exits bypassing admission."),
    ("refused-over-bound",
     "four things happened",
     "the outcome set is five, not four: refused-over-bound is distinct from "
     "operational failure and :390 must tell them apart."),
]


class ChosenContractIsPinned(unittest.TestCase):
    """The phrase check cannot see an obsolete MODEL left beside its replacement."""

    def test_chosen_side_is_present(self):
        text = DOC.read_text(encoding="utf-8")
        for present, _, why in CONTRACT:
            with self.subTest(present=present):
                self.assertIn(present, text, f"chosen contract missing: {why}")

    def test_superseded_side_is_not_asserted(self):
        text = DOC.read_text(encoding="utf-8")
        for _, gone, why in CONTRACT:
            with self.subTest(gone=gone):
                lines = live_hits(text, gone)
                self.assertEqual(
                    lines, [],
                    f"superseded contract asserted at line(s) {lines}: {why}")

    def test_the_pin_can_fail(self):
        """Control: reconstruct the flagged state and confirm it is caught."""
        bad = DOC.read_text(encoding="utf-8") + "\ndispatch_task reports which of four things happened.\n"
        self.assertNotEqual(live_hits(bad, "four things happened"), [],
                            "the pin cannot detect the state qingyun-wu flagged")


class RetractedClaimsStayRetracted(unittest.TestCase):
    def test_doc_is_present_and_substantial(self):
        # Positive control: a missing or truncated doc makes every absence
        # assertion below pass vacuously.
        self.assertTrue(DOC.is_file(), f"{DOC} missing")
        self.assertGreater(len(DOC.read_text(encoding="utf-8")), 20000)

    def test_no_retracted_phrase_is_asserted(self):
        text = DOC.read_text(encoding="utf-8")
        for phrase, why in RETRACTED:
            with self.subTest(phrase=phrase):
                lines = live_hits(text, phrase)
                self.assertEqual(
                    lines, [],
                    f"{phrase!r} is asserted at line(s) {lines}. Retracted "
                    f"because: {why}",
                )

    def test_the_check_can_fail(self):
        # A checker that cannot produce a failure certifies nothing. Run it
        # against a fixture STRING -- never against the doc under edit.
        for phrase, _ in RETRACTED:
            self.assertEqual(live_hits(f"the rule is {phrase} here", phrase), [1])

    def test_narration_is_exempt(self):
        # The discriminating control: the same phrase, marked historical.
        for phrase, _ in RETRACTED:
            self.assertEqual(
                live_hits(f"an earlier revision said {phrase} here", phrase), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
