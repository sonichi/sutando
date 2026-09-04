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
