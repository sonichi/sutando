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
import re
import subprocess
import sys
from pathlib import Path


# A check is green only if it says so. Naming the failures instead lets any
# conclusion GitHub adds later default to green, which is the wrong direction.
_GREEN_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
_GREEN_STATES = frozenset({"SUCCESS"})
_RUNNING_STATUSES = frozenset({"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED"})
_RUNNING_STATES = frozenset({"PENDING", "EXPECTED"})


def _check_state(c) -> str:
    """Check runs carry status+conclusion, commit statuses carry state; anything
    unrecognised is failing so an unknown value never reads as mergeable."""
    if c.get("status") in _RUNNING_STATUSES or c.get("state") in _RUNNING_STATES:
        return "pending"
    if "conclusion" in c and c.get("conclusion") is not None:
        return "green" if c["conclusion"] in _GREEN_CONCLUSIONS else "failing"
    if "state" in c and c.get("state") is not None:
        return "green" if c["state"] in _GREEN_STATES else "failing"
    return "failing"


def _ci_state(rollup) -> str:
    """Collapse a statusCheckRollup list into one of green/pending/failing/none."""
    rc = rollup or []
    if not rc:
        return "none"
    states = [_check_state(c) for c in rc]
    if "failing" in states:
        return "failing"
    if "pending" in states:
        return "pending"
    return "green"


STAND_RE = re.compile(r"^\s*Stand:\s*(.+?)\s*$", re.M)


def _stands(pr: dict) -> list:
    """Distinct `Stand:` trailer values across a PR's commits, sorted.

    This is the ONLY signal that separates the agents. Several agents commit
    through the SAME GitHub account, so `author.login` cannot tell them apart --
    it collapses every one of them (and the human whose account it is) into one
    identity.
    """
    seen = set()
    for c in pr.get("commits") or []:
        seen.update(m.strip() for m in STAND_RE.findall(c.get("messageBody") or "") if m.strip())
    return sorted(seen)


# Sentinel key set by _attach_commits when the trailer fetch could not be made.
# It has to be distinguishable from a successful fetch that found no trailer:
# "we looked and there is none" and "we could not look" are different facts, and
# only the first licenses a verdict about who authored the PR.
STANDS_UNAVAILABLE = "_stands_unavailable"


def _principal(stands: list, unavailable: bool = False) -> str:
    """Who authored the PR: the stand, "joint", "unattributed", or "unknown"."""
    if unavailable:
        return "unknown"
    if not stands:
        return "unattributed"
    return stands[0] if len(stands) == 1 else "joint"


def raw_state(prs: list, owner_login: str, stand: str = None) -> list:
    """Objective per-PR state — NO judgement. Sorted by number.

    Each record: number, title, author, stands, principal, is_mine, base, head,
    ci, mergeable,
    review, approvals, approvals_standing. These are facts the agent then judges
    (is it ready? does the owner need it? caveats?).

    TWO approval counts, because they answer different questions and only one of
    them matches what this repo actually enforces:

      approvals           distinct logins whose latest formal review is APPROVED
                          ON THE CURRENT HEAD. Strictly the newer signal.
      approvals_standing  distinct logins whose latest formal review is APPROVED
                          at ANY commit. This is what the branch rules count.

    Both protection surfaces were read live on 2026-08-02:

        classic protection : required_approving_review_count = 0,
                             dismiss_stale_reviews = false
        ruleset "main"     : approvals = 2,
                             dismiss_stale_reviews_on_push = false,
                             require_last_push_approval = false

    `dismiss_stale = false` on both, so a stale approval still counts and
    head-anchoring is STRICTER THAN WHAT ENFORCES. Emitting only the strict count
    is not a conservative choice -- it fails in exactly one direction (false
    not-ready), which is self-consistent and therefore never contradicts itself.
    On 2026-08-02 the same head-anchoring criterion, applied by hand, produced a
    merge-ready count of 8 against a true 15; it was caught only because a peer
    published a different number. That count was not this script's output -- see
    the scope note on `_fetch_prs` -- but it was this field's rule, and an agent
    judging the emitted state has nothing else to judge from.

    `base` (baseRefName) is emitted for the same reason: a STACKED PR targets
    another PR's branch, so every other field can look ready while none of it is
    a statement about main. #2420 reads mergeable + approved against
    `fix/resolved-divider-anchor`, which is #2419's branch.

    Deliberately NOT emitted: `mergeStateStatus`. It looks like the ideal field
    -- GitHub computes it with the ruleset applied -- but it is a cached verdict.
    #2420 reads CLEAN with one approval under a two-approval ruleset because the
    cache was computed against its own base. A field that is right until it
    silently isn't is worse here than the raw inputs the agent can check.
    """
    out = []
    for pr in prs:
        if pr.get("isDraft"):
            continue
        author = (pr.get("author") or {}).get("login", "")
        stands = _stands(pr)
        stands_unavailable = bool(pr.get(STANDS_UNAVAILABLE))
        head = pr.get("headRefOid") or ""
        # Two passes over the same reviews, differing ONLY in whether a review at
        # an older commit is admitted. Kept as one loop with a flag so the two
        # counts can never drift apart in their tie-breaking or state handling.
        def _approvers(head_only: bool) -> set:
            latest_formal_review = {}
            for index, review in enumerate(pr.get("reviews") or []):
                if head_only and (review.get("commit") or {}).get("oid") != head:
                    continue
                state = review.get("state")
                if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                    continue
                login = (review.get("author") or {}).get("login")
                if not login:
                    continue
                order = (review.get("submittedAt") or "", index)
                if login not in latest_formal_review or order >= latest_formal_review[login][0]:
                    latest_formal_review[login] = (order, state)
            return {
                login
                for login, (_, state) in latest_formal_review.items()
                if state == "APPROVED"
            }

        out.append({
            "number": pr.get("number"),
            "title": pr.get("title", ""),
            "author": author,
            "stands": stands,
            "principal": _principal(stands, stands_unavailable),
            # `is_mine` is trailer-derived, NOT `author == owner_login`. The old
            # form was true for EVERY agent sharing the account, so a field named
            # "mine" actually answered "is this the shared login?" -- which read
            # as "the owner's PR" to one consumer and "my PR" to another, and was
            # wrong for both. None when no --stand is supplied: unknown beats a
            # confident guess.
            # None when no --stand was supplied, AND when the fetch failed: a
            # transient gh/GraphQL error must not silently relabel this agent's
            # own PRs as someone else's. Reporting False there would be the very
            # defect this change exists to remove -- a confident wrong identity.
            "is_mine": None if (stands_unavailable or not stand) else (stand in stands),
            "base": pr.get("baseRefName") or "",
            "head": head,
            "ci": _ci_state(pr.get("statusCheckRollup")),
            "mergeable": pr.get("mergeable") or "UNKNOWN",
            "review": pr.get("reviewDecision") or "none",
            "approvals": len(_approvers(head_only=True)),
            "approvals_standing": len(_approvers(head_only=False)),
        })
    out.sort(key=lambda x: x["number"])
    return out


def state_hash(state: list) -> str:
    """Stable hash of the objective set. Changes when a PR appears/disappears or
    any actionable field (base/head/ci/mergeable/review/approvals/
    approvals_standing) flips; a title edit does not refire.

    `approvals_standing` is in the key for a reason the head-anchored count
    cannot cover: a reviewer converting CHANGES_REQUESTED to APPROVED at an
    OLDER commit moves the enforced gate without moving `approvals`, so before
    this it did not refire and the agent was never woken for a PR that had just
    become mergeable."""
    key = [[s["number"], s["principal"], s["base"], s["head"], s["ci"], s["mergeable"],
            s["review"], s["approvals"], s["approvals_standing"]]
           for s in state]
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]


def fetch_argv(repo: str, owner_login: str) -> list:
    """The exact `gh` command the fetch runs.

    Extracted so `scope_descriptor()` can DERIVE the payload's coverage claim from
    the real argv instead of restating it in prose. A hand-written scope string is
    the thing it is describing plus a chance to be wrong: widen the fetch, forget
    the string, and the payload now asserts a filter the code no longer applies.
    """
    # SCOPE NOTE: `--author owner_login` means this only ever sees the owner's OWN
    # PRs (24 of 116 open on 2026-08-02). Peer PRs where the owner's approval is
    # the thing unblocking a merge are not fetched at all, so they cannot appear
    # in any digest built from this state. Left alone deliberately -- widening the
    # fetch is a scope decision, not a field-completeness fix, and belongs in its
    # own change (issue #2643).
    return [
        "gh", "pr", "list", "--repo", repo, "--state", "open",
        "--author", owner_login, "--limit", "1000",
        "--json", "number,title,author,baseRefName,headRefOid,mergeable,reviewDecision,statusCheckRollup,isDraft,reviews",
    ]


def scope_descriptor(repo: str, owner_login: str, record_count: int = None,
                     fetched_count: int = None) -> dict:
    """Name the emitted population precisely, from the WHOLE pipeline.

    Why this exists: the payload carries counts, per-PR CI, approvals and merge
    state, and nothing in it marks the population as partial. A consumer that
    builds a digest from it therefore reads "31 open" as a repository total. That
    happened on 2026-08-04 -- the figure was the owner-authored subset of ~100
    non-draft PRs, and the SCOPE NOTE explaining why lives in this file, which the
    consumer never sees.

    Three things narrow the population, and the descriptor is only honest if it
    reports all three (@john-the-dev on #2645 -- the first version read ONLY the
    `--author` flag, so with the filter removed it certified
    `is_repo_total: true, excludes: "nothing"` while `raw_state()` still dropped
    every draft and the fetch still capped at `--limit`. That is the same
    completeness-metadata-disagrees-with-the-data-path defect this block exists
    to remove, one level up):

      1. `--author` on the fetch (may be absent)
      2. `raw_state()` drops drafts UNCONDITIONALLY -- so the payload is NEVER a
         repository total, with or without the author filter
      3. `--limit` is a ceiling; at exactly the ceiling, complete and truncated
         are indistinguishable

    `is_repo_total` is gone rather than fixed: no value of it was ever true, so a
    consumer keying on it would be reasoning about a population that cannot
    exist. `population` names the real set and `complete` reports only what the
    record count can actually certify.
    """
    argv = fetch_argv(repo, owner_login)
    author = argv[argv.index("--author") + 1] if "--author" in argv else None
    limit_s = argv[argv.index("--limit") + 1] if "--limit" in argv else None
    try:
        limit = int(limit_s) if limit_s is not None else None
    except (TypeError, ValueError):
        limit = None

    excludes = ["draft PRs (dropped by raw_state, always)"]
    if author:
        excludes.append(
            f"PRs not authored by {author!r} -- including peer PRs where the "
            "owner's approval is the only thing blocking a merge"
        )

    # complete==True is a CERTIFICATION, so it is only ever granted on evidence:
    # a count strictly below the ceiling. Unknown count -> None, never True.
    #
    # The count compared against the ceiling must be the PRE-filter FETCHED count
    # (@john-the-dev's second blocker on #2645). The ceiling applies to
    # `_fetch_prs()`; `raw_state()` then drops drafts, so the emitted count is
    # strictly smaller. Certifying off the emitted count means one dropped draft
    # at a truncated fetch reads as complete:
    #
    #     fetched=1000 (== ceiling, truncated)  ->  emitted=999  ->  "below the
    #     1000 ceiling"  ->  complete=True, on a population GitHub had cut off.
    #
    # Exactly the defect this descriptor exists to remove, reintroduced by
    # measuring the wrong side of the filter. `record_count` stays in the payload
    # as the emitted size — it is what the consumer actually received — but it
    # never decides completeness.
    ceiling_count = fetched_count if fetched_count is not None else record_count
    if ceiling_count is None or limit is None:
        complete = None
        why = "fetched count or fetch ceiling unknown — completeness not certified"
    elif ceiling_count >= limit:
        complete = False
        why = (f"fetch returned {ceiling_count} at a {limit} ceiling — complete "
               "and truncated are indistinguishable here")
    else:
        complete = True
        why = f"fetch returned {ceiling_count}, below the {limit} ceiling"

    return {
        "filter": f"author:{author}" if author else "none",
        "population": (
            f"open, non-draft PRs authored by {author!r}"
            if author else "open, non-draft PRs (all authors)"
        ),
        "excludes": excludes,
        "complete": complete,
        "complete_reason": why,
        "record_count": record_count,
        "fetched_count": ceiling_count,
        "limit": limit,
    }


def _fetch_prs(repo: str, owner_login: str) -> list:  # pragma: no cover — subprocess/gh glue
    cmd = fetch_argv(repo, owner_login)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        print(f"pr-flag: gh failed: {res.stderr[:200]}", file=sys.stderr)
        return []
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return []



_STANDS_Q = """query($owner:String!,$name:String!,$n:Int!,$after:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$n){ commits(first:100, after:$after){
      pageInfo{ hasNextPage endCursor }
      nodes{ commit{ messageBody } }
    } }
  }
}"""

# Defensive ceiling: 100 pages = 10,000 commits. Hitting it means something is
# pathological, and the right answer there is "unknown", never a truncated set
# silently reported as complete -- which is the exact bug this module exists to
# remove, one level up.
_MAX_STAND_PAGES = 100


def _gh_stands_page(owner, name, num, after):  # pragma: no cover - subprocess boundary
    """One page of a PR's commit bodies.

    Returns (ok, nodes, has_next, end_cursor). `ok=False` means we could not
    look -- the caller must fail CLOSED rather than treat it as "found none".
    Kept as its own seam so `_attach_commits` is testable without a network.
    """
    argv = ["gh", "api", "graphql", "-f", "query=" + _STANDS_Q,
            "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"n={num}"]
    if after:
        argv += ["-F", f"after={after}"]
    res = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        print(f"pr-flag: stand fetch failed for #{num}: {res.stderr[:120]}", file=sys.stderr)
        return False, [], False, None
    try:
        conn = json.loads(res.stdout)["data"]["repository"]["pullRequest"]["commits"]
        info = conn.get("pageInfo") or {}
        return True, conn["nodes"], bool(info.get("hasNextPage")), info.get("endCursor")
    except (KeyError, TypeError, json.JSONDecodeError):
        return False, [], False, None


def _attach_commits(repo: str, prs: list) -> list:
    """Populate each PR's `commits` with message bodies only.

    Deliberately NOT folded into the `gh pr list --json` call: asking that query
    for `commits` also pulls each commit's `authors` connection, and at
    --limit 1000 GitHub rejects the whole request ("requesting up to 1,000,000
    possible nodes which exceeds the maximum limit of 500,000") -- which returns
    ZERO PRs, not a partial answer. One narrow query per PR is the cheap,
    total-failure-free shape.
    """
    owner, _, name = repo.partition("/")
    for pr in prs:
        num = pr.get("number")
        if num is None:
            continue
        nodes, after, complete = [], None, False
        for _ in range(_MAX_STAND_PAGES):
            ok, page, has_next, cursor = _gh_stands_page(owner, name, num, after)
            if not ok:
                break
            nodes += page
            if not has_next:
                complete = True
                break
            after = cursor
            if not after:      # hasNextPage true but no cursor -> cannot continue
                break
        if not complete:
            # Could not read the WHOLE history. A truncated read is not a small
            # error here: one missing commit can drop the only Stand trailer and
            # turn "mine" into a confident "unattributed".
            pr["commits"] = []
            pr[STANDS_UNAVAILABLE] = True
            continue
        pr["commits"] = [{"messageBody": n["commit"].get("messageBody") or ""} for n in nodes]
    return prs

def main() -> int:  # pragma: no cover — CLI + gh/state I/O glue; pure logic covered in tests
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="sonichi/sutando")
    ap.add_argument("--owner", default="sonichi", help="GH login whose PRs to fetch (shared by several agents)")
    ap.add_argument("--stand", default=None, help="this agent's Stand trailer; sets is_mine (null if omitted)")
    ap.add_argument("--emit", action="store_true", help="print raw state JSON when it changed, else NO_CHANGE")
    ap.add_argument("--force", action="store_true", help="always emit (ignore dedup)")
    ap.add_argument("--state-file", default=None)
    args = ap.parse_args()

    # Bound separately so the PRE-filter size is available to scope_descriptor:
    # the `--limit` ceiling applies here, before raw_state() drops drafts.
    fetched = _attach_commits(args.repo, _fetch_prs(args.repo, args.owner))
    state = raw_state(fetched, args.owner, args.stand)
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
    print(json.dumps({
        "hash": h,
        "changed": True,
        # fetched_count is the PRE-filter size: the ceiling applies to the fetch,
        # not to what survives raw_state(). See scope_descriptor().
        "scope": scope_descriptor(args.repo, args.owner, record_count=len(state),
                                  fetched_count=len(fetched)),
        "prs": state,
    }, indent=2))
    try:
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps({"hash": h, "count": len(state)}))
    except Exception as e:
        print(f"pr-flag: state write failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
