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
    (r"leaderless"
     r"|worker(?:s)? (?:may |can |will )?claim(?:s)? unassigned"
     r"|self-claim"
     r"|claim(?:s)? from the unassigned pool",
     "never selects its own work",
     "leaderless claiming, in which a worker takes unassigned work when the "
     "router is down. Owner decision 2026-09-08: the fallback is removed."),
    (r"reclaim[^.;]{0,60}stale"
     r"|stale[^.;]{0,60}(?:reclaim|release)"
     r"|\brepool",
     "Stale is not dead",
     "release keyed on a stale beat. A host sleep stales every beat at once, "
     "so staleness releases held work in bulk and duplicates it."),
    (r"router (?:revives|restarts|respawns|relaunches|keeps|starts) "
     r"|router[^.;]{0,30}(?:keeps|holds) (?:a |the )?(?:worker|process) alive",
     "owns worker recovery",
     "the router reviving a worker. Placement is its only power; restart is a "
     "lifecycle and spend decision the core owns."),
    (r"(?:launchd|supervisor) (?:supervis\w*|keeps|restarts|manages|hosts)\s+"
     r"(?:a |the |each |every )?worker(?:'s)? session"
     r"|per-worker plist",
     "Never a worker's session",
     "a process supervisor owning a worker's session. Measured 2026-09-08: a "
     "KeepAlive wrapper whose session never returns retries forever, invisible "
     "to every heartbeat. A restart is not a resume."),
    (r"stamp (?:alone )?(?:authoris|authoriz)"
     r"|authoris(?:e|es|ed)[^.;]{0,20}(?:by|on) the (?:stamp|envelope)"
     r"|(?:verified|valid) envelope[^.;]{0,30}(?:execute|executes|runs)",
     "resolved, not read off the message",
     "an integrity stamp used as an authorisation boundary. A signed guest "
     "command verifies exactly as a signed owner command does."),
    (r"\bmessage bus\b|\bgRPC\b|\bsocket\b|\bRPC\b",
     "os.rename",
     "a socket, RPC or message bus between pool members. An agent session "
     "exposes no port, so the filesystem is the substrate."),
    (r"target_worker|fan_out",
     "roster",
     "sender-directed routing headers. Placement is read from the roster; a "
     "sender's message is not a routing instruction."),
    (r"least[- ]loaded|busy[- ]cap|auto[- ]?scal(?:e|es|ing)"
     r"|sticky (?:auto-)?affinity|overflow to (?:another|an|the next)",
     "Bindings hold until the owner changes them",
     "load-aware or self-resizing placement, and bindings that change "
     "themselves. Each shipped in #3604's picker and is cut."),
    (r"per-worker proactive loop"
     r"|proactive loop (?:on|in|inside) (?:each|every|a) worker"
     r"|worker(?:'s)? own loop (?:claims|routes|enforces)",
     "A scheduling loop inside a worker",
     "a proactive loop inside a worker. Its crons are allowed; a self-driven "
     "loop that selects work is not."),
    (r"pool supervisor|the router (?:schedules|is the scheduler)",
     "router",
     "the scheduler framing. Owner decision 2026-09-08: the scheduler is a "
     "router, and it delivers rather than schedules."),
    # archiving a payload by rename is legitimate; assigning by renaming it is not
    (r"renam\w+[^.;]{0,30}the payload(?![^.;]{0,24}archive)"
     r"|renam\w+[^.;]{0,30}(?:the task file|the request)"
     r"|assignment[^.;]{0,20}(?:is|by) (?:a |the )?rename"
     r"|\.assigned-|\.claimed-worker",
     "existing IS the assignment",
     "assignment carried as a suffix on the task filename. The payload is "
     "immutable; the assignment is a delivery record in the recipient's folder."),
]


def _flat(text):
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
        self.assertGreater(len(DOC.read_text(encoding="utf-8")), 9000)

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


class HistoryStaysInTheNotes(unittest.TestCase):
    def test_normative_doc_carries_no_revision_history(self):
        flat = _flat(DOC.read_text(encoding="utf-8")).lower()
        for phrase in HISTORY:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, flat,
                                 f"revision history in the normative doc: {phrase!r}")


# One assertive sentence per rejected mechanism, sharing none of the doc's wording.
_PROBES = {
    REJECTED[0][0]: "When the placement daemon goes quiet each executor falls back to leaderless grabbing of whatever sits in the shared spool.",
    REJECTED[1][0]: "Tickets held by a seat whose beat lapsed are repooled on the next sweep.",
    REJECTED[2][0]: "The router restarts a worker whose supervision unit gave up on it.",
    REJECTED[3][0]: "A per-worker plist keeps each seat alive with KeepAlive.",
    REJECTED[4][0]: "A command whose stamp authorises it proceeds straight to execution.",
    REJECTED[5][0]: "Each executor opens a socket back to the scheduler and streams progress over it.",
    REJECTED[6][0]: "The submitting client sets target_worker in the header and the queue honours it.",
    REJECTED[7][0]: "The placement pass hands the ticket to the least-loaded seat currently under its cap.",
    REJECTED[8][0]: "Each worker runs a per-worker proactive loop that wakes on a timer to look for work.",
    REJECTED[9][0]: "The pool supervisor is the sole scheduler and holds every lease.",
    REJECTED[10][0]: "Placement renames the task file so the assignment is a rename of the payload itself.",
}


if __name__ == "__main__":
    unittest.main(verbosity=2)
