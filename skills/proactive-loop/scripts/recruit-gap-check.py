#!/usr/bin/env python3
"""Name the PR state where nothing blocks and nobody has been asked.

A cleared CHANGES_REQUESTED and a satisfied approval bar look identical from every
surface that reports "no blocker": reviewDecision goes quiet and mergeStateStatus
stops saying BLOCKED, while an approval is still missing and no one is queued.

The bar comes from the branch RULESET. `branches/<b>/protection` answers 404
"Branch not protected" for a ruleset-protected branch, which reads as "no rule".
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

BLOCKED, RECRUIT, THIN, MET = "blocked", "recruit", "thin", "met"


def latest_states(reviews):
    """Newest non-COMMENTED state per reviewer.

    A COMMENTED review cannot move reviewDecision, so counting it would let a
    remark stand in for a verdict.
    """
    out = {}
    for r in reviews:
        state = r.get("state")
        if state == "COMMENTED":
            continue
        login = (r.get("user") or {}).get("login")
        if login:
            out[login] = state
    return out


def classify(required, latest, shared_logins):
    """Pure verdict. A shared login is counted by GitHub but identifies no one.

    Two shortfalls, never one: `github` decides mergeability, `distinct` decides
    whether anyone outside the shared logins actually vouched. This repo has more
    than one such account, so the parameter is a SET -- a single-login version
    scores the second shared account as a distinct party.
    """
    shared = {shared_logins} if isinstance(shared_logins, str) else set(shared_logins)
    blocking = sorted(u for u, s in latest.items() if s == "CHANGES_REQUESTED")
    counted = sorted(u for u, s in latest.items() if s == "APPROVED")
    distinct = [u for u in counted if u not in shared]
    github_short = max(0, required - len(counted))
    distinct_short = max(0, required - len(distinct))
    if blocking:
        verdict = BLOCKED
    elif github_short:
        verdict = RECRUIT
    elif distinct_short:
        verdict = THIN
    else:
        verdict = MET
    return {
        "verdict": verdict, "required": required, "blocking": blocking,
        "counted": counted, "distinct": distinct,
        "github_short_by": github_short, "distinct_short_by": distinct_short,
    }


def render(pr, v, shared_logins):
    n, req = len(v["counted"]), v["required"]
    if v["verdict"] == BLOCKED:
        return (f"#{pr}: still blocked by {', '.join(v['blocking'])} "
                f"({n}/{req} approvals) -- recruiting is premature")
    if v["verdict"] == RECRUIT:
        return (f"#{pr}: RECRUIT -- nothing blocks, {n}/{req} approvals, short by "
                f"{v['github_short_by']}. Approved: {', '.join(v['counted']) or 'nobody'}")
    if v["verdict"] == THIN:
        return (f"#{pr}: mergeable but thin -- {n}/{req} ({', '.join(v['counted'])}), yet only "
                f"{len(v['distinct'])} are outside the shared logins "
                f"({', '.join(sorted(shared_logins))})")
    return f"#{pr}: bar met ({n}/{req}), nobody to recruit"


def _gh(args):
    p = subprocess.run(["gh", "api"] + args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)}: {p.stderr.strip()[:200]}")
    return json.loads(p.stdout)


def required_approvals(repo, branch):
    for rule in _gh([f"repos/{repo}/rules/branches/{branch}"]):
        if rule.get("type") == "pull_request":
            return int(rule["parameters"].get("required_approving_review_count", 0))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--shared-login", required=True, action="append", dest="shared_logins",
                    help="login shared by several agents; counted by GitHub, identifies no one. "
                         "Repeatable -- this repo has more than one such account.")
    ap.add_argument("--branch", default="main")
    ap.add_argument("prs", nargs="+")
    a = ap.parse_args(argv)
    rc = 0
    try:
        required = required_approvals(a.repo, a.branch)
    except Exception as exc:
        print(f"cannot answer: {exc}")
        return 2
    for pr in a.prs:
        try:
            reviews = _gh([f"repos/{a.repo}/pulls/{pr}/reviews", "--paginate"])
        except Exception as exc:
            print(f"#{pr}: cannot answer -- {exc}")
            rc = max(rc, 2)
            continue
        v = classify(required, latest_states(reviews), a.shared_logins)
        print(render(pr, v, a.shared_logins))
        if v["verdict"] == RECRUIT:
            rc = max(rc, 1)
    return rc


if __name__ == "__main__":
    sys.exit(main())
