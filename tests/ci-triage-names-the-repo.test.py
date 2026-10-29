#!/usr/bin/env python3
"""A verdict that does not name its repo cannot be checked against the PR meant.

`--repo` defaults, and PR numbers collide across repos: sonichi/sutando#1218 and
ag2-space/ag2space-backend#1218 both exist. Measured 2026-09-18 — the default
read the CLOSED sutando PR and printed "no failing checks on #1218" while the
backend PR carried two FAILUREs. Nothing in the output said which repo answered,
so a green verdict about one repo read as a green verdict about the other.

The union shape is NOT the defect here: `_is_bad` reads CheckRun.conclusion and
StatusContext.state alike, and the positive control below pins that both are
still reported. What is asserted is that every verdict line carries the repo.

Run: python3 tests/ci-triage-names-the-repo.test.py
"""
import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CT_PATH = REPO / "skills" / "review-preflight" / "scripts" / "ci-triage.py"

if not CT_PATH.exists():
    raise SystemExit(f"ci-triage.py not found at {CT_PATH} — refusing to report "
                     "a green run in which no test executed")


def _load():
    spec = importlib.util.spec_from_file_location("ci_triage_repo", CT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Both union members, exactly as gh emits them.
FAILED_CHECKRUN = {"__typename": "CheckRun", "name": "test (3.12)",
                   "status": "COMPLETED", "conclusion": "FAILURE",
                   "startedAt": "2026-09-18T10:00:00Z",
                   "completedAt": "2026-09-18T10:05:00Z"}
FAILED_CONTEXT = {"__typename": "StatusContext", "context": "license/cla",
                  "state": "FAILURE", "startedAt": "2026-09-18T10:00:00Z"}
GREEN_CHECKRUN = {"__typename": "CheckRun", "name": "test (3.14)",
                  "status": "COMPLETED", "conclusion": "SUCCESS",
                  "startedAt": "2026-09-18T10:00:00Z",
                  "completedAt": "2026-09-18T10:05:00Z"}


class _Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _runner(rollup, *, gh_ok=True):
    """Answer only statusCheckRollup; every later lookup returns nothing found.

    The subjects/issues path is not under test here, and an empty answer keeps
    the verdict line the only thing the assertions read.
    """
    def run(args, **kw):
        if not gh_ok:
            return _Result("", 1)
        if "statusCheckRollup" in args:
            return _Result(json.dumps({"statusCheckRollup": rollup}))
        return _Result(json.dumps({"comments": []}))
    return run


def _main_output(mod, argv, rollup, *, gh_ok=True):
    mod.subprocess.run = _runner(rollup, gh_ok=gh_ok)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = mod.main(argv)
    return rc, buf.getvalue()


class NamesTheRepo(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_positive_control_both_union_shapes_are_reported(self):
        """Without this, "now it prints failures" would be unfalsifiable."""
        rc, out = _main_output(
            self.mod, ["1218", "--repo", "ag2-space/ag2space-backend"],
            [FAILED_CHECKRUN, FAILED_CONTEXT, GREEN_CHECKRUN])
        self.assertEqual(rc, 0)
        self.assertIn("2 failing check(s)", out)
        self.assertIn("test (3.12)", out)       # CheckRun.conclusion
        self.assertIn("license/cla", out)       # StatusContext.state
        self.assertNotIn("test (3.14)", out)

    def test_failing_verdict_names_the_repo(self):
        _, out = _main_output(
            self.mod, ["1218", "--repo", "ag2-space/ag2space-backend"],
            [FAILED_CHECKRUN])
        self.assertIn("ag2-space/ag2space-backend#1218", out)

    def test_clean_verdict_names_the_repo(self):
        _, out = _main_output(self.mod, ["1218"], [GREEN_CHECKRUN])
        self.assertIn("no failing checks on sonichi/sutando#1218", out)

    def test_gated_verdict_names_the_repo(self):
        pending = {"__typename": "StatusContext", "context": "license/cla",
                   "state": "PENDING", "startedAt": "2026-09-18T10:00:00Z"}
        _, out = _main_output(self.mod, ["1218"], [GREEN_CHECKRUN, pending])
        self.assertIn("no failing checks on sonichi/sutando#1218, but", out)

    def test_unreadable_verdict_names_the_repo_and_is_not_the_clean_sentence(self):
        """"could not read" and "none failing" must never print the same line."""
        _, out = _main_output(self.mod, ["1218"], None, gh_ok=False)
        self.assertIn("sonichi/sutando#1218", out)
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("no failing checks", out)

    def test_same_number_in_two_repos_gives_distinguishable_verdicts(self):
        """The measured defect: one number, two repos, two different answers."""
        _, green = _main_output(self.mod, ["1218"], [GREEN_CHECKRUN])
        _, red = _main_output(
            self.mod, ["1218", "--repo", "ag2-space/ag2space-backend"],
            [FAILED_CHECKRUN])
        self.assertIn("sonichi/sutando#1218", green)
        self.assertIn("ag2-space/ag2space-backend#1218", red)
        self.assertNotIn("ag2-space", green)


if __name__ == "__main__":
    unittest.main(verbosity=2)
