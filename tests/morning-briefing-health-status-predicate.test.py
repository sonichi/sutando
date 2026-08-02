#!/usr/bin/env python3
"""Tests for morning-briefing.py's health-issue selection predicate.

The bug: `get_health_issues()` scraped health-check.py's *human* output and
selected lines containing "✗". health-check.py renders ✗ only for
down/missing/not_loaded (`src/health-check.py`, the icon ternary) — every other
non-ok status renders differently:

    ✓ ok   ·   ⚠ warn   ·   ✗ down|missing|not_loaded   ·   ♻ stale   ·   ~ (rest)

So `fail` (which includes "on battery at N% — critically low"), `wedged` (a hung
launchd service), `error` and `empty` fell into the `~` catch-all and `stale`
into `♻`, and none could ever appear in the briefing. Meanwhile
health-check.py's own failure predicate counts them:

    failures = [c for c in checks if c["status"] in
                ("down", "missing", "not_loaded", "fail", "stale", "warn")]

Net effect: the briefing said "no issues" while the tool it just ran considered
the host to be failing. `warn` stays excluded — that exclusion was deliberate
("warns are expected/known") and this change preserves it.

The fix reads `--json` and selects on `status`, which is also what
`src/dashboard.py` and `src/agent-api.py` already do with the same subprocess.

No real health-check.py runs here — subprocess.run is mocked.
"""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "morning-briefing.py"
HEALTH_CHECK = REPO / "src" / "health-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("morning_briefing", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


def _run_with(checks, returncode=0):
    """Call get_health_issues() with health-check.py stubbed to emit `checks`."""
    mod = _load()
    payload = json.dumps({"checks": checks})
    with patch.object(mod.subprocess, "run",
                      return_value=_Result(payload, returncode)) as m:
        issues = mod.get_health_issues()
    return issues, m


class TestNonGlyphStatusesAreReported(unittest.TestCase):
    """The regression: these statuses do not render as ✗ and were invisible."""

    def test_fail_is_reported(self):
        # The concrete case that motivated this: critically-low battery.
        issues, _ = _run_with([
            {"name": "battery", "status": "fail",
             "detail": "on battery at 4% — critically low (threshold 10%)"},
        ])
        self.assertEqual(len(issues), 1, "a `fail` check must reach the briefing")
        self.assertIn("battery", issues[0])
        self.assertIn("critically low", issues[0])

    def test_wedged_is_reported(self):
        issues, _ = _run_with([
            {"name": "com.sutando.core", "status": "wedged",
             "detail": "loaded but not responding"},
        ])
        self.assertEqual(len(issues), 1, "a `wedged` service must reach the briefing")
        self.assertIn("com.sutando.core", issues[0])

    def test_stale_is_reported(self):
        issues, _ = _run_with([
            {"name": "sutando-app", "status": "stale",
             "detail": "running, but binary is older than source"},
        ])
        self.assertEqual(len(issues), 1, "a `stale` check must reach the briefing")

    def test_error_and_empty_are_reported(self):
        issues, _ = _run_with([
            {"name": "a", "status": "error", "detail": "boom"},
            {"name": "b", "status": "empty", "detail": "zero bytes"},
        ])
        self.assertEqual(len(issues), 2)


class TestPreservedBehaviour(unittest.TestCase):
    """What the change must NOT alter."""

    def test_ok_is_excluded(self):
        issues, _ = _run_with([{"name": "a", "status": "ok", "detail": "fine"}])
        self.assertEqual(issues, [])

    def test_warn_is_still_excluded(self):
        # Deliberate per the original comment: "warns are expected/known".
        issues, _ = _run_with([
            {"name": "a", "status": "warn", "detail": "nearing threshold"},
        ])
        self.assertEqual(issues, [], "warn must remain excluded from the briefing")

    def test_down_missing_not_loaded_still_reported(self):
        # These DID work before (they render ✗); they must keep working.
        for st in ("down", "missing", "not_loaded"):
            with self.subTest(status=st):
                issues, _ = _run_with([{"name": "x", "status": st, "detail": "d"}])
                self.assertEqual(len(issues), 1, f"{st} regressed")

    def test_caps_at_three(self):
        issues, _ = _run_with([
            {"name": f"n{i}", "status": "fail", "detail": "d"} for i in range(7)
        ])
        self.assertEqual(len(issues), 3)

    def test_name_and_detail_are_joined(self):
        issues, _ = _run_with([
            {"name": "svc", "status": "down", "detail": "not running"},
        ])
        self.assertEqual(issues, ["svc: not running"])

    def test_missing_detail_falls_back_to_name(self):
        issues, _ = _run_with([{"name": "svc", "status": "down", "detail": ""}])
        self.assertEqual(issues, ["svc"])


class TestFailureModesDoNotReadAsHealthy(unittest.TestCase):
    """An unparseable or absent result must not silently mean "no issues" by
    accident — it must reach the explicit [] fallback."""

    def test_empty_stdout_returns_empty(self):
        mod = _load()
        with patch.object(mod.subprocess, "run", return_value=_Result("", 1)):
            self.assertEqual(mod.get_health_issues(), [])

    def test_malformed_json_returns_empty(self):
        mod = _load()
        with patch.object(mod.subprocess, "run", return_value=_Result("not json")):
            self.assertEqual(mod.get_health_issues(), [])

    def test_timeout_returns_empty(self):
        mod = _load()
        import subprocess as sp
        with patch.object(mod.subprocess, "run",
                          side_effect=sp.TimeoutExpired("hc", 30)):
            self.assertEqual(mod.get_health_issues(), [])

    def test_json_flag_is_passed(self):
        """Selecting on status requires --json; without it stdout is the human
        table and json.loads would fail, silently emptying the section."""
        _, m = _run_with([{"name": "a", "status": "ok", "detail": ""}])
        argv = m.call_args[0][0]
        self.assertIn("--json", argv, "health-check.py must be invoked with --json")


class TestPredicateMatchesHealthCheck(unittest.TestCase):
    """Structural: keep the constant honest against health-check.py's own set.

    This is the assertion that would have caught the original bug — the briefing
    and the tool disagreed about what "failure" means.
    """

    def test_covers_health_checks_own_failure_statuses_except_warn(self):
        mod = _load()
        src = HEALTH_CHECK.read_text()
        # health-check.py's own predicate, verbatim in four places.
        needle = '("down", "missing", "not_loaded", "fail", "stale", "warn")'
        self.assertIn(needle, src,
                      "health-check.py's failure predicate moved — re-derive "
                      "_BRIEFING_FAILURE_STATUSES from its new form")
        own = {"down", "missing", "not_loaded", "fail", "stale", "warn"}
        expected = own - {"warn"}
        missing = expected - set(mod._BRIEFING_FAILURE_STATUSES)
        self.assertEqual(missing, set(),
                         f"briefing would silently drop {missing}")
        self.assertNotIn("warn", mod._BRIEFING_FAILURE_STATUSES)

    def test_every_status_health_check_emits_is_classified(self):
        """Any status health-check.py can emit is either excluded on purpose
        (ok/warn) or reported. A new status must not default to invisible."""
        import re as _re
        mod = _load()
        emitted = set(_re.findall(r'"status"\s*:\s*"([a-z_]+)"',
                                  HEALTH_CHECK.read_text()))
        unclassified = emitted - {"ok", "warn"} - set(mod._BRIEFING_FAILURE_STATUSES)
        self.assertEqual(
            unclassified, set(),
            f"health-check.py emits {unclassified}, which the briefing would "
            f"neither report nor deliberately exclude")


if __name__ == "__main__":
    unittest.main(verbosity=2)
