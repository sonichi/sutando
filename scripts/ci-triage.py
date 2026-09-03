#!/usr/bin/env python3
"""Map a PR's failing checks to already-open issues, before anyone diagnoses them.

A red check is a pointer into the record, not new information: the ones that
recur are precisely the ones already filed. But the check NAME is the detector
("diff coverage >= 95% (python)") while the issue is filed under the SUBJECT
("tests/outbox-race.test.py"), so searching the name finds nothing and the
failure reads as novel. This extracts subjects from the failure text and
searches those.

Usage:
    python3 scripts/ci-triage.py <PR> [--repo owner/name]

Exit code: 0 always — this is an advisory lookup, never a gate.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

# A test path is the highest-signal subject: issues are titled after them.
_TEST_PATH = re.compile(r"\b(tests/[\w.\-/]+?\.test\.(?:py|sh|ts))\b")
# Some failures name a source file instead (coverage misses, lint hits).
_SRC_PATH = re.compile(r"\b((?:src|scripts|skills)/[\w.\-/]+?\.(?:py|sh|ts))\b")


# A log names far more files than it blames; only these lines accuse one.
_BLAMED = re.compile(r"✖|✗|FAIL|TIMED OUT|Error|Traceback|Missing lines", re.I)
# Lint accusations carry none of those words: `tool: path:LINE message`. Without
# this a real finding is invisible while a mere listing can still look blamed.
_LINT_HIT = re.compile(r"[\w.\-/]+\.(?:py|sh|ts):\d+")


def subjects_from_text(text: str) -> "list[str]":
    """Distinct file subjects named in failure text, most-blamed first.

    A suite log lists every skipped and passing file too, so matching the whole
    text ranks noise alongside the culprit. Lines carrying a failure marker are
    the ones that accuse a file, so those subjects lead.
    """
    def scan(chunk: str) -> "list[str]":
        tests, srcs = [], []
        for rx, out in ((_TEST_PATH, tests), (_SRC_PATH, srcs)):
            out.extend(m.group(1) for m in rx.finditer(chunk or ""))
        return tests + srcs

    def accuses(line: str) -> bool:
        # Paths removed first: `*-failure-*.test.py` contains "FAIL", so a
        # line listing such files would otherwise accuse every file on it.
        stripped = _SRC_PATH.sub(" ", _TEST_PATH.sub(" ", line))
        return bool(_BLAMED.search(stripped) or _LINT_HIT.search(line))

    blamed = "\n".join(l for l in (text or "").splitlines() if accuses(l))
    ordered, seen = [], set()
    # Only blamed lines. Whole-text order is a listing, not an accusation, and a
    # confident pointer at an unrelated file is worse than reporting nothing.
    for v in scan(blamed):
        if v not in seen:
            seen.add(v)
            ordered.append(v)
    return ordered


def _gh(run, args) -> "object | None":
    """None on any failure: a lookup that did not answer must not read as 'nothing filed'."""
    try:
        r = run(["gh"] + args)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout) if r.stdout.strip() else None
    except ValueError:
        return None


def _is_bad(c: dict) -> bool:
    # CANCELLED is capacity (job cap or a superseded run), not a defect — and
    # triaging it sends the reader after a cause that does not exist.
    return c.get("conclusion") in ("FAILURE", "TIMED_OUT") or c.get("state") in ("FAILURE", "ERROR")


def _is_incomplete(c: dict) -> bool:
    """Neither green nor failing — still blocks the merge, and reads as absent.

    Two shapes share this list: a CheckRun carries `status`/`conclusion` and no
    `state`; a StatusContext carries `state` and neither of the others.
    """
    if "status" in c:
        return c.get("status") != "COMPLETED"
    return c.get("state") == "PENDING"


def latest_by_name(rollup) -> "list[dict]":
    """One head can carry several runs, so a check name can appear more than once.

    A concurrency cancellation leaves the loser's jobs in the rollup beside the
    winner's, and a job that starts during cancellation records FAILURE.
    """
    latest: "dict[str, tuple[str, dict]]" = {}
    for c in rollup or []:
        name = c.get("name") or c.get("context") or "?"
        stamp = c.get("completedAt") or c.get("startedAt") or ""
        prev = latest.get(name)
        # Equal stamps leave no order to read, so keep the failing entry rather
        # than forgiving it on a coin flip.
        if prev is None or stamp > prev[0] or (stamp == prev[0] and _is_bad(c)):
            latest[name] = (stamp, c)
    return [c for _, c in latest.values()]


def failing_checks(pr: str, run, repo: str) -> "list[str] | None":
    j = _gh(run, ["pr", "view", pr, "--repo", repo, "--json", "statusCheckRollup"])
    if j is None:
        return None
    return [
        c.get("name") or c.get("context") or "?"
        for c in latest_by_name(j.get("statusCheckRollup"))
        if _is_bad(c)
    ]


def incomplete_checks(pr: str, run, repo: str) -> "list[str] | None":
    """Checks that are neither green nor failing, by display name."""
    j = _gh(run, ["pr", "view", pr, "--repo", repo, "--json", "statusCheckRollup"])
    if j is None:
        return None
    return [
        c.get("name") or c.get("context") or "?"
        for c in latest_by_name(j.get("statusCheckRollup"))
        if _is_incomplete(c)
    ]


def failure_text(pr: str, run, repo: str) -> str:
    """Bot comments carry the subject for gates that report to the PR, not the log.

    BOT comments only. A human or agent comment can name any file — quoting an
    unrelated failure, or this tool's own output — and those names then read as
    the subject of the CURRENT failure with the tool's authority behind them.
    """
    j = _gh(run, ["pr", "view", pr, "--repo", repo, "--json", "comments"])
    if j is None:
        return ""
    out = []
    for c in (j.get("comments") or []):
        login = ((c.get("author") or {}).get("login") or "")
        if login.endswith("[bot]") or login in ("github-actions",):
            out.append(c.get("body") or "")
    return "\n".join(out)


def _run_ids(pr: str, run, repo: str) -> "list[str]":
    j = _gh(run, ["pr", "view", pr, "--repo", repo, "--json", "statusCheckRollup"])
    ids, seen = [], set()
    for c in latest_by_name((j or {}).get("statusCheckRollup")):
        m = re.search(r"/runs/(\d+)", c.get("detailsUrl") or "")
        if _is_bad(c) and m and m.group(1) not in seen:
            seen.add(m.group(1))
            ids.append(m.group(1))
    return ids


def annotation_text(pr: str, run, repo: str) -> str:
    """`::error::` output lands in ANNOTATIONS, not the log.

    A log holds the workflow's echoed script — including the literal
    `::error::` lines it will emit — so grepping it finds the source and not
    the failure. The annotations endpoint is where the emitted text is.
    """
    out = []
    for rid in _run_ids(pr, run, repo)[:2]:
        jobs = _gh(run, ["api", f"repos/{repo}/actions/runs/{rid}/jobs?per_page=100"])
        for j in ((jobs or {}).get("jobs") or []):
            if j.get("conclusion") not in ("failure", "timed_out"):
                continue
            ann = _gh(run, ["api", f"repos/{repo}/check-runs/{j.get('id')}/annotations?per_page=100"])
            for a in (ann or []):
                out.append(str(a.get("message") or ""))
    return "\n".join(out)


def log_text(pr: str, run, repo: str) -> str:
    """Fall back to the failed job's log: some gates report 'see the job log'."""
    out = []
    for rid in _run_ids(pr, run, repo)[:2]:
        try:
            r = run(["gh", "run", "view", rid, "--repo", repo, "--log"])
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            out.append(r.stdout)
    return "\n".join(out)


def open_issues_for(subject: str, run, repo: str) -> "list[dict] | None":
    j = _gh(run, ["issue", "list", "--repo", repo, "--state", "open",
                  "--search", subject, "--json", "number,title,body", "--limit", "20"])
    if j is None:
        return None

    def _hits(needle: str) -> "list[dict]":
        # `gh issue list --search` ranks; it does not match. An issue that merely
        # scores for a path is a wrong pointer wearing the tool's authority.
        return [{"number": i["number"], "title": i["title"]} for i in j
                if needle in (i.get("title") or "") or needle in (i.get("body") or "")]

    found = _hits(subject)
    # A failure log names `tests/x.test.py`; humans title the issue `x.test.py`.
    # Fall back to the basename so a title-only match is not missed on the prefix.
    base = subject.rsplit("/", 1)[-1]
    if not found and base != subject and len(base) > 6:
        found = _hits(base)
    return found


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pr")
    p.add_argument("--repo", default="sonichi/sutando")
    a = p.parse_args(argv)
    run = lambda args: subprocess.run(args, capture_output=True, text=True, timeout=30)

    red = failing_checks(a.pr, run, a.repo)
    if red is None:
        print("ci-triage: could not read checks (gh failed) — UNKNOWN, not 'none failing'")
        return 0
    if not red:
        waiting = incomplete_checks(a.pr, run, a.repo)
        if waiting is None:
            print(f"ci-triage: no failing checks on #{a.pr}; could not re-read "
                  "the rollup for incomplete ones — UNKNOWN, not 'ready'")
        elif waiting:
            print(f"ci-triage: no failing checks on #{a.pr}, but "
                  f"{len(waiting)} not yet green — the merge is still gated:")
            for n in waiting:
                print(f"  … {n}")
        else:
            print(f"ci-triage: no failing checks on #{a.pr}")
        return 0
    print(f"ci-triage: {len(red)} failing check(s) on #{a.pr}:")
    for n in red:
        print(f"  ✖ {n}")

    subjects = subjects_from_text(failure_text(a.pr, run, a.repo))
    if not subjects:
        subjects = subjects_from_text(annotation_text(a.pr, run, a.repo))
    if not subjects:
        # Gates that say "see the job log" put the subject only there.
        subjects = subjects_from_text(log_text(a.pr, run, a.repo))
    if not subjects:
        print("\n  no accusing line named a file — diagnose by hand")
        return 0
    print(f"\n  subjects named in the failure text: {', '.join(subjects[:6])}")
    for s in subjects[:6]:
        hits = open_issues_for(s, run, a.repo)
        if hits is None:
            print(f"  {s}: issue search FAILED — unknown, not 'nothing filed'")
        elif hits:
            print(f"  {s}: {len(hits)} open issue(s) — read before diagnosing")
            for h in hits:
                print(f"      #{h['number']} {h['title'][:72]}")
        else:
            print(f"  {s}: no open issue")
    return 0


if __name__ == "__main__":
    sys.exit(main())
