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
    ("is not a new admission at all",
     "true only of the handler-fallback exit. A direct exit with no prior "
     "receipt performs fresh admission -- which of the two applies is a "
     "property of the exit, not of the design."),
    ("still unclaimed on disk, so it is re-admitted normally",
     "mtime cannot tell receipt-before-emit from emit-before-ack, so re-admitting "
     "on age alone duplicates an in-flight task. The ticker decides on the receipt's "
     "phase and lease, never on age."),
    ("only the first may be filled in by handler affinity",
     "the design retires handler affinity and deletes the file; rooms bind only "
     "by an explicit pin. Absent and empty both route to the core, and neither is "
     "ever filled by affinity."),
    ("Believe the task, not the phase",
     "the task file is written BEFORE the notification, so its presence is true on "
     "both sides of the emit and cannot prove publication. The ticker branches on the "
     "task's CLAIM state, never on its existence."),
    ("the claim settles which member takes a given",
     "a bound set that races needs a group lease v1 does not define. A room has ONE "
     "bound instance (instances[0]); later entries are a failover order the CORE "
     "consults on a binding rewrite, never a set workers contend for."),
]


def live_hits(text, phrase):
    """Lines asserting `phrase`, excluding those narrating its retraction.

    Matches over whitespace-flattened PARAGRAPHS, not raw lines: a pin written
    as one sentence does not match a doc that wrapped it, so the assertion
    passes on a phrase that was never present and the pin certifies nothing.
    The paragraph is also the unit the retraction narration lives in, so the
    HISTORICAL exemption is evaluated over the same span it is written across.
    """
    out, start, buf = [], 1, []

    def flush(start, buf):
        if not buf:
            return
        flat = " ".join(" ".join(buf).split())
        if phrase in flat and not any(h in flat.lower() for h in HISTORICAL):
            out.append(start)

    for i, line in enumerate(text.splitlines(), 1):
        if line.strip():
            if not buf:
                start = i
            buf.append(line)
        else:
            flush(start, buf)
            buf = []
    flush(start, buf)
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
    ("step 2 ships no reader",
     "the step-2 consumer",
     "the reader arrives with the step-3 publisher; an earlier draft justified "
     "self-describing staleness by a step-2 consumer that three other sections "
     "say does not exist."),
    ("prior receipt",
     "never by a second admission",
     "two contracts, not one: a direct exit WITH a prior receipt transitions it, one "
     "WITHOUT must admit. Stating only the transition half reads as universal and "
     "leaves the probe-direct path with no admission at all."),
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
        bad = DOC.read_text(encoding="utf-8") + "\n\ndispatch_task reports which of four things happened.\n"
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


class EveryPinCanFire(unittest.TestCase):
    """A pin that cannot produce a positive certifies nothing.

    The wrapped-phrase bug was invisible because the assertion passed on text
    absent from BOTH versions. Injecting each phrase proves the matcher would
    see it if the doc ever asserted it again.
    """

    def test_each_phrase_is_detectable_when_injected(self):
        base = DOC.read_text(encoding="utf-8")
        pins = [g for _, g, _ in CONTRACT] + [ph for ph, _ in RETRACTED]
        self.assertGreater(len(pins), 4)
        for phrase in pins:
            with self.subTest(phrase=phrase):
                self.assertEqual(live_hits(base, phrase), [],
                                 "phrase is asserted in the live doc")
                self.assertNotEqual(
                    live_hits(base + "\n\n" + phrase + "\n", phrase), [],
                    "pin cannot detect its own phrase -- it certifies nothing")


if __name__ == "__main__":
    unittest.main(verbosity=1)
