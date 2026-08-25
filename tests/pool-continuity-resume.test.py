#!/usr/bin/env python3
"""Continuity-first startup policy: always resume, and diagnose before blaming.

The two inversions this suite exists to pin: size and age must never turn a
resume into a fresh start, and a burst of rapid deaths must not be treated as
proof the session is corrupt until an isolated probe says so.

Run: python3 tests/pool-continuity-resume.test.py   (stdlib only)
"""
from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from pool_resume import (  # noqa: E402
    BACKOFF, NEW, PROBE, RESUME, advisories, decide, failures_on,
    runtime_capability)


def head(session="sess-A", runtime="claude"):
    return {"session_id": session, "runtime": runtime}


def fails(session, n):
    return [{"session_id": session, "ok": False} for _ in range(n)]


class FirstStartTests(unittest.TestCase):
    def test_no_head_starts_a_first_generation(self):
        d = decide(None)
        self.assertEqual((d["action"], d["reason"]), (NEW, "initial"))

    def test_a_head_without_a_session_starts_fresh(self):
        d = decide({"session_id": None, "runtime": "claude"})
        self.assertEqual((d["action"], d["reason"]), (NEW, "initial"))

    def test_a_recorded_session_is_always_resumed_first(self):
        d = decide(head())
        self.assertEqual((d["action"], d["session_id"]), (RESUME, "sess-A"))


class NeverGateOnSizeOrAgeTests(unittest.TestCase):
    """Expensive is not a reason to discard a conversation.

    The guarantee is structural rather than behavioural: decide() is not
    given size or age at all, so it cannot gate on them. Asserting "a big
    transcript still resumes" against a function that never sees the size
    would pass no matter what the body did — so the signature is the check.
    """

    def test_decide_is_not_even_given_a_size_or_age(self):
        params = set(inspect.signature(decide).parameters)
        self.assertEqual(params, {"head", "attempts", "probe_ok"})
        self.assertEqual(
            [p for p in params if "byte" in p or "size" in p or "age" in p],
            [])

    def test_a_head_carrying_size_and_age_changes_nothing(self):
        big = dict(head(), transcript_bytes=512 * 1024 * 1024,
                   age_s=90 * 24 * 3600)
        self.assertEqual(decide(big), decide(head()))

    def test_an_enormous_transcript_only_advises_compaction(self):
        self.assertEqual(
            advisories(head(), transcript_bytes=512 * 1024 * 1024,
                       policy={"max_bytes": 64 * 1024 * 1024}),
            ["compact_after_resume"])

    def test_an_ancient_session_only_advises_a_rollover(self):
        self.assertEqual(
            advisories(head(), age_s=90 * 24 * 3600,
                       policy={"max_age_s": 7 * 24 * 3600}),
            ["rollover_suggested"])

    def test_no_advisory_is_ever_an_action(self):
        adv = advisories(head(), transcript_bytes=10 ** 12, age_s=10 ** 9,
                         policy={"max_bytes": 1, "max_age_s": 1})
        self.assertEqual([a for a in adv if a in (RESUME, NEW, PROBE, BACKOFF)],
                         [])

    def test_advisories_are_empty_within_bounds(self):
        self.assertEqual(
            advisories(head(), transcript_bytes=1, age_s=1,
                       policy={"max_bytes": 10, "max_age_s": 10}), [])

    def test_advisories_need_a_policy_to_fire(self):
        self.assertEqual(advisories(head(), transcript_bytes=10 ** 12), [])

    def test_both_bounds_exceeded_yields_both_advisories(self):
        self.assertEqual(
            sorted(advisories(head(), transcript_bytes=100, age_s=100,
                              policy={"max_bytes": 1, "max_age_s": 1})),
            ["compact_after_resume", "rollover_suggested"])


class AttributionTests(unittest.TestCase):
    """A rapid death is not evidence about the session until a probe says so."""

    def test_one_failure_retries_the_same_session(self):
        d = decide(head(), fails("sess-A", 1))
        self.assertEqual((d["action"], d["session_id"]), (RESUME, "sess-A"))

    def test_a_reproduced_failure_asks_for_a_probe_not_a_new_session(self):
        d = decide(head(), fails("sess-A", 2))
        self.assertEqual(d["action"], PROBE)

    def test_a_healthy_probe_blames_the_session(self):
        d = decide(head(), fails("sess-A", 2), probe_ok=True)
        self.assertEqual((d["action"], d["reason"], d["quarantine"]),
                         (NEW, "resume_failed", "sess-A"))

    def test_a_failing_probe_blames_the_environment_and_backs_off(self):
        """The case a counter gets wrong: a broken config kills a fresh
        session too, and spawning more would bury the real fault."""
        d = decide(head(), fails("sess-A", 2), probe_ok=False)
        self.assertEqual(d["action"], BACKOFF)
        self.assertNotIn("quarantine", d)

    def test_many_failures_without_a_probe_never_reach_a_new_session(self):
        for n in range(2, 12):
            self.assertEqual(decide(head(), fails("sess-A", n))["action"],
                             PROBE, f"{n} failures skipped the probe")

    def test_a_success_resets_the_failure_run(self):
        attempts = fails("sess-A", 5) + [{"session_id": "sess-A", "ok": True}]
        self.assertEqual(decide(head(), attempts)["action"], RESUME)

    def test_failures_against_another_session_do_not_count(self):
        attempts = fails("sess-OLD", 9)
        self.assertEqual(decide(head(), attempts)["action"], RESUME)

    def test_failures_on(self):
        self.assertEqual(failures_on("s", fails("s", 3)), 3)
        self.assertEqual(failures_on("s", []), 0)
        self.assertEqual(failures_on(None, fails("s", 3)), 0)
        self.assertEqual(
            failures_on("s", fails("s", 2) + [{"session_id": "s", "ok": True}]),
            0)


class RuntimeCapabilityTests(unittest.TestCase):
    def test_claude_can_resume_and_preassign(self):
        self.assertEqual(runtime_capability("claude"),
                         {"resume": True, "preassign": True})

    def test_codex_can_resume_but_not_preassign(self):
        """Verified on this host: `codex resume [SESSION_ID]` exists, and no
        top-level flag assigns an id at creation."""
        self.assertEqual(runtime_capability("codex"),
                         {"resume": True, "preassign": False})

    def test_an_unknown_runtime_degrades_rather_than_raising(self):
        self.assertEqual(runtime_capability("gemini"),
                         {"resume": False, "preassign": False})

    def test_a_runtime_that_cannot_resume_reports_a_continuity_break(self):
        d = decide(head(runtime="gemini"))
        self.assertEqual((d["action"], d["reason"]), (NEW, "runtime_switch"))
        self.assertIn("continuity break", d["note"])


class ReportingTests(unittest.TestCase):
    def test_every_decision_carries_a_human_readable_note(self):
        cases = [decide(None), decide(head()), decide(head(), fails("sess-A", 1)),
                 decide(head(), fails("sess-A", 2)),
                 decide(head(), fails("sess-A", 2), probe_ok=True),
                 decide(head(), fails("sess-A", 2), probe_ok=False)]
        for d in cases:
            self.assertTrue(d.get("note"), d)

    def test_a_continuity_break_says_so_in_words(self):
        d = decide(head(), fails("sess-A", 2), probe_ok=True)
        self.assertIn("continuity", d["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
