#!/usr/bin/env python3
"""
Unit tests for the pure core of scripts/pr_flag.py.

After the 2026-07-27 refactor (Chi: "are you using a script to do judgement that
should be done by an agent?"), the script does ONLY mechanical state-gathering +
dedup — no "ready"/"held"/"needs-you" judgement (that's the agent's, done live).
So these tests cover the objective raw_state fields + the dedup hash; the
gh/state I/O is glue.

Run: python3 tests/pr-flag.test.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("pr_flag", REPO / "scripts" / "pr_flag.py")
pf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pf)


def _pr(number, author, review="", ci="green", mergeable="MERGEABLE", draft=False,
        title="t", head=None, approvers=(), stale_approvers=(), base="main",
        extra_reviews=(), commit_bodies=None):
    head = head or f"head-{number}"
    rollup = []
    if ci == "green":
        rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
    elif ci == "pending":
        rollup = [{"status": "IN_PROGRESS", "conclusion": None}]
    elif ci == "failing":
        rollup = [{"status": "COMPLETED", "conclusion": "FAILURE", "name": "x"}]
    return {
        "number": number, "title": title, "author": {"login": author},
        "commits": [{"messageBody": b} for b in (commit_bodies or [])],
        "reviewDecision": review, "statusCheckRollup": rollup,
        "headRefOid": head, "mergeable": mergeable, "isDraft": draft,
        "baseRefName": base,
        "reviews": [
            *[{"author": {"login": a}, "state": "APPROVED", "commit": {"oid": head}}
              for a in approvers],
            *[{"author": {"login": a}, "state": "APPROVED", "commit": {"oid": "old-head"}}
              for a in stale_approvers],
            *extra_reviews,
        ],
    }


def main() -> int:
    OWNER = "sonichi"

    # _ci_state collapsing (incl. a single failure among greens → failing)
    assert pf._ci_state([]) == "none"
    assert pf._ci_state([{"status": "COMPLETED", "conclusion": "SUCCESS"}]) == "green"
    assert pf._ci_state([{"status": "IN_PROGRESS"}]) == "pending"
    assert pf._ci_state([{"conclusion": "FAILURE"}]) == "failing"
    assert pf._ci_state([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]) == "failing"
    assert pf._ci_state([{"__typename": "StatusContext", "state": "SUCCESS"}]) == "green"
    assert pf._ci_state([{"__typename": "StatusContext", "state": "PENDING"}]) == "pending"
    assert pf._ci_state([{"__typename": "StatusContext", "state": "EXPECTED"}]) == "pending"
    assert pf._ci_state([{"__typename": "StatusContext", "state": "FAILURE"}]) == "failing"
    assert pf._ci_state([{"__typename": "StatusContext", "state": "ERROR"}]) == "failing"
    assert pf._ci_state([
        {"__typename": "CheckRun", "status": "IN_PROGRESS"},
        {"__typename": "StatusContext", "state": "FAILURE"},
    ]) == "failing"
    print("  ok  _ci_state collapses correctly")

    prs = [
        _pr(10, OWNER, ci="green", mergeable="MERGEABLE"),
        _pr(11, OWNER, ci="pending"),
        _pr(12, "peer", review="REVIEW_REQUIRED", ci="green", approvers=["qingyun", "rui"]),
        _pr(13, "peer", review="CHANGES_REQUESTED", ci="failing", approvers=["qingyun", "qingyun"]),
        _pr(14, OWNER, draft=True),  # draft → excluded
    ]
    st = pf.raw_state(prs, OWNER)
    got = {s["number"]: s for s in st}

    # objective fields, NO judgement fields (no court/why/ready/held)
    assert set(got) == {10, 11, 12, 13}, "draft excluded, rest present"
    for s in st:
        assert set(s) == {"number", "title", "author", "stands", "principal", "is_mine",
                          "base", "head", "ci", "mergeable", "review", "approvals",
                          "approvals_standing"}, s
        assert "court" not in s and "why" not in s and "ready" not in s, "script must emit NO judgement: " + str(s)
    print("  ok  raw_state emits objective fields only — no judgement")

    # No --stand supplied -> is_mine is None everywhere (unknown, not a guess).
    assert got[10]["is_mine"] is None and got[10]["ci"] == "green" and got[10]["review"] == "none"
    assert got[11]["is_mine"] is None and got[11]["ci"] == "pending"
    assert got[12]["is_mine"] is None and got[12]["approvals"] == 2 and got[12]["review"] == "REVIEW_REQUIRED"
    # distinct approvers only: qingyun approving twice counts once
    assert got[13]["approvals"] == 1 and got[13]["ci"] == "failing"
    print("  ok  is_mine is None without --stand; ci / review / distinct-approvals correct")

    # sorted by number
    assert [s["number"] for s in st] == sorted(s["number"] for s in st)
    print("  ok  sorted by PR number")

    # dedup hash: stable on title-only change, flips on an actionable field change
    h1 = pf.state_hash(st)
    st_title = pf.raw_state([dict(p, title="different") for p in prs], OWNER)
    assert pf.state_hash(st_title) == h1, "title-only change must NOT refire"
    st_ci = pf.raw_state([(_pr(11, OWNER, ci="green") if p["number"] == 11 else p) for p in prs], OWNER)
    assert pf.state_hash(st_ci) != h1, "a CI flip MUST refire"
    st_appr = pf.raw_state([(_pr(12, "peer", review="REVIEW_REQUIRED", ci="green", approvers=["qingyun"]) if p["number"] == 12 else p) for p in prs], OWNER)
    assert pf.state_hash(st_appr) != h1, "an approvals change MUST refire"
    st_head = pf.raw_state([(_pr(12, "peer", review="REVIEW_REQUIRED", ci="green",
                                head="new-head", approvers=["qingyun", "rui"])
                             if p["number"] == 12 else p) for p in prs], OWNER)
    assert pf.state_hash(st_head) != h1, "a head change MUST refire"
    stale = pf.raw_state([_pr(15, "peer", approvers=["rui"], stale_approvers=["qingyun"])], OWNER)
    assert stale[0]["approvals"] == 1, "stale-head approvals must not be counted"

    effective = _pr(16, "peer")
    effective["reviews"] = [
        # Intentionally not API-ordered: submittedAt determines effective state.
        {"author": {"login": "qingyun"}, "state": "CHANGES_REQUESTED",
         "submittedAt": "2026-07-28T02:00:00Z", "commit": {"oid": "head-16"}},
        {"author": {"login": "rui"}, "state": "APPROVED",
         "submittedAt": "2026-07-28T02:00:00Z", "commit": {"oid": "head-16"}},
        {"author": {"login": "qingyun"}, "state": "APPROVED",
         "submittedAt": "2026-07-28T01:00:00Z", "commit": {"oid": "head-16"}},
        {"author": {"login": "rui"}, "state": "CHANGES_REQUESTED",
         "submittedAt": "2026-07-28T01:00:00Z", "commit": {"oid": "head-16"}},
        {"author": {"login": "john"}, "state": "APPROVED",
         "submittedAt": "2026-07-28T01:00:00Z", "commit": {"oid": "head-16"}},
        {"author": {"login": "john"}, "state": "DISMISSED",
         "submittedAt": "2026-07-28T02:00:00Z", "commit": {"oid": "head-16"}},
        {"author": {"login": "rui"}, "state": "COMMENTED",
         "submittedAt": "2026-07-28T03:00:00Z", "commit": {"oid": "head-16"}},
        {"author": {"login": "stale"}, "state": "APPROVED",
         "submittedAt": "2026-07-28T03:00:00Z", "commit": {"oid": "old-head"}},
    ]
    assert pf.raw_state([effective], OWNER)[0]["approvals"] == 1, (
        "only each reviewer's latest effective current-head formal state counts"
    )
    print("  ok  dedup hash flips on ci / approvals / head; effective current-head approvals counted")

    # ---- the enforced gate needs a SECOND approval count -------------------
    # `approvals` is head-anchored. This repo's rules are not: both surfaces read
    # `dismiss_stale_reviews = false` on 2026-08-02, so a stale approval still
    # counts toward the two required. Emitting only the strict number fails in
    # exactly ONE direction -- false not-ready -- which never contradicts itself,
    # so nothing downstream can notice. It produced a published merge-ready count
    # of 8 against a true 15 that day.
    two_stale = pf.raw_state([_pr(20, "peer", stale_approvers=["a", "b"])], OWNER)[0]
    assert two_stale["approvals"] == 0, "head-anchored count must still ignore stale approvals"
    assert two_stale["approvals_standing"] == 2, (
        "the enforced gate counts stale approvals; without this the PR reads not-ready")
    # Control: the two numbers are NOT merely aliases of each other.
    two_fresh = pf.raw_state([_pr(21, "peer", approvers=["a", "b"])], OWNER)[0]
    assert two_fresh["approvals"] == two_fresh["approvals_standing"] == 2, two_fresh
    none_at_all = pf.raw_state([_pr(22, "peer")], OWNER)[0]
    assert none_at_all["approvals"] == none_at_all["approvals_standing"] == 0, none_at_all
    print("  ok  approvals_standing counts stale approvals; approvals still does not")

    # A CHANGES_REQUESTED that was later converted AT AN OLDER COMMIT must stop
    # counting against the PR -- latest-per-author, not any-CR-ever.
    converted = _pr(23, "peer", stale_approvers=["b"], extra_reviews=[
        {"author": {"login": "a"}, "state": "CHANGES_REQUESTED",
         "submittedAt": "2026-08-02T01:00:00Z", "commit": {"oid": "old-head"}},
        {"author": {"login": "a"}, "state": "APPROVED",
         "submittedAt": "2026-08-02T02:00:00Z", "commit": {"oid": "old-head"}},
    ])
    assert pf.raw_state([converted], OWNER)[0]["approvals_standing"] == 2, (
        "a converted CHANGES_REQUESTED must count as that reviewer's approval")
    still_blocked = _pr(24, "peer", stale_approvers=["b"], extra_reviews=[
        {"author": {"login": "a"}, "state": "APPROVED",
         "submittedAt": "2026-08-02T01:00:00Z", "commit": {"oid": "old-head"}},
        {"author": {"login": "a"}, "state": "CHANGES_REQUESTED",
         "submittedAt": "2026-08-02T02:00:00Z", "commit": {"oid": "old-head"}},
    ])
    assert pf.raw_state([still_blocked], OWNER)[0]["approvals_standing"] == 1, (
        "a LATER changes-request must revoke that reviewer's earlier approval")
    print("  ok  latest-per-author applies to the standing count in both directions")

    # ---- base is emitted, and a stacked PR is distinguishable ---------------
    # #2420 targets #2419's branch, so mergeable + approved says nothing about
    # main. Without `base` in the payload the agent cannot see that at all.
    stacked = pf.raw_state([_pr(25, "peer", approvers=["a", "b"],
                                base="fix/resolved-divider-anchor")], OWNER)[0]
    assert stacked["base"] == "fix/resolved-divider-anchor", stacked
    assert pf.raw_state([_pr(26, "peer")], OWNER)[0]["base"] == "main"
    print("  ok  base (baseRefName) is emitted; a stacked PR is distinguishable from a main one")

    # ---- dedup: the two new fields are actionable, so they must refire ------
    base_h = pf.state_hash(pf.raw_state([_pr(30, "peer", stale_approvers=["a"])], OWNER))
    gained = pf.state_hash(pf.raw_state([_pr(30, "peer", stale_approvers=["a", "b"])], OWNER))
    assert gained != base_h, (
        "a second STANDING approval makes a PR mergeable and must wake the agent; "
        "head-anchored `approvals` does not move here, which is why it was missed")
    rebased = pf.state_hash(pf.raw_state([_pr(30, "peer", stale_approvers=["a"], base="other")], OWNER))
    assert rebased != base_h, "a base change must refire"
    print("  ok  dedup hash flips on approvals_standing and on base")

    # ---- a malformed review node must be IGNORED, not counted, not fatal ----
    # GitHub can return a review whose `author` is null -- a deleted account, or a
    # review left by an app/integration that no longer resolves. There is no login
    # to key a latest-per-author map on, so the row is skipped. Both counts pass
    # through the same skip, so a malformed node cannot inflate one and not the
    # other, and it must not abort state collection for the whole repo either.
    malformed = _pr(40, "peer", approvers=["a"], extra_reviews=[
        {"author": None, "state": "APPROVED",
         "submittedAt": "2026-08-02T03:00:00Z", "commit": {"oid": "head-40"}},
        {"author": {}, "state": "APPROVED",
         "submittedAt": "2026-08-02T03:00:00Z", "commit": {"oid": "old-head"}},
        {"author": {"login": ""}, "state": "CHANGES_REQUESTED",
         "submittedAt": "2026-08-02T04:00:00Z", "commit": {"oid": "head-40"}},
    ])
    got_bad = pf.raw_state([malformed], OWNER)[0]
    assert got_bad["approvals"] == 1, (
        f"an authorless APPROVED must not inflate the head-anchored count: {got_bad}")
    assert got_bad["approvals_standing"] == 1, (
        f"...nor the standing count, which admits older commits: {got_bad}")
    # The empty-login CHANGES_REQUESTED is the discriminating half: if the skip were
    # keyed on `author is None` rather than on a falsy login, it would land in the
    # map under "" and silently revoke a real approval.
    # ...and the record is still COMPLETE: a malformed node must not abort or
    # truncate state collection, which is the "not fatal" half of the claim.
    assert set(got_bad) == {"number", "title", "author", "stands", "principal", "is_mine",
                            "base", "head", "ci", "mergeable", "review", "approvals",
                            "approvals_standing"}, got_bad
    # Control: the same fixture with a REAL login does move the counts, so the
    # assertions above are about the missing login and not about the fixture.
    named = _pr(41, "peer", approvers=["a"], extra_reviews=[
        {"author": {"login": "b"}, "state": "APPROVED",
         "submittedAt": "2026-08-02T03:00:00Z", "commit": {"oid": "head-41"}},
    ])
    got_named = pf.raw_state([named], OWNER)[0]
    assert got_named["approvals"] == 2 and got_named["approvals_standing"] == 2, got_named
    print("  ok  a review with no author.login is skipped by BOTH counts, and is not fatal")

    # empty repo → empty state, stable hash
    assert pf.raw_state([], OWNER) == []
    assert pf.state_hash([]) == pf.state_hash([])
    print("  ok  empty repo → empty state")


    # ---- Stand-trailer principal ------------------------------------------
    # THE REGRESSION: several agents commit through ONE GitHub account, so
    # `author.login` cannot separate them. Before this, `is_mine` was
    # `author == owner_login` -> True for every one of these three PRs, which is
    # what made a digest call another agent's work "yours".
    PRO, MINI = "Echo Act IV Pro", "Echo Act IV Mini"
    shared = [
        _pr(20, OWNER, commit_bodies=["body\n\nStand: " + PRO]),
        _pr(21, OWNER, commit_bodies=["body\n\nStand: " + MINI]),
        _pr(22, OWNER, commit_bodies=["a\n\nStand: " + PRO, "b\n\nStand: " + MINI]),
        _pr(23, OWNER, commit_bodies=["no trailer here"]),
    ]
    g = {x["number"]: x for x in pf.raw_state(shared, OWNER, stand=PRO)}
    assert g[20]["principal"] == PRO and g[20]["is_mine"] is True, g[20]
    assert g[21]["principal"] == MINI and g[21]["is_mine"] is False, g[21]
    assert g[22]["principal"] == "joint" and g[22]["stands"] == [MINI, PRO], g[22]
    assert g[22]["is_mine"] is True, "a joint PR IS partly mine"
    assert g[23]["principal"] == "unattributed" and g[23]["is_mine"] is False, g[23]
    # every one of them shares the SAME author login -- that is the whole point
    assert {x["author"] for x in g.values()} == {OWNER}
    print("  ok  same login, different Stand trailers -> distinct principals")

    # viewed as Mini, ownership flips -- proving is_mine tracks the trailer,
    # not the account
    m = {x["number"]: x for x in pf.raw_state(shared, OWNER, stand=MINI)}
    assert m[20]["is_mine"] is False and m[21]["is_mine"] is True
    print("  ok  is_mine follows the stand, not the shared account")

    # trailers dedup across commits and sort
    dup = _pr(24, OWNER, commit_bodies=["x\n\nStand: " + PRO, "y\n\nStand: " + PRO])
    assert pf.raw_state([dup], OWNER)[0]["stands"] == [PRO]
    print("  ok  repeated trailers dedup to one stand")

    # identity moves the dedup hash: same objective state, different author
    a = _pr(30, OWNER, commit_bodies=["m\n\nStand: " + PRO])
    b = _pr(30, OWNER, commit_bodies=["m\n\nStand: " + MINI])
    assert pf.state_hash(pf.raw_state([a], OWNER)) != pf.state_hash(pf.raw_state([b], OWNER))
    print("  ok  a change of principal refires the digest")


    # ---- a FAILED trailer fetch is UNKNOWN, never a verdict ----------------
    # qingyun-wu on #2553: _attach_commits turned any gh/GraphQL failure into
    # commits=[], which read downstream as "no Stand trailer" -> unattributed ->
    # is_mine False. A transient error could therefore relabel this agent's own
    # PRs as someone else's -- a confident wrong identity, which is precisely the
    # defect this module exists to remove.
    failed = _pr(40, OWNER, commit_bodies=[])
    failed[pf.STANDS_UNAVAILABLE] = True
    g = pf.raw_state([failed], OWNER, stand=PRO)[0]
    assert g["principal"] == "unknown", g
    assert g["is_mine"] is None, "a fetch failure must not assert ownership either way"
    assert g["stands"] == [], g
    print("  ok  failed trailer fetch -> principal 'unknown', is_mine None")

    # and it stays distinguishable from a genuine no-trailer PR
    none_found = pf.raw_state([_pr(41, OWNER, commit_bodies=["no trailer"])], OWNER, stand=PRO)[0]
    assert none_found["principal"] == "unattributed" and none_found["is_mine"] is False
    assert none_found["principal"] != g["principal"], "looked-and-found-none != could-not-look"
    print("  ok  'unattributed' (looked, none) stays distinct from 'unknown' (could not look)")

    # the dedup hash must move between them too, or a fetch outage silently
    # inherits the previous run's identity verdict
    assert pf.state_hash(pf.raw_state([failed], OWNER, stand=PRO)) != \
           pf.state_hash(pf.raw_state([_pr(40, OWNER, commit_bodies=["x\n\nStand: " + PRO])], OWNER, stand=PRO))
    print("  ok  an unknown principal refires rather than reusing the last verdict")

    # ---- a TRUNCATED commit fetch is also UNKNOWN (100/101 boundary) --------
    # qingyun-wu + john-the-dev on #2553, both at head 32a5ce73: the query was
    # `commits(first:100)` with no pageInfo/cursor, and _attach_commits treated
    # that first page as the whole history. The STANDS_UNAVAILABLE sentinel
    # covered a FAILED fetch but not a SUCCESSFUL, incomplete one -- so a Stand
    # trailer on commit 101 was dropped and the script still emitted a confident
    # principal. Same defect class the PR exists to remove, one layer down.
    # These exercise _attach_commits() itself, not just raw_state() fixtures.
    def _page_factory(pages):
        calls = []
        def _fake(owner, name, num, after):
            calls.append(after)
            return pages[len(calls) - 1]
        return _fake, calls

    real_page = pf._gh_stands_page
    try:
        # page 1: 100 commits, no trailer, hasNextPage.  page 2: commit 101 HAS it.
        p1 = [{"commit": {"messageBody": f"c{i}"}} for i in range(100)]
        p2 = [{"commit": {"messageBody": "c100\n\nStand: " + PRO}}]
        pf._gh_stands_page, calls = _page_factory([
            (True, p1, True, "CUR1"),
            (True, p2, False, None),
        ])
        prs = pf._attach_commits("o/n", [{"number": 101}])
        assert len(prs[0]["commits"]) == 101, len(prs[0]["commits"])
        assert calls == [None, "CUR1"], calls
        assert not prs[0].get(pf.STANDS_UNAVAILABLE), "a COMPLETE paged read is not unavailable"
        got = pf.raw_state([_pr(101, OWNER, commit_bodies=[c["messageBody"] for c in prs[0]["commits"]])],
                           OWNER, stand=PRO)[0]
        assert got["principal"] == PRO, got
        assert got["is_mine"] is True, got
        print("  ok  a Stand trailer on commit 101 survives pagination")

        # a page that FAILS mid-walk must fail closed, never report a partial set
        pf._gh_stands_page, _ = _page_factory([
            (True, p1, True, "CUR1"),
            (False, [], False, None),
        ])
        trunc = pf._attach_commits("o/n", [{"number": 102}])[0]
        assert trunc[pf.STANDS_UNAVAILABLE] is True, trunc
        assert trunc["commits"] == [], "a partial history must not be published as complete"
        print("  ok  a mid-walk page failure fails CLOSED, not partial")

        # hasNextPage true but no cursor: cannot continue -> also closed
        pf._gh_stands_page, _ = _page_factory([(True, p1, True, None)])
        stuck = pf._attach_commits("o/n", [{"number": 103}])[0]
        assert stuck[pf.STANDS_UNAVAILABLE] is True and stuck["commits"] == []
        print("  ok  hasNextPage without a cursor fails CLOSED")
    finally:
        pf._gh_stands_page = real_page

    # ---- scope descriptor: the payload must state its own coverage ----------
    # Why: the emitted state carries counts, CI, approvals and merge state with
    # nothing marking the population as partial, so a consumer reads its length
    # as a repo total. On 2026-08-04 a digest reported "31 open" for a repo with
    # ~100 open non-draft PRs. Issue #2643.
    sc = pf.scope_descriptor("o/n", "someowner", record_count=30)
    assert sc["filter"] == "author:someowner", sc
    assert "someowner" in sc["population"], sc
    assert any("approval" in e for e in sc["excludes"]), sc
    print("  ok  scope_descriptor names the author filter and the population")

    # Draft exclusion is UNCONDITIONAL — raw_state() drops drafts whether or not
    # the fetch is author-filtered, so it must appear in `excludes` either way.
    assert any("draft" in e for e in sc["excludes"]), sc

    real_argv = pf.fetch_argv
    try:
        pf.fetch_argv = lambda repo, owner: [
            "gh", "pr", "list", "--repo", repo, "--state", "open", "--limit", "1000",
        ]
        widened = pf.scope_descriptor("o/n", "someowner", record_count=30)
        # The anti-drift property: removing --author must change the claim...
        assert widened["filter"] == "none", widened
        assert "all authors" in widened["population"], widened
        # ...but it must NOT become a repository total. This is @john-the-dev's
        # blocker on #2645: the first version certified `excludes: "nothing"` here
        # while raw_state() still dropped every draft.
        assert any("draft" in e for e in widened["excludes"]), widened
        assert "is_repo_total" not in widened, "the field that could never be true is gone"
        print("  ok  no-author argv widens the population but STILL excludes drafts")
    finally:
        pf.fetch_argv = real_argv

    # Completeness is a certification, granted only on evidence.
    at_ceiling = pf.scope_descriptor("o/n", "someowner", record_count=1000)
    assert at_ceiling["complete"] is False, at_ceiling
    assert "indistinguishable" in at_ceiling["complete_reason"], at_ceiling
    below = pf.scope_descriptor("o/n", "someowner", record_count=999)
    assert below["complete"] is True, below
    unknown = pf.scope_descriptor("o/n", "someowner", record_count=None)
    assert unknown["complete"] is None, unknown
    print("  ok  complete: True below ceiling, False AT it, None when uncountable")

    # @john-the-dev's SECOND blocker on #2645, as his own repro: the ceiling
    # applies to the FETCH, and raw_state() then drops drafts, so certifying off
    # the emitted count lets a truncated fetch read as complete. His numbers:
    #   fetched=1000 (== ceiling) -> one draft dropped -> emitted=999 -> complete=True
    at_ceiling_with_a_draft = pf.scope_descriptor(
        "o/n", "someowner", record_count=999, fetched_count=1000)
    assert at_ceiling_with_a_draft["complete"] is False, at_ceiling_with_a_draft
    assert "1000" in at_ceiling_with_a_draft["complete_reason"], at_ceiling_with_a_draft
    # ...and the emitted size is still reported, it just does not decide the certificate.
    assert at_ceiling_with_a_draft["record_count"] == 999, at_ceiling_with_a_draft
    assert at_ceiling_with_a_draft["fetched_count"] == 1000, at_ceiling_with_a_draft
    print("  ok  a truncated FETCH is not certified complete by a smaller emitted count")

    # The mirror case: a genuinely short fetch still certifies, so the fix is not
    # just "always refuse" — that would be a gate that cannot go positive.
    short = pf.scope_descriptor("o/n", "someowner", record_count=30, fetched_count=31)
    assert short["complete"] is True, short
    print("  ok  a fetch below the ceiling still certifies complete")

    # Back-compat: callers that pass only record_count keep the old meaning
    # rather than silently losing their certificate.
    legacy = pf.scope_descriptor("o/n", "someowner", record_count=30)
    assert legacy["complete"] is True, legacy
    assert legacy["fetched_count"] == 30, legacy
    print("  ok  record_count-only callers still resolve a ceiling")

    # A non-numeric --limit must degrade to "ceiling unknown", never crash the
    # digest. scope_descriptor parses whatever argv actually carries, so a
    # future flag change (`--limit auto`, a templated value) reaches int() as a
    # string. Certifying completeness against an unparseable ceiling would be
    # the exact over-claim this descriptor exists to prevent, so the fallback
    # has to be `limit=None` -> `complete=None`, not a guess.
    real_argv = pf.fetch_argv
    try:
        pf.fetch_argv = lambda repo, owner: [
            "gh", "pr", "list", "--repo", repo, "--state", "open",
            "--author", owner, "--limit", "not-a-number",
        ]
        odd = pf.scope_descriptor("o/n", "someowner", record_count=30)
        assert odd["limit"] is None, odd
        assert odd["complete"] is None, odd
        # and the rest of the descriptor still works — the author filter is
        # unaffected by an unparseable limit.
        assert odd["filter"] == "author:someowner", odd
        print("  ok  an unparseable --limit yields complete=None, not a false certification")
    finally:
        pf.fetch_argv = real_argv

    # And the fetch itself must actually use that argv, or the descriptor
    # describes a command that isn't the one being run.
    argv = pf.fetch_argv("o/n", "someowner")
    assert argv[:5] == ["gh", "pr", "list", "--repo", "o/n"], argv
    assert "--author" in argv and argv[argv.index("--author") + 1] == "someowner", argv
    print("  ok  fetch_argv is the real gh command the descriptor reads")

    print("\nAll pr-flag core cases pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
