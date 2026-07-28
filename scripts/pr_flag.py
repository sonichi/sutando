#!/usr/bin/env python3
"""
PR-flag — mechanical state gatherer for the owner's open PRs.

Design (Chi 2026-07-27, "are you using a script to do judgement that should be
done by an agent?" → refactor): this script does ONLY the mechanical, deterministic
part — fetch open PRs, read each one's objective state (CI / mergeable /
reviewDecision / approvals / author), and dedup on a state-hash so nothing wakes
the agent when nothing changed. It makes NO judgement: no "ready", no "held", no
"which PRs need you". That judgement is the AGENT's, done live each cycle from
this raw state — because a script deciding "ready" is structurally blind to
content (the #2339 case: green + approved but with fail-open bugs the script
couldn't see, which a hardcoded rule wrongly called "ready").

Usage:
  python3 scripts/pr_flag.py --emit [--repo R] [--owner L] [--state-file P] [--force]

`--emit` prints the raw per-PR state as JSON **only when the set changed** since
last fire (and records the new hash); prints `NO_CHANGE` and exits 0 otherwise,
so the cron stays quiet and the agent isn't woken for nothing. `--force` always
emits (ignores dedup). Exit 0 always (fail-open; never break a cron).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def _ci_state(rollup) -> str:
    """Collapse a statusCheckRollup list into one of green/pending/failing/none."""
    rc = rollup or []
    if not rc:
        return "none"
    if any((c.get("conclusion") in ("FAILURE", "CANCELLED", "TIMED_OUT")) for c in rc):
        return "failing"
    if any((c.get("status") in ("IN_PROGRESS", "QUEUED", "PENDING")) for c in rc):
        return "pending"
    return "green"


def raw_state(prs: list, owner_login: str) -> list:
    """Objective per-PR state — NO judgement. Sorted by number.

    Each record: number, title, author, is_mine, ci, mergeable, review, approvals.
    `approvals` = count of distinct logins whose review state is APPROVED. These
    are facts the agent then judges (is it ready? does the owner need it? caveats?).
    """
    out = []
    for pr in prs:
        if pr.get("isDraft"):
            continue
        author = (pr.get("author") or {}).get("login", "")
        approvers = {
            (r.get("author") or {}).get("login")
            for r in (pr.get("reviews") or [])
            if r.get("state") == "APPROVED"
        }
        approvers.discard(None)
        out.append({
            "number": pr.get("number"),
            "title": pr.get("title", ""),
            "author": author,
            "is_mine": author == owner_login,
            "ci": _ci_state(pr.get("statusCheckRollup")),
            "mergeable": pr.get("mergeable") or "UNKNOWN",
            "review": pr.get("reviewDecision") or "none",
            "approvals": len(approvers),
        })
    out.sort(key=lambda x: x["number"])
    return out


def state_hash(state: list) -> str:
    """Stable hash of the objective set. Changes when a PR appears/disappears or
    any actionable field (ci/mergeable/review/approvals) flips; a title edit does
    not refire."""
    key = [[s["number"], s["is_mine"], s["ci"], s["mergeable"], s["review"], s["approvals"]]
           for s in state]
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]


def _fetch_prs(repo: str) -> list:  # pragma: no cover — subprocess/gh glue
    cmd = [
        "gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "50",
        "--json", "number,title,author,mergeable,reviewDecision,statusCheckRollup,isDraft,reviews",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        print(f"pr-flag: gh failed: {res.stderr[:200]}", file=sys.stderr)
        return []
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return []


def main() -> int:  # pragma: no cover — CLI + gh/state I/O glue; pure logic covered in tests
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="sonichi/sutando")
    ap.add_argument("--owner", default="sonichi", help="GH login whose authored PRs are 'mine'")
    ap.add_argument("--emit", action="store_true", help="print raw state JSON when it changed, else NO_CHANGE")
    ap.add_argument("--force", action="store_true", help="always emit (ignore dedup)")
    ap.add_argument("--state-file", default=None)
    args = ap.parse_args()

    state = raw_state(_fetch_prs(args.repo), args.owner)
    h = state_hash(state)

    # dedup: resolve the stored-hash file the same way every reader does
    sf = Path(args.state_file) if args.state_file else None
    if sf is None:
        ws = ""
        try:
            ws = subprocess.run(["bash", "scripts/sutando-config.sh", "workspace"],
                                capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            ws = ""
        sf = Path(ws) / "state" / "pr-flag-state.json" if ws else Path("state/pr-flag-state.json")
    prev = ""
    try:
        prev = json.loads(sf.read_text()).get("hash", "")
    except Exception:
        prev = ""

    if h == prev and not args.force:
        print("NO_CHANGE")
        return 0

    # emit the objective state for the AGENT to judge, then record the hash
    print(json.dumps({"hash": h, "changed": True, "prs": state}, indent=2))
    try:
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps({"hash": h, "count": len(state)}))
    except Exception as e:
        print(f"pr-flag: state write failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
