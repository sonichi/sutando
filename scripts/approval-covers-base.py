#!/usr/bin/env python3
"""Does each approval cover the tree that would actually merge?

An approval is normally checked against the PR head. That misses the case where
the BASE moved under the very files the PR edits: the PR's own commits are
unchanged, the head may only be a base merge, and the approval can even be
re-stamped onto it — yet nobody has read the combination that will land.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

QUALIFYING = ("write", "admin", "maintain")


def gh(args: list[str]) -> object:
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"gh failed: {' '.join(args[:3])}: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def refresh_base(repo_dir: str, remote: str, branch: str) -> bool:
    """True when `remote/branch` is now up to date.

    Fails CLOSED: the API calls here already raise on failure, and a fetch that
    fails silently is the one error that makes the gate answer "covered".
    """
    r = subprocess.run(["git", "-C", repo_dir, "fetch", remote, branch, "-q"],
                       capture_output=True, text=True)
    return r.returncode == 0


def base_touched_since(repo_dir: str, base_ref: str, since: str,
                       files: list[str]) -> list[str]:
    """Files in `files` that `base_ref` changed after `since`.

    Time, not commit_id: a re-stamped review keeps its submitted_at but points at
    a head nobody re-read, so commit_id cannot answer this and timestamps can.
    """
    if not files:
        return []
    out = subprocess.run(
        ["git", "-C", repo_dir, "log", f"--since={since}", "--name-only",
         "--pretty=format:", base_ref, "--"] + files,
        capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"git log failed: {out.stderr.strip()[:200]}")
    return sorted({ln.strip() for ln in out.stdout.splitlines() if ln.strip()})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pr", type=int)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--no-fetch", action="store_true",
                    help="skip the base refresh; you are asserting it is current")
    a = ap.parse_args(argv)

    pr = gh(["pr", "view", str(a.pr), "--repo", a.repo, "--json",
             "baseRefName,files,state"])
    if pr["state"] != "OPEN":
        print(f"#{a.pr} is {pr['state']} — nothing to gate")
        return 0
    files = [f["path"] for f in pr["files"]]
    base = f"{a.remote}/{pr['baseRefName']}"
    if not a.no_fetch and not refresh_base(a.repo_dir, a.remote, pr["baseRefName"]):
        print(f"REFUSING: could not refresh {base}. A stale base has no commits since "
              f"any approval, so every approval would read as covering — the answer "
              f"this gate exists to distrust. Fetch and retry, or pass --no-fetch if "
              f"you have already refreshed it.", file=sys.stderr)
        return 3

    reviews = gh(["api", f"repos/{a.repo}/pulls/{a.pr}/reviews", "--paginate"])
    latest: dict = {}
    for r in reviews or []:
        login = (r.get("user") or {}).get("login")
        if login and r.get("state") in ("APPROVED", "CHANGES_REQUESTED"):
            latest[login] = r

    covered = uncovered = 0
    print(f"#{a.pr} base={base}  files={len(files)}")
    for login, r in sorted(latest.items()):
        if r["state"] != "APPROVED":
            print(f"  {login:20s} {r['state']} — not an approval")
            continue
        perm = (gh(["api", f"repos/{a.repo}/collaborators/{login}/permission"])
                or {}).get("permission", "?")
        if perm not in QUALIFYING:
            print(f"  {login:20s} APPROVED perm={perm} — does not count toward the gate")
            continue
        moved = base_touched_since(a.repo_dir, base, r["submitted_at"], files)
        if moved:
            uncovered += 1
            print(f"  {login:20s} APPROVED {r['submitted_at']} perm={perm}")
            print(f"      UNCOVERED — base moved these PR files since: {', '.join(moved)}")
        else:
            covered += 1
            print(f"  {login:20s} APPROVED {r['submitted_at']} perm={perm}  covers the tree")

    print(f"\nqualifying approvals covering the merge tree: {covered}"
          f"   uncovered: {uncovered}")
    if uncovered:
        print("NOT READY: an approval that predates a base change to this PR's own "
              "files has not read the combination that would land.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
