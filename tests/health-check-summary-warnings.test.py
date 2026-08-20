#!/usr/bin/env python3
"""The summary line must not claim health while warnings are on screen.

`issues` deliberately excludes `warn` — warnings must not fail the exit code or
wake the launchd notifier. That is correct and this test does not change it.
What was wrong is the PRINTED summary: with three `⚠` rows above it, the tool
still said "All systems operational.", so anything reading the tool by its last
line reported healthy while a real warning stood.

Regression cover: a two-state summary for a three-state tool.
"""
import importlib.util
import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(ROOT, "src", "health-check.py")

# Import the REAL function, not a copy of it. A test that re-implements the
# logic it is guarding cannot fail when that logic regresses — the whole point
# of extracting `summary_line` was to make this test able to see the change.
_spec = importlib.util.spec_from_file_location("health_check_mod", SRC)
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)


def summarise(checks):
    return hc.summary_line(checks).strip()


OK = {"name": "task-queue", "status": "ok", "detail": ""}
WARN1 = {"name": "core-supervisor", "status": "warn", "detail": "core degraded"}
WARN2 = {"name": "screen-capture", "status": "warn", "detail": "not running"}
DOWN1 = {"name": "voice-agent", "status": "down", "detail": "port 9900"}
DOWN2 = {"name": "web-client", "status": "down", "detail": "port 8080"}
STALE = {"name": "bodhi-dist", "status": "stale", "detail": "rebuilt"}


class SummaryTests(unittest.TestCase):
    def test_clean_still_says_operational(self):
        """The all-clear wording is unchanged — tooling may match on it."""
        self.assertEqual(summarise([OK, OK]), "All systems operational.")

    def test_warning_present_does_not_claim_operational(self):
        out = summarise([OK, WARN1])
        self.assertNotIn("All systems operational", out)
        self.assertIn("1 warning", out)
        self.assertIn("core-supervisor", out)

    def test_multiple_warnings_are_counted_and_named(self):
        out = summarise([OK, WARN1, WARN2])
        self.assertIn("2 warning(s)", out)
        self.assertIn("core-supervisor", out)
        self.assertIn("screen-capture", out)

    def test_warn_is_still_not_an_issue(self):
        """Guard the property we deliberately did NOT change."""
        issues = [c for c in [OK, WARN1, WARN2] if c["status"] not in ("ok", "warn")]
        self.assertEqual(issues, [])


class FailureTests(unittest.TestCase):
    """A down row must never be summarised as health: a ✗ present with
    "All systems operational" is the failure this class exists to reject."""

    def test_down_alone_is_not_operational(self):
        out = summarise([OK, DOWN1])
        self.assertNotIn("All systems operational", out)
        self.assertIn("voice-agent", out)
        self.assertIn("ISSUE", out)

    def test_down_with_warns_does_not_claim_no_failures(self):
        out = summarise([OK, DOWN1, WARN1])
        self.assertNotIn("No failures", out)
        self.assertIn("voice-agent", out)
        self.assertIn("core-supervisor", out)

    def test_every_failure_is_named(self):
        out = summarise([DOWN1, DOWN2])
        self.assertIn("2 ISSUE(S)", out)
        self.assertIn("voice-agent", out)
        self.assertIn("web-client", out)

    def test_stale_is_reported_because_it_exits_non_zero(self):
        """`stale` renders ♻ rather than ✗, which is why I first excluded it —
        but the exit-code list counts it, so an all-clear here would contradict."""
        out = summarise([OK, STALE])
        self.assertNotIn("All systems operational", out)
        self.assertIn("bodhi-dist", out)

    def test_summary_and_exit_code_cannot_disagree(self):
        """Both must read the same predicate; this is the regression that
        matters, not the wording."""
        for combo in ([OK, STALE], [OK, DOWN1], [OK, WARN1], [OK],
                      [OK, STALE, WARN1], [DOWN1, STALE]):
            issues = [c for c in combo if hc.is_issue(c)]
            says_clear = summarise(combo) == "All systems operational."
            # One direction only: a warning yields a non-clear summary with no
            # issues, which is correct. What must never happen is exit!=0 + clear.
            if issues:
                self.assertFalse(says_clear,
                                 f"exits 1 on {[c['name'] for c in issues]} "
                                 f"but summary reads {summarise(combo)!r}")

    def test_is_issue_reads_the_shared_benign_constant(self):
        """_BENIGN_STATUSES existed and nothing used it; that is how the two
        rules drifted in the first place."""
        self.assertEqual(hc._BENIGN_STATUSES, ("ok", "warn"))
        self.assertFalse(hc.is_issue({"status": "ok"}))
        self.assertFalse(hc.is_issue({"status": "warn"}))
        self.assertTrue(hc.is_issue({"status": "stale"}))

    def test_an_unknown_future_status_is_reported_not_swallowed(self):
        odd = {"name": "new-probe", "status": "erupted", "detail": ""}
        out = summarise([OK, odd])
        self.assertIn("new-probe", out)


class SourceTests(unittest.TestCase):
    def test_main_renders_via_the_shared_function(self):
        """Structural guard: main() must call summary_line, not inline its own
        copy — otherwise the behavioural tests above stop covering what runs."""
        with open(SRC) as fh:
            src = fh.read()
        self.assertIn("print(summary_line(checks))", src)
        self.assertEqual(src.count('return "All systems operational."'), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
