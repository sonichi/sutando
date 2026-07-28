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
        title="t", head=None, approvers=(), stale_approvers=()):
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
        "reviewDecision": review, "statusCheckRollup": rollup,
        "headRefOid": head, "mergeable": mergeable, "isDraft": draft,
        "reviews": [
            *[{"author": {"login": a}, "state": "APPROVED", "commit": {"oid": head}}
              for a in approvers],
            *[{"author": {"login": a}, "state": "APPROVED", "commit": {"oid": "old-head"}}
              for a in stale_approvers],
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
        assert set(s) == {"number", "title", "author", "is_mine", "head", "ci", "mergeable", "review", "approvals"}, s
        assert "court" not in s and "why" not in s and "ready" not in s, "script must emit NO judgement: " + str(s)
    print("  ok  raw_state emits objective fields only — no judgement")

    assert got[10]["is_mine"] and got[10]["ci"] == "green" and got[10]["review"] == "none"
    assert got[11]["is_mine"] and got[11]["ci"] == "pending"
    assert not got[12]["is_mine"] and got[12]["approvals"] == 2 and got[12]["review"] == "REVIEW_REQUIRED"
    # distinct approvers only: qingyun approving twice counts once
    assert got[13]["approvals"] == 1 and got[13]["ci"] == "failing"
    print("  ok  is_mine / ci / review / distinct-approvals are correct")

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

    # empty repo → empty state, stable hash
    assert pf.raw_state([], OWNER) == []
    assert pf.state_hash([]) == pf.state_hash([])
    print("  ok  empty repo → empty state")

    print("\nAll pr-flag core cases pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
