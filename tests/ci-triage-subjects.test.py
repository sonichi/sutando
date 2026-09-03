#!/usr/bin/env python3
"""Regression pin: a red check's SUBJECT is what finds the issue, not its name.

The check that goes red is named for its detector ("diff coverage >= 95%
(python)"); the open issue is titled after the file ("outbox-race.test.py").
Searching the detector name finds nothing, so the failure reads as novel and
gets diagnosed from scratch. `subjects_from_text` extracts the file, and ranks
lines that ACCUSE a file above the many that merely mention one.

Run: python3 tests/ci-triage-subjects.test.py
Exit code: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ci_triage", REPO / "scripts" / "ci-triage.py")
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

failures: "list[str]" = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        if detail:
            print(f"       {detail}")
        failures.append(label)


# Shaped like a real coverage-gate log: one accusing line, many benign mentions.
LOG = """
  ✖ test TIMED OUT under instrumentation (>120s): tests/outbox-race.test.py
      - tests/browser-persistent.test.py (1/11 skipped)
      - tests/workspace-default.test.py (4/44 skipped)
  running scripts/coverage-gate.sh
"""

subs = ct.subjects_from_text(LOG)

check("a) the accused file is found at all", "tests/outbox-race.test.py" in subs, f"got {subs}")

check("b) the accused file RANKS FIRST, ahead of merely-mentioned ones",
      subs and subs[0] == "tests/outbox-race.test.py", f"got {subs}")

# Changed deliberately: benign mentions are now EXCLUDED, not merely ranked
# lower. A confident pointer at an unrelated file is worse than reporting none.
check("c) files mentioned WITHOUT a failure marker are excluded entirely",
      "tests/browser-persistent.test.py" not in subs
      and "tests/workspace-default.test.py" not in subs, f"got {subs}")

# CONTROL: without blame-ranking, log order would put the accused file first only
# by luck. Put it LAST in the text and confirm ranking still promotes it.
LOG_REORDERED = """
      - tests/browser-persistent.test.py (1/11 skipped)
      - tests/workspace-default.test.py (4/44 skipped)
  ✖ test TIMED OUT under instrumentation (>120s): tests/outbox-race.test.py
"""
subs2 = ct.subjects_from_text(LOG_REORDERED)
check("d) CONTROL: ranking is by blame, not by position in the text",
      subs2 and subs2[0] == "tests/outbox-race.test.py",
      f"got {subs2} — first-in-text would have returned browser-persistent")

# A coverage miss names a SOURCE file on the accusing line.
COV = "src/review-preflight.py (90.3%): Missing lines 147-148,153-154"
check("e) a source file on an accusing line is a subject too",
      ct.subjects_from_text(COV)[:1] == ["src/review-preflight.py"],
      f"got {ct.subjects_from_text(COV)}")

check("f2) a log with NO accusing line yields nothing, not a listing",
      ct.subjects_from_text("  - tests/a.test.py (skipped)\n  - tests/b.test.py (ok)") == [],
      "whole-text fallback would return both")

# Verbatim annotation text from job 99387881539 (#3588). `::error::` output
# lands in annotations; the job LOG holds the workflow's echoed script instead.
check("e2) a real CI annotation line yields the accused test file",
      ct.subjects_from_text("FAILED: tests/runtime-tui-reference-client.test.py (exit 1)")
      == ["tests/runtime-tui-reference-client.test.py"])

check("f) empty and None inputs yield no subjects, not a crash",
      ct.subjects_from_text("") == [] and ct.subjects_from_text(None) == [])

check("g) duplicates collapse", ct.subjects_from_text(LOG + LOG).count("tests/outbox-race.test.py") == 1)


class _R:
    def __init__(self, rc, out=""):
        self.returncode, self.stdout = rc, out


check("h) a failed gh call reads as UNKNOWN (None), never as 'nothing filed'",
      ct.open_issues_for("x", lambda a: _R(1), "o/r") is None
      and ct.failing_checks("1", lambda a: _R(1), "o/r") is None)

check("i) unparseable stdout is also UNKNOWN",
      ct.open_issues_for("x", lambda a: _R(0, "not json"), "o/r") is None)

_ROLLUP = json.dumps({"statusCheckRollup": [
    {"name": "real", "conclusion": "FAILURE"},
    {"name": "superseded", "conclusion": "CANCELLED"},
    {"name": "fine", "conclusion": "SUCCESS"}]})
check("j2) CANCELLED is capacity, not a failing check — only FAILURE is triaged",
      ct.failing_checks("1", lambda a: _R(0, _ROLLUP), "o/r") == ["real"],
      f'got {ct.failing_checks("1", lambda a: _R(0, _ROLLUP), "o/r")}')

check("j) an empty issue list is a real zero, distinct from a failed call",
      ct.open_issues_for("x", lambda a: _R(0, "[]"), "o/r") == [])

# gh's --search RANKS; it does not match. An issue that merely scores for a path
# is a wrong pointer carrying the tool's authority, so it must be dropped.
_RANKED = json.dumps([{"number": 1, "title": "unrelated flake", "body": "nothing here"},
                      {"number": 2, "title": "tests/x.test.py times out", "body": ""}])
check("k) an issue that only RANKS for the subject is dropped; a literal match is kept",
      [h["number"] for h in ct.open_issues_for("tests/x.test.py", lambda a: _R(0, _RANKED), "o/r")] == [2],
      f'got {ct.open_issues_for("tests/x.test.py", lambda a: _R(0, _RANKED), "o/r")}')


def _raise(_a, **_k):
    raise OSError("gh not on PATH")


_COMMENTS = json.dumps({"comments": [
    {"author": {"login": "github-actions"}, "body": "first"},
    {"author": {"login": "github-actions"}, "body": "second"}]})

# Two failing checks pointing at the SAME run, plus a green one at another run.
_RUNS_ROLLUP = json.dumps({"statusCheckRollup": [
    {"name": "a", "conclusion": "FAILURE", "detailsUrl": "https://x/runs/77/job/1"},
    {"name": "b", "conclusion": "FAILURE", "detailsUrl": "https://x/runs/77/job/2"},
    {"name": "c", "conclusion": "SUCCESS", "detailsUrl": "https://x/runs/99/job/3"}]})
_RUNS_TWO = json.dumps({"statusCheckRollup": [
    {"name": "a", "conclusion": "FAILURE", "detailsUrl": "https://x/runs/77/job/1"},
    {"name": "b", "conclusion": "TIMED_OUT", "detailsUrl": "https://x/runs/88/job/2"}]})

_ACCUSING = "✖ test TIMED OUT under instrumentation (>120s): tests/outbox-race.test.py"


def _ann_runner(a, **_k):
    if "statusCheckRollup" in a:
        return _R(0, _RUNS_ROLLUP)
    if "/jobs" in a[-1]:
        # One failed job and one green one: only the failed job is read.
        return _R(0, json.dumps({"jobs": [{"id": 9, "conclusion": "failure"},
                                          {"id": 8, "conclusion": "success"}]}))
    if "/check-runs/9/" in a[-1]:
        return _R(0, json.dumps([{"message": "the real message"}]))
    # The GREEN job also has annotations. They must not appear: this is what
    # makes the skip-guard load-bearing rather than incidentally satisfied.
    if "/check-runs/8/" in a[-1]:
        return _R(0, json.dumps([{"message": "green job noise"}]))
    return _R(1)


def _log_runner(a, **_k):
    if "statusCheckRollup" in a:
        return _R(0, _RUNS_TWO)
    if "run" in a and "77" in a:
        raise OSError("transient")
    if "run" in a and "88" in a:
        return _R(0, "job log body")
    return _R(1)


def _full_runner(a, **_k):
    if "statusCheckRollup" in a:
        return _R(0, json.dumps({"statusCheckRollup": [
            {"name": "diff coverage >= 95% (python)", "conclusion": "FAILURE"}]}))
    if "comments" in a:
        return _R(0, json.dumps({"comments": [
            {"author": {"login": "github-actions"}, "body": _ACCUSING}]}))
    if "issue" in a:
        return _R(0, json.dumps([{"number": 4242,
                                  "title": "tests/outbox-race.test.py times out",
                                  "body": ""}]))
    return _R(1)


def _no_subject_runner(a, **_k):
    if "statusCheckRollup" in a:
        return _R(0, json.dumps({"statusCheckRollup": [
            {"name": "some gate", "conclusion": "FAILURE",
             "detailsUrl": "https://x/runs/77/job/1"}]}))
    # Comments, annotations and log all readable but naming no file.
    if "comments" in a:
        return _R(0, json.dumps({"comments": [
            {"author": {"login": "github-actions"}, "body": "it broke"}]}))
    return _R(0, "")


def _search_fails_runner(a, **_k):
    if "issue" in a:
        return _R(1)
    return _full_runner(a)


def _no_issue_runner(a, **_k):
    # Search SUCCEEDS and matches nothing. The other half of the discriminator
    # this script exists for: a real zero must not print like a failed lookup.
    if "issue" in a:
        return _R(0, "[]")
    return _full_runner(a)

# Every network helper takes an injected `run`, so the read path needs no
# network. A lookup that did not answer must read UNKNOWN, not empty.

check("l) _gh maps a raising runner to UNKNOWN, not a crash",
      ct._gh(_raise, ["pr", "view"]) is None)

check("m) _gh maps empty stdout to UNKNOWN (a silent gh is not an answer)",
      ct._gh(lambda a: _R(0, "   "), ["x"]) is None)

# gh prints a JSON error body and exits non-zero. Parsing it would yield a
# confident, wrong answer, so the returncode must be checked before the body.
check("m2) a non-zero exit is UNKNOWN even when stdout is VALID json",
      ct._gh(lambda a: _R(1, '{"message":"Not Found"}'), ["x"]) is None)

check("n) failure_text returns '' when gh fails, and joins comment bodies when it works",
      ct.failure_text("1", lambda a: _R(1), "o/r") == ""
      and "second" in ct.failure_text("1", lambda a: _R(0, _COMMENTS), "o/r"))

# _run_ids feeds both annotation_text and log_text: a wrong id sends every
# downstream lookup at another PR's run.
_ids = ct._run_ids("1", lambda a: _R(0, _RUNS_ROLLUP), "o/r")
check("o) _run_ids takes ids only from FAILING checks, and de-duplicates them",
      _ids == ["77"], f"got {_ids}")

check("p) _run_ids yields nothing when gh fails (no id is better than a wrong id)",
      ct._run_ids("1", lambda a: _R(1), "o/r") == [])

_ann = ct.annotation_text("1", _ann_runner, "o/r")
check("q) annotation_text reads the ANNOTATIONS endpoint, skipping non-failed jobs",
      _ann == "the real message" and "green job noise" not in _ann, f"got {_ann!r}")

check("r) log_text survives a raising runner and keeps the successful log",
      ct.log_text("1", _log_runner, "o/r") == "job log body")

check("s) log_text drops a non-zero run rather than treating its stdout as a log",
      ct.log_text("1", lambda a: _R(1, "partial garbage"), "o/r") == "")


# CONTROL: main() is the only caller wiring the helpers together, so one that
# works in isolation can still be mis-sequenced.

def _main_out(argv, runner):
    buf = io.StringIO()
    real, ct.subprocess.run = ct.subprocess.run, runner
    try:
        with contextlib.redirect_stdout(buf):
            rc = ct.main(argv)
    finally:
        ct.subprocess.run = real
    return rc, buf.getvalue()

rc, out = _main_out(["1"], lambda a, **k: _R(1))
check("t) main reports UNKNOWN when gh cannot read the checks — never 'none failing'",
      rc == 0 and "UNKNOWN" in out and "no failing checks" not in out, out.strip())

rc, out = _main_out(["1"], lambda a, **k: _R(0, json.dumps({"statusCheckRollup": []})))
check("u) main reports a real zero when the checks are readable and green",
      rc == 0 and "no failing checks" in out, out.strip())

rc, out = _main_out(["1"], _full_runner)
check("v) main names the failing check, extracts the subject, and reports the open issue",
      rc == 0 and "outbox-race.test.py" in out and "#4242" in out, out.strip())

rc, out = _main_out(["1"], _no_subject_runner)
check("w) main says diagnose-by-hand when no line ACCUSES a file",
      rc == 0 and "diagnose by hand" in out, out.strip())

rc, out = _main_out(["1"], _no_issue_runner)
check("x0) a successful search matching nothing prints a real zero, not UNKNOWN",
      rc == 0 and "no open issue" in out and "FAILED" not in out, out.strip())

rc, out = _main_out(["1"], _search_fails_runner)
check("x) a failed issue search reads as UNKNOWN, not as 'no open issue'",
      rc == 0 and "issue search FAILED" in out and "no open issue" not in out, out.strip())

# A CI log says `tests/x.test.py`; a human titles the issue `x.test.py`.
# Measured live on #3527, whose title omits the prefix.
_TITLE_ONLY = json.dumps([{"number": 9999,
                           "title": "coverage gate: outbox-race.test.py exceeds the cap",
                           "body": "no path here"}])
check("y) an issue naming the file by BASENAME in its title is still found",
      [h["number"] for h in ct.open_issues_for("tests/outbox-race.test.py",
                                               lambda a: _R(0, _TITLE_ONLY), "o/r")] == [9999])

# The fallback must not become a wildcard: a short or unrelated basename that
# appears nowhere still yields nothing.
_UNRELATED = json.dumps([{"number": 1, "title": "something else", "body": "nothing"}])
check("z) the basename fallback does not match an unrelated issue",
      ct.open_issues_for("tests/outbox-race.test.py",
                         lambda a: _R(0, _UNRELATED), "o/r") == [])

# A human/agent comment can quote an unrelated failure — including this tool's
# own output — and those names would read as the current failure's subject.
_MIXED = json.dumps({"comments": [
    {"author": {"login": "github-actions"}, "body": "✖ TIMED OUT: tests/real-subject.test.py"},
    {"author": {"login": "john-the-dev"}, "body": "✖ TIMED OUT: tests/quoted-elsewhere.test.py"},
]})
_ft = ct.failure_text("1", lambda a: _R(0, _MIXED), "o/r")
check("aa) failure_text takes the BOT comment's subject",
      "tests/real-subject.test.py" in _ft)
check("bb) and ignores a human comment quoting an unrelated failure",
      "tests/quoted-elsewhere.test.py" not in _ft, _ft)

# Measured live on #3606: the tool named six files whose NAMES contain
# "failure" and missed the real one, which a lint reported.
_LISTING = "  tests/discord-bridge-archive-failure-keeps-the-file.test.py  tests/outbox-error-paths.test.py"
check("cc) a line merely LISTING *-failure-* files is not an accusation",
      ct.subjects_from_text(_LISTING) == [], ct.subjects_from_text(_LISTING))

_LINT = "prose-cap: tests/injection-guard-sweep.test.py:362 comment block is 3 lines (cap 2)"
check("dd) a lint accusation (tool: path:LINE msg) names its subject",
      ct.subjects_from_text(_LINT) == ["tests/injection-guard-sweep.test.py"],
      ct.subjects_from_text(_LINT))

check("ee) a real ✖ accusation still wins, with the path intact",
      ct.subjects_from_text("✖ test TIMED OUT: tests/outbox-race.test.py")
      == ["tests/outbox-race.test.py"])

print()
if failures:
    print(f"{len(failures)} failure(s)")
    sys.exit(1)
print("A red check's subject is extracted and blame-ranked.")
