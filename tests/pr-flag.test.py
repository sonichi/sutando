#!/usr/bin/env python3
"""
Unit tests for the pure core of scripts/pr_flag.py (the PR-flag mechanism).

Root cause it fixes (Chi 2026-07-27): agent PRs are authored under the owner's
identity so GitHub never surfaces them to him, and ad-hoc flagging is a
discipline that gets missed / mis-targeted. The pure classify/dedup logic is
tested here; gh/discord/state I/O is glue.

Assertion-based (every line runs on pass). Run:
  python3 tests/pr-flag.test.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("pr_flag", REPO / "scripts" / "pr_flag.py")
pf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pf)


def _pr(number, author, review="", ci="green", mergeable="MERGEABLE", draft=False, title="t", approvers=()):
    rollup = []
    if ci == "green":
        rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
    elif ci == "pending":
        rollup = [{"status": "IN_PROGRESS", "conclusion": None}]
    elif ci == "failing":
        rollup = [{"status": "COMPLETED", "conclusion": "FAILURE", "name": "x"}]
    return {
        "number": number, "title": title, "author": {"login": author},
        "reviewDecision": review, "statusCheckRollup": rollup,
        "mergeable": mergeable, "isDraft": draft,
        "reviews": [{"author": {"login": a}, "state": "APPROVED"} for a in approvers],
    }


def main() -> int:
    OWNER = "sonichi"

    # _ci_state collapsing
    assert pf._ci_state([]) == "none"
    assert pf._ci_state([{"status": "COMPLETED", "conclusion": "SUCCESS"}]) == "green"
    assert pf._ci_state([{"status": "IN_PROGRESS"}]) == "pending"
    assert pf._ci_state([{"conclusion": "FAILURE"}]) == "failing"
    # a single failure among greens → failing (don't call a red PR ready)
    assert pf._ci_state([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]) == "failing"
    print("  ok  _ci_state collapses correctly")

    prs = [
        _pr(10, OWNER, ci="green", mergeable="MERGEABLE"),            # mine, ready
        _pr(11, OWNER, ci="green", mergeable="CONFLICTING"),          # mine, needs rebase
        _pr(12, OWNER, ci="pending"),                                 # mine, CI pending
        _pr(13, "peer", review="REVIEW_REQUIRED"),                    # peer, needs approval
        _pr(14, "peer", review="CHANGES_REQUESTED"),                  # peer, changes requested
        _pr(15, "peer", review="APPROVED"),                           # peer, approved → NOT owner's action
        _pr(16, OWNER, draft=True),                                   # mine but draft → skip
        _pr(17, OWNER, review="CHANGES_REQUESTED"),                   # MINE + changes requested (the #2342 case)
    ]
    items = pf.classify_prs(prs, OWNER)
    got = {i["number"]: i for i in items}

    assert got[10]["court"] == "owner" and "ready for your merge" in got[10]["why"], got[10]
    assert got[11]["court"] == "agent" and "rebase" in got[11]["why"], got[11]
    assert got[12]["court"] == "agent" and "pending" in got[12]["why"], got[12]
    # peer PRs are NOT the owner's action — even REVIEW_REQUIRED / CHANGES_REQUESTED
    # (their authors own the changes). Scoping to peers is the noise-bomb this avoids.
    assert 13 not in got, "peer REVIEW_REQUIRED is not the owner's action (avoids the 45-PR noise-bomb)"
    assert 14 not in got, "peer CHANGES_REQUESTED is the peer author's job, not the owner's"
    assert 15 not in got, "an approved peer PR is not the owner's action"
    assert 16 not in got, "a draft is skipped"
    # the load-bearing case: MY pr with CHANGES_REQUESTED is in the AGENT's court —
    # I address it; it is NEVER shown as ready for the owner's merge (the #2342/#2308 mis-flag).
    assert got[17]["court"] == "agent" and "address" in got[17]["why"], got[17]
    print("  ok  classify_prs surfaces only the owner's OWN open PRs")
    print("  ok  peer REVIEW_REQUIRED / CHANGES_REQUESTED excluded (no noise-bomb)")
    print("  ok  my CHANGES_REQUESTED PR is in MY court, never 'ready for your merge'")

    # only a clean OWN PR is in the owner's court (peer PRs added separately below)
    assert [i["number"] for i in items if i["court"] == "owner"] == [10]
    print("  ok  only green+mergeable+unblocked own PR is in the owner's court")

    # peer PRs: only the "one-approval-from-merge" set surfaces (the #2336 case).
    peer = [
        _pr(30, "peer", review="REVIEW_REQUIRED", ci="green", approvers=["qingyun"]),   # green + 1 approval → owner court
        _pr(31, "peer", review="REVIEW_REQUIRED", ci="green", approvers=[]),             # green but 0 approvals → skip
        _pr(32, "peer", review="REVIEW_REQUIRED", ci="pending", approvers=["qingyun"]),  # not green → skip
        _pr(33, "peer", review="CHANGES_REQUESTED", ci="green", approvers=["qingyun"]),  # changes requested → skip
        _pr(34, "peer", review="REVIEW_REQUIRED", ci="green", approvers=["a", "b"]),     # 2 approvals → owner court
    ]
    gp = {i["number"]: i for i in pf.classify_prs(peer, OWNER)}
    assert gp[30]["court"] == "owner" and "approval unblocks" in gp[30]["why"], gp.get(30)
    assert 31 not in gp, "peer with 0 approvals is not surfaced (avoids the firehose)"
    assert 32 not in gp, "peer not green is not surfaced"
    assert 33 not in gp, "peer changes-requested is the author's job, not the owner's"
    assert gp[34]["court"] == "owner" and "2 approval" in gp[34]["why"], gp.get(34)
    print("  ok  peer 'one-approval-from-merge' set surfaces to owner court; rest excluded")

    # HOLD-list: a PR I flagged issues on is never shown as ready — the #2339 contradiction.
    held_mine = pf.classify_prs([_pr(10, OWNER, ci="green", mergeable="MERGEABLE")], OWNER,
                                holds={"10": "2 bugs"})
    assert held_mine[0]["court"] == "held" and "2 bugs" in held_mine[0]["why"], held_mine
    held_peer = pf.classify_prs([_pr(30, "peer", review="REVIEW_REQUIRED", ci="green", approvers=["q"])], OWNER,
                                holds={"30": "risky"})
    assert held_peer[0]["court"] == "held", held_peer
    d3 = pf.render_digest(held_mine, "999")
    assert "Held (I flagged issues" in d3 and "#10" in d3, d3
    assert "Ready for your merge" not in d3, "a held-only set must NOT print a 'ready' section: " + d3
    print("  ok  held PRs render under 'Held', never as 'ready for merge'")

    # cover the remaining agent-court branches of classify_prs
    more = [
        _pr(20, OWNER, ci="failing"),                          # mine, CI failing
        _pr(21, OWNER, review="REVIEW_REQUIRED", ci="green"),  # mine, needs a review
        _pr(22, OWNER, ci="none", mergeable="UNKNOWN"),        # mine, no checks/unknown → 'open'
    ]
    g2 = {i["number"]: i for i in pf.classify_prs(more, OWNER)}
    assert g2[20]["court"] == "agent" and "CI failing" in g2[20]["why"], g2[20]
    assert g2[21]["court"] == "agent" and "review" in g2[21]["why"], g2[21]
    assert g2[22]["court"] == "agent" and g2[22]["why"] == "open", g2[22]
    print("  ok  agent-court branches: CI-failing / needs-review / open all covered")

    # sorted by number
    assert [i["number"] for i in items] == sorted(i["number"] for i in items)
    print("  ok  output sorted by PR number")

    # dedup hash: stable across title-only change, flips on state change
    h1 = pf.state_hash(items)
    prs_titlechange = [dict(p, title="different") for p in prs]
    h_title = pf.state_hash(pf.classify_prs(prs_titlechange, OWNER))
    assert h1 == h_title, "title-only change must NOT refire the flag"
    prs_ci_flip = [(_pr(12, OWNER, ci="green") if p["number"] == 12 else p) for p in prs]
    h_ciflip = pf.state_hash(pf.classify_prs(prs_ci_flip, OWNER))
    assert h1 != h_ciflip, "a PR's CI flipping green MUST refire the flag"
    print("  ok  dedup hash: stable on title change, flips on CI state change")

    # empty → empty digest, empty hash-set is stable
    assert pf.render_digest([], None) == ""
    assert pf.classify_prs([], OWNER) == []
    print("  ok  no PRs → empty digest")

    # digest: owner-court PRs are flagged; agent-court summarized in the 'On me' line
    d = pf.render_digest(items, "999")
    assert "<@999>" in d, d
    assert "Ready for your merge" in d and "#10" in d, d
    assert "On me" in d and "#17" in d, d          # my changes-requested PR is 'on me', not a merge flag
    print("  ok  digest flags owner-court, summarizes 'on me'")

    # when NOTHING is ready for the owner, say so honestly (all-agent-court set)
    agent_only = [i for i in items if i["court"] == "agent"]
    d2 = pf.render_digest(agent_only, "999")
    assert "Nothing of mine is ready for your merge" in d2, d2
    print("  ok  honest 'nothing ready' when owner court is empty")

    print("\nAll pr-flag core cases pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
