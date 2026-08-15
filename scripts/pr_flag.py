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


def _mergeable_key(row: dict) -> str:
    """Cache key for a carried mergeable value.

    Scoped to the REVISION, not the PR: a force-push or retarget parks the field
    at UNKNOWN, and a number-only key would attach the old revision's MERGEABLE
    to the new one -- suppressing exactly the wake-up that re-evaluation needs.
    """
    return f"{row.get('number')}@{row.get('head') or ''}#{row.get('base') or ''}"


def carry_unknown_mergeable(state: list, previous: dict) -> list:
    """Replace UNKNOWN mergeable with the last value seen for THIS revision.

    GitHub's lazy recomputation parks the field at UNKNOWN; carrying the previous
    value keeps that churn out of the hash while leaving a real
    CONFLICTING/MERGEABLE transition fully visible.
    """
    out = []
    for s in state:
        row = dict(s)
        if row.get("mergeable") == "UNKNOWN":
            # A miss (new revision, or a pre-scoping number-keyed state file)
            # leaves UNKNOWN in place -- the conservative direction.
            prior = (previous or {}).get(_mergeable_key(row))
            if prior:
                row["mergeable"] = prior
        out.append(row)
    return out


def mergeable_map(state: list) -> dict:
    """Per-REVISION mergeable, for the next run to carry forward."""
    return {_mergeable_key(s): s.get("mergeable") for s in state
            if s.get("mergeable") and s.get("mergeable") != "UNKNOWN"}


def state_hash(state: list) -> str:
    """Stable hash of the objective set. Changes when a PR appears/disappears or
    any actionable field (base/head/ci/review/approvals/
    approvals_standing) flips; a title edit does not refire.

    `approvals_standing` is in the key for a reason the head-anchored count
    cannot cover: a reviewer converting CHANGES_REQUESTED to APPROVED at an
    OLDER commit moves the enforced gate without moving `approvals`, so before
    this it did not refire and the agent was never woken for a PR that had just
    become mergeable.

    `mergeable` is included but must be normalized by `carry_unknown_mergeable`
    first: GitHub parks it at UNKNOWN while recomputing, and that churn is not a
    state change. A real CONFLICTING/MERGEABLE flip has no other carrier -- the
    target branch can advance with head, base name, ci, reviews and approvals all
    unchanged -- so excluding the field entirely would drop actionable news.
    """
    key = [[s["number"], s["principal"], s["base"], s["head"], s["ci"],
            s["mergeable"], s["review"], s["approvals"], s["approvals_standing"]]
           for s in state]
    return hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:12]


def fetch_argv(repo: str, owner_login: str) -> list:
    """The exact `gh` command the fetch runs.

    Extracted so `scope_descriptor()` can DERIVE the payload's coverage claim from
    the real argv instead of restating it in prose. A hand-written scope string is
    the thing it is describing plus a chance to be wrong: widen the fetch, forget
    the string, and the payload now asserts a filter the code no longer applies.
    """
    # Stage 2 of two. Stays --author-scoped because these heavy fields 504 when
    # requested repo-wide; discovery_argv covers the rest of the population.
    return [
        "gh", "pr", "list", "--repo", repo, "--state", "open",
        "--author", owner_login, "--limit", "1000",
        "--json", "number,title,author,baseRefName,headRefOid,mergeable,reviewDecision,statusCheckRollup,isDraft,reviews",
    ]


def discovery_argv(repo: str) -> list:
    """Stage 1: every open PR, light fields only, no author filter.
    Omits statusCheckRollup and reviews -- the two fields that 504 repo-wide."""
    return [
        "gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "1000",
        "--json", "number,author,isDraft,mergeable,reviewDecision,baseRefName,headRefOid",
    ]


def peer_candidates(discovered: list, owner_login: str) -> list:
    """Peer PRs whose merge the owner could still unblock -- stage 2's input.

    Prunes drafts and peers already carrying CHANGES_REQUESTED (their author must
    clear the review first). Never prunes on APPROVED: that is the case an owner
    action most often unblocks.
    """
    out = []
    for pr in discovered:
        if pr.get("isDraft"):
            continue
        author = ((pr.get("author") or {}).get("login") or "")
        if author == owner_login:
            continue                       # fetch_argv already covers these
        if pr.get("reviewDecision") == "CHANGES_REQUESTED":
            continue
        out.append(pr["number"])
    return out


def read_prior_mergeable(sf) -> dict:
    """Prior mergeable map, fail-open. Inline, this swallowed only decode/OS
    errors: valid JSON that is not an object reached .get and raised."""
    if sf is None or not sf.exists():
        return {}
    try:
        doc = json.loads(sf.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    got = doc.get("mergeable") or {}
    return got if isinstance(got, dict) else {}


def _argv_limit(argv: list):
    """The `--limit` ceiling an argv carries, or None."""
    if "--limit" not in argv:
        return None
    try:
        return int(argv[argv.index("--limit") + 1])
    except (IndexError, TypeError, ValueError):
        return None


def scope_descriptor(repo: str, owner_login: str, record_count: int = None,
                     fetched_count: int = None, peer_stage: dict = None) -> dict:
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
    limit = _argv_limit(argv)

    # A peer stage that could not run is NOT a widened population. Treat it as
    # owner-only so the payload never claims coverage it does not have.
    peer_ok = bool(peer_stage) and peer_stage.get("discovery_ok", True)
    excludes = ["draft PRs (dropped by raw_state, always)"]
    if peer_stage and not peer_ok:
        excludes.append(
            "peer PRs -- the stage-1 discovery fetch FAILED, so the peer half is "
            "UNKNOWN this fire, not empty"
        )
    if peer_stage and not peer_stage.get("owner_ok", True):
        excludes.append(
            "the owner-authored fetch FAILED -- owner rows are UNKNOWN, not absent"
        )
    if author and not peer_ok:
        excludes.append(
            f"PRs not authored by {author!r} -- including peer PRs where the "
            "owner's approval is the only thing blocking a merge"
        )
    elif author and peer_ok:
        # The peer stage restores the peer half, but its own prune is still a
        # real exclusion and must be declared.
        excludes.append(
            "peer PRs already carrying CHANGES_REQUESTED (pruned before stage 2: "
            "the author must clear the review before any approval can matter)"
        )
        if peer_stage.get("failed"):
            excludes.append(
                f"{peer_stage['failed']} peer PR(s) whose stage-2 fetch FAILED -- "
                "absent from this payload but NOT known to be uninteresting"
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
    # TWO fetches, TWO ceilings. Comparing a combined count to the owner limit
    # certifies neither: discovery can sit at its own ceiling while the sum stays
    # under the owner's. Every stage that ran must clear its OWN limit.
    stages = [("owner fetch", fetched_count if fetched_count is not None
               else record_count, limit)]
    if peer_ok and peer_stage is not None:
        stages.append(("discovery", peer_stage.get("discovered"),
                       _argv_limit(discovery_argv(repo))))

    unknown = [n for n, c, l in stages if c is None or l is None]
    at_ceiling = [(n, c, l) for n, c, l in stages
                  if c is not None and l is not None and c >= l]
    if unknown:
        complete = None
        why = (f"{', '.join(unknown)} count or ceiling unknown — completeness "
               "not certified")
    elif at_ceiling:
        complete = False
        why = "; ".join(f"{n} returned {c} at a {l} ceiling — complete and "
                        f"truncated are indistinguishable here"
                        for n, c, l in at_ceiling)
    else:
        complete = True
        why = "; ".join(f"{n} returned {c}, below the {l} ceiling"
                        for n, c, l in stages)
    ceiling_count = stages[0][1]

    if peer_stage and (not peer_ok or peer_stage.get("failed")
                       or not peer_stage.get("owner_ok", True)):
        complete = False
        why = "a fetch failed; population is uncertified"
    return {
        "filter": ("author:{} + peer stage".format(author) if (author and peer_ok)
                   else (f"author:{author}" if author else "none")),
        "population": (
            (f"open, non-draft PRs authored by {author!r}, PLUS open non-draft peer "
             "PRs without an open CHANGES_REQUESTED")
            if (author and peer_ok)
            else (f"open, non-draft PRs authored by {author!r}"
                  if author else "open, non-draft PRs (all authors)")
        ),
        "excludes": excludes,
        "complete": complete,
        "complete_reason": why,
        "record_count": record_count,
        "fetched_count": ceiling_count,
        "limit": limit,
    }


def _fetch_discovered(repo: str):  # pragma: no cover — subprocess/gh glue
    """(ok, rows). `ok` is False on ANY failure so an outage can never be
    serialized as a genuinely empty repository."""
    res = subprocess.run(discovery_argv(repo), capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        print(f"pr-flag: discovery gh failed: {res.stderr[:200]}", file=sys.stderr)
        return False, []
    try:
        return True, json.loads(res.stdout)
    except json.JSONDecodeError:
        print("pr-flag: discovery returned unparseable JSON", file=sys.stderr)
        return False, []


def _fetch_peer_pr(repo: str, number: int) -> dict:  # pragma: no cover — subprocess/gh glue
    """Heavy fields for ONE peer PR. Per-PR because the repo-wide form 504s."""
    res = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json",
         "number,title,author,baseRefName,headRefOid,mergeable,reviewDecision,"
         "statusCheckRollup,isDraft,reviews"],
        capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        print(f"pr-flag: peer #{number} gh failed: {res.stderr[:120]}", file=sys.stderr)
        return {}
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {}


def _fetch_prs(repo: str, owner_login: str):  # pragma: no cover — subprocess/gh glue
    """(ok, rows) — same contract as _fetch_discovered."""
    cmd = fetch_argv(repo, owner_login)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        print(f"pr-flag: gh failed: {res.stderr[:200]}", file=sys.stderr)
        return False, []
    try:
        return True, json.loads(res.stdout)
    except json.JSONDecodeError:
        print("pr-flag: owner fetch returned unparseable JSON", file=sys.stderr)
        return False, []



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
    own_ok, own = _fetch_prs(args.repo, args.owner)
    disc_ok, discovered = _fetch_discovered(args.repo)
    candidates = peer_candidates(discovered, args.owner)
    peers, peer_failures = [], 0
    for number in candidates:
        pr = _fetch_peer_pr(args.repo, number)
        if pr:
            peers.append(pr)
        else:
            peer_failures += 1
    if not disc_ok:
        print("pr-flag: discovery FAILED — the peer half is unknown, not empty; "
              "population is uncertified this fire", file=sys.stderr)
    fetched = _attach_commits(args.repo, own + peers)
    state = raw_state(fetched, args.owner, args.stand)

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

    prior = read_prior_mergeable(sf)
    state = carry_unknown_mergeable(state, prior)
    h = state_hash(state)
    # A population we could not certify must not overwrite the last good hash:
    # doing so lets the next healthy fire read as NO_CHANGE and stay silent.
    certified = own_ok and disc_ok and peer_failures == 0

    prev = ""
    try:
        prev = json.loads(sf.read_text()).get("hash", "")
    except Exception:
        prev = ""

    # `certified` gates the fast path: an uncertified run whose surviving rows
    # happen to hash to the last healthy state would otherwise exit silently.
    if h == prev and certified and not args.force:
        print("NO_CHANGE")
        return 0

    # emit the objective state for the AGENT to judge, then record the hash
    print(json.dumps({
        "hash": h,
        "changed": True,
        # fetched_count is the PRE-filter size: the ceiling applies to the fetch,
        # not to what survives raw_state(). See scope_descriptor().
        "scope": scope_descriptor(args.repo, args.owner, record_count=len(state),
                                  peer_stage={"discovered": len(discovered),
                                              "candidates": len(candidates),
                                              "fetched": len(peers),
                                              "failed": peer_failures,
                                              "discovery_ok": disc_ok,
                                              "owner_ok": own_ok},
                                  # OWNER-stage size: the owner ceiling applies
                                  # here. Discovery certifies its own, above.
                                  fetched_count=len(own)),
        "prs": state,
    }, indent=2))
    try:
        sf.parent.mkdir(parents=True, exist_ok=True)
        if certified:
            sf.write_text(json.dumps({"hash": h, "count": len(state),
                                      "mergeable": mergeable_map(state)}))
        else:
            print("pr-flag: population uncertified — hash NOT recorded, so the "
                  "next healthy fire still emits", file=sys.stderr)
    except Exception as e:
        print(f"pr-flag: state write failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
