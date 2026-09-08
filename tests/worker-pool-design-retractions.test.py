#!/usr/bin/env python3
"""The normative design must not assert a mechanism this design rejected.
A sentence denying the mechanism is exempt, or this flags the rejection itself.
"""
import pathlib
import re
import unittest

DOC = pathlib.Path(__file__).resolve().parents[1] / "docs" / "worker-pool-design.md"
NOTES = pathlib.Path(__file__).resolve().parents[1] / "docs" / "worker-pool-design-notes.md"

# A sentence carrying one of these is denying the mechanism, not prescribing it.
DENIAL = (
    "out of scope", "must not", "does not", "do not", "is not", "are not",
    "never", "no longer", "not used", "removed", "rather than",
)

# History belongs in the notes file. The normative file states rules only.
HISTORY = (
    "an earlier revision", "an earlier draft", "retracted", "two reviewers",
    "the previous paragraph", "superseded version", "a reviewer found",
)

# (pattern rejected as a MECHANISM, positive control that must be present, why)
REJECTED = [
    (r"\blead\b|\brouter\b|\bfollower\b",
     "pool supervisor",
     "a lead, router or follower inside the runtime daemon: the daemon is "
     "transport plus composition, and a scheduler is durable-transition work. "
     "The scheduler is the pool supervisor, its own process."),
    (r"the core (?:sweeps|reclaims|assigns|routes|schedules)"
     r"|the core(?:'s)? sweep"
     r"|the core (?:is|as) the (?:scheduler|supervisor|control plane)"
     r"|core-owned (?:sweep|schedul)",
     "The core agent is an executor only",
     "the core as scheduler. Quota is per account, so a control plane inside an "
     "LLM session goes dark in exactly the outage it must act in."),
    (r"queue_handler_task"
     r"|(?:the )?watcher (?:owns|is) the admission"
     r"|admission owner"
     r"|each watcher (?:claims|admits|routes)",
     "the supervisor",
     "queue_handler_task, or any watcher, as the admission owner. Admission is "
     "one transaction performed by the one scheduler."),
    (r"keyed on the (?:task )?filename"
     r"|claim keyed on the basename"
     r"|filename is the (?:claim )?key"
     r"|(?:the )?basename as the key",
     "canonical task ID",
     "a claim keyed on the filename. Measured 2026-09-08: a stem-keyed "
     "classifier accumulated 254 rows under names that no longer exist."),
    (r"per-worker proactive loop"
     r"|proactive loop on (?:each|every) worker"
     r"|follower-loop fallback"
     r"|the worker's (?:own )?loop (?:claims|routes|enforces)",
     "Worker agents run no proactive loop",
     "a per-worker proactive loop or follower-loop fallback as a routing "
     "mechanism. Server-side routing gives a unique seat, so no fallback "
     "survives in any executor."),
    (r"task-event-handler-claims|task-event-handler-accepts|pool-probation"
     r"|\bfallbacks/|\bdirect/|\bsettled/|\.admit/"
     r"|`token`|`spent`|`held/|`claimed/",
     "lease_until",
     "the claims/accepts/receipts/token file protocol. A multi-file state "
     "change has no transaction, so every seam needs a prose ordering rule."),
    (r"TASK_FILE",
     "Executor.offer",
     "TASK_FILE on stdout as the delivery abstraction. Delivery is the "
     "adapter's problem, and delivery is not admission."),
    (r"_pick\(\)|five-tier|five tier",
     "Routing table",
     "a five-tier _pick(). Routing is one table evaluated once by one party."),
]


def _flat(text):
    """Markdown wraps mid-sentence, so a control phrase is routinely absent from
    every single line while present in the document."""
    return " ".join(text.split())


def live_hits(text, pattern):
    """Sentences asserting `pattern`, excluding those denying it.

    Matches over whitespace-flattened PARAGRAPHS, not raw lines: a rule written
    as one sentence does not match a doc that wrapped it, so the assertion
    passes on wording that was never present and the pin certifies nothing.
    The exemption is judged per SENTENCE, so a denial cannot shelter a live
    prescription that happens to share its paragraph.
    """
    rx = re.compile(pattern, re.I)
    out, start, buf = [], 1, []

    def flush(start, buf):
        if not buf:
            return
        flat = " ".join(" ".join(buf).split())
        for sent in re.split(r"(?<=[.:;])\s+", flat):
            if rx.search(sent) and not any(d in sent.lower() for d in DENIAL):
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


class DocIsPresentAndSubstantial(unittest.TestCase):
    """Positive control: a missing or truncated doc makes every absence
    assertion below pass vacuously."""

    def test_normative_doc_exists(self):
        self.assertTrue(DOC.is_file(), f"{DOC} missing")
        self.assertGreater(len(DOC.read_text(encoding="utf-8")), 12000)

    def test_notes_doc_exists(self):
        self.assertTrue(NOTES.is_file(), f"{NOTES} missing")
        self.assertGreater(len(NOTES.read_text(encoding="utf-8")), 6000)


class RejectedMechanismsAreNotPrescribed(unittest.TestCase):
    def test_no_rejected_mechanism_is_asserted(self):
        text = DOC.read_text(encoding="utf-8")
        for pattern, _control, why in REJECTED:
            with self.subTest(pattern=pattern):
                lines = live_hits(text, pattern)
                self.assertEqual(
                    lines, [],
                    f"rejected mechanism asserted at line(s) {lines}. "
                    f"Rejected because: {why}")

    def test_every_positive_control_is_present(self):
        """Each rejection is paired with wording the doc MUST keep. Without it a
        rejection is satisfiable by deleting the section it guards."""
        text = _flat(DOC.read_text(encoding="utf-8"))
        for _pattern, control, why in REJECTED:
            with self.subTest(control=control):
                self.assertIn(control, text,
                              f"positive control missing: {control!r} ({why})")

    def test_every_pattern_can_fire(self):
        """A pin that cannot produce a positive certifies nothing. Injected into
        an assertive sentence, each pattern must be seen."""
        for pattern, _control, _why in REJECTED:
            with self.subTest(pattern=pattern):
                probe = _PROBES[pattern]
                self.assertNotEqual(
                    live_hits(probe, pattern), [],
                    f"pattern cannot detect its own mechanism: {probe!r}")

    def test_a_denial_is_exempt(self):
        """Discriminating control: the same mechanism, denied."""
        for pattern, _control, _why in REJECTED:
            with self.subTest(pattern=pattern):
                probe = _PROBES[pattern]
                denied = "This design does not use it: " + probe.rstrip(".") + " is out of scope."
                self.assertEqual(
                    live_hits(denied, pattern), [],
                    f"a denial of {pattern!r} was read as a prescription")


# One assertive sentence per rejected mechanism, sharing none of the doc's wording.
_PROBES = {
    REJECTED[0][0]: "The lead process assigns work to each follower.",
    REJECTED[1][0]: "On every pass the core sweeps its workers and reclaims their tasks.",
    REJECTED[2][0]: "The admission owner is queue_handler_task, which holds the lock.",
    REJECTED[3][0]: "The claim is keyed on the filename of the task file.",
    REJECTED[4][0]: "Pin affinity is enforced by the per-worker proactive loop.",
    REJECTED[5][0]: "A winner writes a receipt under task-event-handler-claims and continues.",
    REJECTED[6][0]: "Delivery is a TASK_FILE line printed on stdout.",
    REJECTED[7][0]: "Placement runs through the five-tier _pick() ladder.",
}


class HistoryLivesInTheNotesFile(unittest.TestCase):
    """The split is the point: a rule and the account of how it got there in one
    paragraph is what forced a 1,225-line test onto a 1,914-line draft."""

    def test_normative_doc_carries_no_history(self):
        text = DOC.read_text(encoding="utf-8").lower()
        found = [h for h in HISTORY if h in text]
        self.assertEqual(found, [],
                         f"history phrasing in the normative doc: {found}")

    def test_the_history_scan_can_fire(self):
        probe = "an earlier revision said the core sweeps, and that was retracted."
        self.assertNotEqual([h for h in HISTORY if h in probe], [],
                            "the history scan cannot detect history")

    def test_the_notes_file_is_where_history_is_allowed(self):
        """Control on the split: the notes file must actually carry the record,
        or the normative file is clean because nothing was written down."""
        notes = NOTES.read_text(encoding="utf-8").lower()
        self.assertNotEqual([h for h in HISTORY if h in notes], [],
                            "the notes file records no history, so the split is "
                            "hiding the record rather than relocating it")


class TheChosenContractIsPinned(unittest.TestCase):
    """A phrase scan cannot see an obsolete MODEL left beside its replacement:
    nothing is re-worded, so nothing trips it. Pin the chosen side directly."""

    PRESENT = [
        ("tasks(", "the store's table must be declared, not described"),
        ("TEXT PRIMARY KEY, -- canonical task ID", "the canonical id is the primary key"),
        ("PENDING", "the state machine's re-offerable state"),
        ("OFFERED", "delivery is not admission, so OFFERED is a real state"),
        ("ACCEPTED", "an executor must explicitly accept"),
        ("SUCCEEDED", "a terminal state"),
        ("lease_owner", "possession is guarded on the executor identity"),
        ("attempt", "the anti-replay token in the accept guard"),
        ("PROBING", "the only exit from WEDGED is commanded"),
        ("QUIESCED", "resource exhaustion is a health state, not a mood"),
        ("Process with core", "the bound-but-unavailable wait must be user-visible"),
        ("Rebind", "one of the three owner choices"),
        ("Executor.offer", "the interface is named, not implied"),
        ("id:", "identity comes from the header"),
        ("254", "the identity rule cites its measurement"),
    ]

    def test_every_chosen_element_is_present(self):
        text = _flat(DOC.read_text(encoding="utf-8"))
        for phrase, why in self.PRESENT:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text, f"chosen contract missing: {why}")

    def test_the_pin_can_fail(self):
        """Control: reconstruct the flagged state against a fixture string."""
        for phrase, _why in self.PRESENT:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, "a document that says none of it")


class OneSchedulerIsStatedNormatively(unittest.TestCase):
    """The architectural decision has to be findable as a rule, not inferable
    from the absence of alternatives."""

    def _flat(self):
        return re.sub(r"\s+", " ", DOC.read_text(encoding="utf-8"))

    def test_the_supervisor_is_named_the_only_scheduler(self):
        self.assertRegex(self._flat(), r"only scheduler",
                         "say there is one scheduler; do not leave it to be derived")

    def test_the_zero_candidate_hole_is_closed_by_construction(self):
        f = self._flat()
        self.assertRegex(
            f, r"removed by construction",
            "reconciliation must not be presented as the repair for a routing hole")
        self.assertRegex(
            f, r"lease expiry and restart convergence",
            "scope the periodic backstop, or a reader reads it as the routing path")

    def test_the_lifecycles_are_stated_separate(self):
        self.assertRegex(self._flat(), r"lifecycles are separate",
                         "co-location is allowed; lifecycle coupling is not")


if __name__ == "__main__":
    unittest.main(verbosity=1)
