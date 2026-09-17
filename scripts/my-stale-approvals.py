#!/usr/bin/env python3
"""Find approvals of yours that a later push left describing code nobody read.

A stale CHANGES_REQUESTED advertises itself: the PR is blocked and the author
says so. A stale APPROVAL is silent by construction, and on a repo with
`dismiss_stale_reviews_on_push: false` it keeps COUNTING toward the merge bar —
so it does not delay a PR, it authorises a head you never saw.

Two measurements this makes on your behalf, because both were got wrong by hand:

1. SCOPE. "PRs with a review requested of me" is not "PRs I approved". Measured
   on one repo: 19 stale approvals, of which 3 were in the review-request list.
   A scan filtered by review-requests reported 16% of the population as the total.

2. STALENESS. Compare against the newest AUTHORED commit, split by parent count.
   A base merge moves the head without anyone writing code, so comparing against
   the head alone marks approvals stale that are not.

DECISIVE means your unread approval is part of a merge that can happen soon:
open, not draft, nobody else holding CHANGES_REQUESTED, and the tally already at
or one short of the bar. It does NOT mean re-reviewing carries the PR by itself —
at exactly one short, your vote already counts and someone else's is the missing
one. Both cells are worth re-reading first, for the same reason: a merge lands on
your stale tick either now or on the next approval. The rest are blocked by
someone else regardless.

Only COLLABORATOR/MEMBER/OWNER approvals count at the gate, so only those are
counted toward the bar.

Usage:
  python3 scripts/my-stale-approvals.py [--repo OWNER/NAME] [--login LOGIN]
                                        [--bar N] [--decisive-only] [--json]
Exit: 0 always (a report, not a gate) unless --fail-on-decisive is passed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

GATE_ASSOCIATIONS = ("COLLABORATOR", "MEMBER", "OWNER")


def gh_json(*args, default=None):
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        return default
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return default


def current_login() -> str | None:
    d = gh_json("api", "user", "--jq", '{login: .login}')
    return (d or {}).get("login")


PR_LIST_LIMIT = 200


def repos_reviewed(login: str):
    """Every repo holding an open PR this login reviewed, newest first.

    A single-repo scan reported as an exposure is the defect this exists to
    close: measured, one login had 60 of 98 reviewed PRs in its main repo.
    """
    q = f"is:pr is:open reviewed-by:{login}"
    # --slurp is required with --paginate: gh otherwise emits one JSON object
    # per page, json.loads rejects it, and a 2-page search reads as empty.
    pages = gh_json("api", "--paginate", "--slurp",
                    f"search/issues?q={q.replace(' ', '+')}&per_page=100", default=None)
    if pages is None:
        # A failed search and an empty one must not be the same value.
        return [], {"total": None, "seen": 0, "ok": False}
    if isinstance(pages, dict):
        pages = [pages]
    seen, items, total = [], 0, 0
    for page in pages:
        total = max(total, page.get("total_count", 0))
        for item in page.get("items", []):
            items += 1
            repo = item.get("repository_url", "").split("/repos/", 1)[-1]
            if repo and repo not in seen:
                seen.append(repo)
    # A repo appearing only in the unreached remainder is silently out of scope,
    # which is the "single-repo scan reported as an exposure" defect above.
    return seen, {"total": total, "seen": items, "ok": True}


def newest_authored(repo: str, number: int) -> str:
    """Newest commit someone actually wrote. Merge commits have 2+ parents and
    move the head without any review-worthy change, so they must not count."""
    commits = gh_json("api", f"repos/{repo}/pulls/{number}/commits", "--paginate", default=[]) or []
    dates = [c["commit"]["committer"]["date"] for c in commits if len(c.get("parents", [])) == 1]
    return max(dates, default="")


def latest_per_author(reviews):
    """GitHub counts only each author's latest review, and a DISMISSED one
    counts as no stance — it does not resurrect that author's previous review.
    Skipping DISMISSED instead of clearing resurrects it in both polarities:
    a dismissed approval reads as authorising unread code, and a dismissed
    CHANGES_REQUESTED reads as a block GitHub no longer holds."""
    out = {}
    for r in reviews:
        state = r.get("state")
        if state in ("APPROVED", "CHANGES_REQUESTED"):
            out[r["user"]["login"]] = r
        elif state == "DISMISSED":
            out.pop(r["user"]["login"], None)
    return out


def scan(repo: str, login: str, bar: int):
    prs = gh_json("pr", "list", "--repo", repo, "--state", "open",
                  "--limit", str(PR_LIST_LIMIT),
                  "--json", "number,title,author,isDraft,baseRefName", default=[]) or []
    rows, reach = [], 0
    for p in prs:
        if p["isDraft"] or p["author"]["login"] == login:
            continue
        reviews = gh_json("api", f"repos/{repo}/pulls/{p['number']}/reviews", "--paginate", default=[]) or []
        latest = latest_per_author(reviews)
        mine = latest.get(login)
        if not mine:
            continue
        # Reach: a 0 from a repo holding none of your approvals is untestable,
        # not a measurement. reviewed-by: counts COMMENTED; this does not.
        reach += 1
        if mine["state"] != "APPROVED":
            continue
        cutoff = newest_authored(repo, p["number"])
        if not cutoff or mine["submitted_at"] > cutoff:
            continue
        commits = gh_json("api", f"repos/{repo}/pulls/{p['number']}/commits", "--paginate", default=[]) or []
        after = [c for c in commits
                 if len(c.get("parents", [])) == 1
                 and c["commit"]["committer"]["date"] > mine["submitted_at"]]
        # A conflict-resolving merge carries hand-typed hunks REST cannot show,
        # so it is an unknown to report, not a no-op to drop.
        merges = [c for c in commits
                  if len(c.get("parents", [])) > 1
                  and c["commit"]["committer"]["date"] > mine["submitted_at"]]
        blockers = [u for u, r in latest.items()
                    if r["state"] == "CHANGES_REQUESTED" and u != login]
        qualifying = sum(1 for r in latest.values()
                         if r["state"] == "APPROVED" and r.get("author_association") in GATE_ASSOCIATIONS)
        rows.append({
            "number": p["number"], "title": p["title"], "author": p["author"]["login"],
            "base": p["baseRefName"], "approved_at": mine["submitted_at"],
            "newest_authored": cutoff, "commits_after": len(after),
            "merges_after": len(merges),
            "blocked_by_others": blockers, "qualifying_approvals": qualifying,
            "decisive": not blockers and qualifying >= bar - 1,
        })
    rows.sort(key=lambda r: (not r["decisive"], -r["commits_after"]))
    # At the cap the remainder is unseen, so a 0 from here is not a measurement.
    return rows, reach, len(prs) >= PR_LIST_LIMIT


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None,
                    help="one repo; default scans EVERY repo you have reviewed an open PR in")
    ap.add_argument("--login", default=None, help="defaults to the authenticated gh user")
    ap.add_argument("--bar", type=int, default=2, help="required approving reviews")
    ap.add_argument("--decisive-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fail-on-decisive", action="store_true",
                    help="exit 1 when a decisive stale approval exists")
    a = ap.parse_args()

    login = a.login or current_login()
    if not login:
        print("my-stale-approvals: could not resolve a login (gh api user failed); "
              "pass --login", file=sys.stderr)
        return 2

    if a.repo:
        repos, coverage = [a.repo], None
    else:
        repos, coverage = repos_reviewed(login)
    if not repos:
        print(f"my-stale-approvals: found no repo with an open PR reviewed by {login}",
              file=sys.stderr)
        return 0

    all_rows, scanned = [], []
    for repo in repos:
        rows, reach, capped = scan(repo, login, a.bar)
        for r in rows:
            r["repo"] = repo
        all_rows.extend(rows)
        scanned.append((repo, len(rows), reach, capped))
    all_rows.sort(key=lambda r: (not r["decisive"], -r["commits_after"]))
    shown = [r for r in all_rows if r["decisive"]] if a.decisive_only else all_rows

    if a.json:
        print(json.dumps({"login": login, "bar": a.bar,
                          "coverage": coverage,
                          "scanned": [{"repo": r, "stale": n, "reach": k, "at_pr_limit": c}
                                      for r, n, k, c in scanned],
                          "rows": shown}, indent=2))
        return 1 if a.fail_on_decisive and any(r["decisive"] for r in all_rows) else 0

    decisive = sum(1 for r in all_rows if r["decisive"])
    print(f"{login}: {len(all_rows)} stale approval(s), {decisive} decisive "
          f"(bar={a.bar}) across {len(repos)} repo(s)")
    for r in shown:
        mark = "DECISIVE" if r["decisive"] else "blocked "
        why = (f"blocked by {','.join(r['blocked_by_others'])}" if r["blocked_by_others"]
               else f"{r['qualifying_approvals']}/{a.bar} qualifying")
        # A merge after the approval may carry hand-resolved lines the REST API
        # cannot show, so it prints as an unknown rather than vanishing.
        m = (f", +{r['merges_after']} merge(s), content not checked"
             if r.get("merges_after") else "")
        print(f"  {mark} {r['repo']}#{r['number']:<5} {r['author']:<15} "
              f"{r['commits_after']} commit(s){m} after your {r['approved_at'][:10]}  "
              f"[{why}] base={r['base'][:12]}  {r['title'][:40]}")
    # Scope is part of the answer: a repo holding none of your reviews cannot
    # produce a meaningful zero, so say which zeros were testable.
    print("  scope:")
    if coverage and not coverage["ok"]:
        print("    repo discovery FAILED (gh search returned nothing usable) — "
              "the repo set below is not a measurement")
    elif coverage and coverage["seen"] < coverage["total"]:
        print(f"    repo discovery reached {coverage['seen']} of {coverage['total']} "
              f"reviewed PRs — a repo appearing only in the other "
              f"{coverage['total'] - coverage['seen']} is NOT in this scan")
    for repo, n, reach, capped in scanned:
        note = "" if reach else "  (no approvals of yours here — this 0 is untestable)"
        if capped:
            note += f"  (hit the {PR_LIST_LIMIT}-PR list cap — the remainder is unseen)"
        print(f"    {repo}: {n} stale, {reach} PR(s) carrying your review{note}")
    if a.fail_on_decisive and decisive:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
