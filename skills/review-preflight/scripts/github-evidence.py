#!/usr/bin/env python3
"""Read-only PR outcome receipt. Unknown evidence is never a successful check.

Usage: python3 github-evidence.py OWNER/REPO PR [--expect-head SHA]
Exit 0: stable receipt (which can be blocked); exit 2: incomplete/stale evidence.
No log scraping, votes, comments, merge, or persistent credential access.
"""
import argparse
import datetime
import json
import re
import subprocess
from typing import Optional


def collect(repo: str, number: int, expected_head: Optional[str] = None, runner=None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or number < 1:
        raise ValueError("expected OWNER/REPO and positive PR number")
    run = runner or (lambda args: subprocess.run(args, capture_output=True, text=True, timeout=25))
    receipt = {"schema": 1, "repository": repo, "pr": number,
               "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "evidence_status": "unknown", "merge_readiness": "unknown", "errors": []}

    def read(args, allowed=(0,)):
        try:
            result = run(["gh"] + args)
            if result.returncode not in allowed:
                raise ValueError("GitHub query failed")
            return json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            receipt["errors"].append("Structured GitHub evidence unavailable; no success inferred")
            return None

    endpoint = f"repos/{repo}/pulls/{number}"
    before = read(["api", endpoint])
    if not isinstance(before, dict) or not isinstance(before.get("head"), dict):
        return receipt
    head = before["head"].get("sha")
    receipt["head_sha"] = head
    checks = read(["pr", "checks", str(number), "--repo", repo, "--required", "--json",
                   "name,state,bucket,link"], allowed=(0, 1, 8))
    after = read(["api", endpoint])
    if not isinstance(after, dict) or (after.get("head") or {}).get("sha") != head:
        receipt["errors"].append("PR head changed during observation; repeat the query")
        return receipt
    if not isinstance(head, str) or not head or (expected_head and head != expected_head):
        receipt["errors"].append("Expected head does not match current PR head")
        return receipt
    # Merge is a server outcome, independent of CI and agent intent.
    receipt["state"] = after.get("state")
    receipt["merged_at"] = after.get("merged_at")
    receipt["merged"] = after.get("merged") is True and bool(after.get("merged_at"))
    if not isinstance(checks, list) or not checks:
        receipt["errors"].append("Required-check set unavailable or empty; readiness is unknown")
        return receipt
    if any(not isinstance(c, dict) or not c.get("name") or not c.get("link") or
           c.get("bucket") not in ("pass", "fail", "pending", "skipping", "cancel") for c in checks):
        receipt["errors"].append("Required-check evidence malformed; readiness is unknown")
        return receipt
    receipt["required_checks"] = checks
    receipt["evidence_status"] = "stable"
    # Duplicate names cannot be silently reduced to a preferred green run.
    names = [c["name"] for c in checks]
    if len(names) != len(set(names)):
        receipt["errors"].append("Ambiguous repeated check names; inspect run/attempt identity")
        return receipt
    receipt["merge_readiness"] = "checks_passed" if all(c["bucket"] == "pass" for c in checks) else "blocked"
    receipt["note"] = "Checks passing does not establish review approval, human authority, package publication, or merge."
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("pr", type=int)
    parser.add_argument("--expect-head")
    args = parser.parse_args()
    receipt = collect(args.repo, args.pr, args.expect_head)
    print(json.dumps(receipt, indent=2))
    return 0 if receipt["evidence_status"] == "stable" and not receipt["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
