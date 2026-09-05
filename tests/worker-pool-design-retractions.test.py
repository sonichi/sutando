#!/usr/bin/env python3
"""A phrase the design has retracted must not be ASSERTED again anywhere in it.
A line narrating its own retraction is exempt, or this flags the fix itself.
"""
import pathlib
import re
import unittest

DOC = pathlib.Path(__file__).resolve().parents[1] / "docs" / "worker-pool-design.md"

# A line carrying one of these is describing the retraction, not asserting it.
HISTORICAL = (
    "an earlier revision", "an earlier draft", "used to", "no longer",
    "still called", "still said", "still asserted", "retracted", "was left",
)

RETRACTED = [
    ("never a second live claimant",
     "the core stand-in is check-then-act like any other: a worker can pass its "
     "eligibility read just before the core crosses stand_in_after_s and claim a "
     "different task for the same room. The document's own measurement -- different "
     "task keys, same room -> 0, 0 -- says both win, so the core needs the "
     "revocation boundary it once claimed to be exempt from."),
    ("the core can simply tell it to stop",
     "responsive is not quiescent: an answer describes the instant it was written "
     "and does not forbid the next claim. The re-bind goes over the same durable "
     "revocation boundary as the stale case."),
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
    ("every member is a target",
     "a set's later members are STANDBY and never claimants. Only a repin makes two "
     "instances momentarily both read themselves as instances[0], and the per-task "
     "claim already arbitrates that window."),
    ("members race for the same claim",
     "same retraction, in the routing rule: pinned-to-a-set means claim only when this "
     "worker IS instances[0]; otherwise suppress."),
    ("it is bounded and benign",
     "asserted, not shown. The per-task claim covers ONE task; two DIFFERENT tasks "
     "in one room each win their own claim, which is the concurrency this section "
     "forbids. The window is fenced by a generation revalidated before execution."),
    ("it cannot be *executed* under one",
     "revalidate-then-execute is check-then-act: a repin landing between the two "
     "still executes under a stale generation, and reconcile cannot unsend an "
     "external effect. The fence is per repin CAUSE — self-fencing on beat age for "
     "death, drain-before-rewrite for an owner command."),
    ("a worker dead it moves that name out of position 0",
     "death rewrites NO binding in v1. The only rewrite is an owner re-bind, applied "
     "while the outgoing worker is responsive; on death the core stands in and the "
     "returning worker still reads itself as instances[0]."),
    ("the outgoing worker **fences itself**",
     "self-fencing on beat age is still check-then-act: age advances between the "
     "check and the execution. v1 removes the automatic worker-to-worker repin "
     "entirely -- on death the CORE stands in (:999), so there is no second live "
     "claimant to fence."),
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
        # Per SENTENCE, not per paragraph: a paragraph-wide exemption lets a
        # live re-assertion ride along beside any retraction narration.
        for sent in re.split(r"(?<=[.:;])\s+", flat):
            if phrase in sent and not any(h in sent.lower() for h in HISTORICAL):
                out.append(start)
                return

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


class ExemptionIsSentenceScoped(unittest.TestCase):
    """A re-assertion beside a retraction narration must still be caught.

    Reported by @yixuan-ag2 against 8e0ba96b with the control pair below: the
    exemption was judged over the whole flattened paragraph, so any pin phrase
    co-located with a HISTORICAL token went silently exempt -- and the exempt
    paragraphs are precisely the ones discussing retractions.
    """

    def test_a_live_reassertion_inside_an_exempt_paragraph_is_caught(self):
        for phrase, _ in RETRACTED:
            exempt = ("An earlier revision used to claim this, and it was "
                      "retracted. " + phrase + " remains true of the ticker.")
            self.assertTrue(
                live_hits(exempt, phrase),
                "%r rode along beside a HISTORICAL token" % phrase,
            )

    def test_the_narration_itself_is_still_exempt(self):
        for phrase, _ in RETRACTED:
            narration = "An earlier draft said " + phrase + " and that was retracted."
            self.assertEqual(
                live_hits(narration, phrase), [],
                "%r false-positived on its own retraction" % phrase,
            )


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


class NoStandInPathIsDescribed(unittest.TestCase):
    """v1 removed the core stand-in, so NO path may admit new work for an
    ineligible worker. This is not a phrase pin: it asserts the absence of a
    described BEHAVIOUR across the whole document, and it fails if any future
    revision reintroduces one under any wording that pairs an ineligibility
    trigger with the core claiming.

    The reviewer's own point stands — the interleaving property belongs to the
    implementation PR. What a design doc can assert is that the unsafe operation
    is not described anywhere, which is exactly what (b) claims.
    """

    # Semantic, not lexical. The previous pattern listed three exact phrasings and
    # so returned zero hits on a document carrying nineteen stand-in lines.
    STANDIN = re.compile(
        r"core stands? in\b"
        r"|stands? in (for|on)\b"
        r"|stand-in decision"
        r"|(served|handled|claimed|picked up) by the core"
        r"|falls? (to|back to) the core"
        r"|stale-target fallthrough"
        r"|core (?:\w+ ){0,2}(?:take|takes|took|taking) over"
        r"|the core (claims|serves|owns) (that|its|the|his|her|their) room",
        re.I)
    # A bound room whose worker cannot act. Rule 3 (work addressed to NOBODY) is a
    # different case and legitimately reaches the core, so the scan must not flag it.
    TRIGGER = re.compile(r"stale|wedged|ineligible|quota spent|hung session|quiesced"
        r"|dead worker|too broken|gets stood in for", re.I)
    # No lexical unbound-exemption: it cannot tell "this room is unbound" from
    # "like an unbound room". Pairing with TRIGGER is what separates them.

    def _lines(self):
        """Sentences, not physical lines. Markdown wraps at ~90 chars, so a trigger and
        its stand-in routinely land on different lines of one sentence and never pair."""
        text = DOC.read_text(encoding="utf-8")
        joined = re.sub(r"\n(?![\s*\-|#])", " ", text)
        return [u for block in joined.splitlines() for u in re.split(r"(?<=[.;])\s+", block)]

    # A regex cannot tell an assertion from its denial: a sentence stating that no
    # fallthrough exists matches the same tokens as one describing it.
    def test_no_bound_room_falls_to_the_core(self):
        bad = [(i + 1, l.strip()[:70]) for i, l in enumerate(self._lines())
               if self.STANDIN.search(l) and self.TRIGGER.search(l)]
        self.assertEqual(
            bad, [],
            "a stand-in path for a BOUND room is described at %s"
            % [n for n, _ in bad])

    def test_the_check_fires_on_wording_it_does_not_contain(self):
        """A control quoting the pattern's own branch validates nothing.

        The previous control injected the literal string "the core stands in
        for its rooms" -- the first alternate of the pattern it was testing --
        so it asserted only that a regex matches itself. These paraphrases
        share the meaning and none is a literal branch.
        """
        for phrase in [
            "When a worker is stale, the core takes over its room.",
            "a dead worker's rooms are served by the core",
            "the room falls to the core",
            "the core's stand-in decision",
            "stands in on unclaimed-task age",
        ]:
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    self.STANDIN.search(phrase),
                    "pattern cannot detect a stand-in phrased as: %r" % phrase)

    def test_rule_three_is_not_flagged(self):
        """Negative control: work addressed to nobody still reaches the core.

        Rule 3 survives direction (b) untouched, so these must NOT trip the
        scan. They match STANDIN and carry no ineligibility trigger, which is
        precisely the discrimination the pairing buys.
        """
        for ok in [
            "An unbound room falls to the core, exactly as an unreadable file does.",
            "A task addressed to nobody is served by the core.",
        ]:
            with self.subTest(ok=ok):
                self.assertTrue(self.STANDIN.search(ok), "sanity: phrasing matches")
                self.assertFalse(self.TRIGGER.search(ok),
                                 "rule 3 must not be flagged as a stand-in")

    def test_the_pairing_fires_on_the_reviewers_control(self):
        """keweichen's control: paraphrased stand-in on an ineligible worker."""
        c = "When a worker is stale, the core takes over its room."
        self.assertTrue(self.STANDIN.search(c) and self.TRIGGER.search(c))


class RemovalUnbindsBeforeTheInstaller(unittest.TestCase):
    """A removed worker never returns, so no-stand-in would strand its rooms forever
    unless removal rewrites the bindings — and the rewrite must precede the installer.
    """

    # Prose test, same class as the stand-in scan: a regression net over the wording
    # that exists, not a proof the transition is correct.
    def _text(self):
        return DOC.read_text(encoding="utf-8")

    def test_remove_is_not_installer_only(self):
        t = self._text()
        self.assertNotIn(
            "remove worker W | the core runs the installer", t,
            "remove/resize listed as an installer call leaves bindings naming a worker "
            "that cannot return")

    def test_the_worker_is_fenced_before_anything_is_reclaimed_or_rewritten(self):
        """The previous version of this test asserted rewrite-before-installer, which
        PINNED an unsafe order: a suspended worker resumes and claims on a stale
        eligibility read. Stopping the process is the only fence."""
        t = self._text()
        i_fence = t.find("FENCE W FIRST")
        i_drain = t.find("Drain what it holds")
        i_rewrite = t.find("REWRITE THE BINDINGS")
        for name, i in (("fence", i_fence), ("drain", i_drain), ("rewrite", i_rewrite)):
            self.assertNotEqual(i, -1, "no %s step is described" % name)
        self.assertLess(i_fence, i_drain, "reclaiming before the worker is stopped lets "
                                          "the reclaimed path and W run the same task")
        self.assertLess(i_fence, i_rewrite, "publishing a new binding before W is stopped "
                                            "lets W claim against its stale view")

    def test_ineligibility_is_not_offered_as_the_fence(self):
        t = self._text()
        self.assertIn("Marking W ineligible is NOT a substitute", t,
                      "a read-gated flag cannot fence a process already past the read")

    def test_resize_to_zero_reaches_core_by_rule_three(self):
        t = self._text()
        self.assertRegex(
            t, r"every room ends unbound and the core serves it under rule 3",
            "resize to 0 must restore core-only mode by UNBINDING, not by a stand-in")


if __name__ == "__main__":
    unittest.main(verbosity=1)
