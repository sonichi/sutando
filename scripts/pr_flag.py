#!/usr/bin/env python3
"""
PR-flag mechanism — structurally surface open PRs that need the owner's action.

Root cause this fixes (Chi 2026-07-27 "fix root cause"): PRs I open are authored
under the owner's own GH-mapped identity (commits use his noreply email), so
GitHub never lists them in his "review requested" queue — and ad-hoc flagging by
the agent is a *discipline* that gets missed or mis-targeted (flagged the wrong
PR; flagged "mergeable" when it was actually CHANGES_REQUESTED). This makes
flagging a *mechanism*: scan open PRs, classify which need the owner, and emit a
correct-state digest with dedup so it fires only on an actionable state change.

Pure core (`classify_prs`) is unit-tested; the gh/discord/state I/O is glue.

Usage:
  python3 scripts/pr_flag.py --repo sonichi/sutando --owner sonichi \
      --channel <discord_channel_id> [--mention <owner_discord_id>] [--dry-run]

Exit 0 always (fail-open; a flag mechanism must never break a caller/cron).
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
    fail = any((c.get("conclusion") in ("FAILURE", "CANCELLED", "TIMED_OUT")) for c in rc)
    if fail:
        return "failing"
    pending = any((c.get("status") in ("IN_PROGRESS", "QUEUED", "PENDING")) for c in rc)
    if pending:
        return "pending"
    return "green"


def classify_prs(prs: list, owner_login: str, holds: dict | None = None) -> list:
    """Return the PRs that need the owner's action, each annotated with why+state.

    Scope = **PRs authored under the owner's identity** (`author == owner_login`),
    OPEN and not draft. This is precisely the set GitHub hides from him: agent
    commits carry the owner's GH-mapped email, so his own PRs never appear in his
    "review requested" queue — the exact root-cause gap (Chi 2026-07-27).

    Deliberately NOT scoped to "any REVIEW_REQUIRED / CHANGES_REQUESTED PR": that
    swept in ~40 peer PRs whose changes are their *authors'* job, not the owner's
    (a noise-bomb). A peer PR that needs the owner's specific approval (the #2336
    case) is driven by explicit routing — someone @-mentions him — which is
    conversational, not PR metadata; that stays handled case-by-case.

    `changes_requested` on the owner's OWN PR is surfaced distinctly, so his PR is
    never mislabeled merge-ready (the #2342 mis-flag this mechanism prevents).

    Output is sorted by PR number; each item is a plain dict (JSON-serializable).
    """
    holds = holds or {}
    out = []
    for pr in prs:
        if pr.get("isDraft"):
            continue
        num = pr.get("number")
        author = (pr.get("author") or {}).get("login", "")
        decision = pr.get("reviewDecision") or ""  # '', APPROVED, REVIEW_REQUIRED, CHANGES_REQUESTED
        ci = _ci_state(pr.get("statusCheckRollup"))
        mergeable = pr.get("mergeable") or "UNKNOWN"

        is_mine = author == owner_login
        if not is_mine:
            # Peer PRs are usually the peer author's job — but surface the narrow
            # "one-approval-from-merge" set the owner can unblock: green, still
            # REVIEW_REQUIRED, and already carrying ≥1 approval (the #2336 case
            # that triggered this mechanism). Everything else peer is skipped, so
            # this is NOT the ~45-PR REVIEW_REQUIRED firehose.
            approvers = {
                (r.get("author") or {}).get("login")
                for r in (pr.get("reviews") or [])
                if r.get("state") == "APPROVED"
            }
            approvers.discard(None)
            if ci == "green" and decision == "REVIEW_REQUIRED" and len(approvers) >= 1:
                pcourt = "owner"
                pwhy = f"peer PR, {len(approvers)} approval(s) — your approval unblocks the merge"
                if str(num) in holds:  # I flagged issues on it — never show as ready
                    pcourt, pwhy = "held", "held — " + holds[str(num)]
                out.append({
                    "number": num, "title": pr.get("title", ""), "author": author,
                    "court": pcourt, "why": pwhy,
                    "ci": ci, "mergeable": mergeable, "review": decision or "none",
                })
            continue

        # Whose court is the PR in? `mergeable == MERGEABLE` only means "no merge
        # conflict" — a CHANGES_REQUESTED review still blocks merge and is the
        # AGENT's job to address (the systematic error this mechanism corrects:
        # calling a changes-requested PR "ready to merge"). Only a clean,
        # unblocked PR is in the OWNER's court (his to merge).
        if decision == "CHANGES_REQUESTED":
            court, why = "agent", "changes requested — I need to address"
        elif ci == "failing":
            court, why = "agent", "CI failing — I need to fix"
        elif mergeable == "CONFLICTING":
            court, why = "agent", "needs rebase (conflict) — mine to do"
        elif ci == "pending":
            court, why = "agent", "CI pending"
        elif decision == "REVIEW_REQUIRED":
            court, why = "agent", "needs a review before it can merge"
        elif ci == "green" and mergeable == "MERGEABLE":
            court, why = "owner", "green + approved + mergeable — ready for your merge"
        else:
            court, why = "agent", "open"

        if str(num) in holds:  # I flagged issues — never present a held PR as ready
            court, why = "held", "held — " + holds[str(num)]

        out.append({
            "number": num,
            "title": pr.get("title", ""),
            "author": author,
            "court": court,
            "why": why,
            "ci": ci,
            "mergeable": mergeable,
            "review": decision or "none",
        })
    out.sort(key=lambda x: x["number"])
    return out


def state_hash(items: list) -> str:
    """Stable hash of the actionable set — (number, why, ci, mergeable, review).

    Changes when a PR appears/disappears OR its actionable state flips (CI green,
    new CHANGES_REQUESTED, rebase cleared, …). Title changes don't refire.
    """
    key = [
        [i["number"], i["court"], i["why"], i["ci"], i["mergeable"], i["review"]]
        for i in items
    ]
    blob = json.dumps(key, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def render_digest(items: list, mention: str | None) -> str:
    """Digest for the owner: flag ONLY the PRs in his court (ready to merge);
    summarize the rest ('on me') so he sees the full picture without being asked
    to act on PRs that are the agent's to finish."""
    if not items:
        return ""
    who = f"<@{mention}> " if mention else ""
    owner_court = [i for i in items if i["court"] == "owner"]
    held = [i for i in items if i["court"] == "held"]
    agent_court = [i for i in items if i["court"] == "agent"]
    lines = []
    if owner_court:
        lines.append(f"🚩 {who}**Ready for your merge** ({len(owner_court)}):")
        for i in owner_court:
            lines.append(f"🟢 **#{i['number']}** — {i['title'][:70]} (CI {i['ci']}, {i['review']})")
    else:
        lines.append(f"🚩 {who}**Nothing of mine is ready for your merge right now.**")
    if held:
        # PRs that are green/approved on paper but I flagged issues on — never
        # presented as ready (the #2339 contradiction this prevents).
        lines.append("\n⏸ **Held (I flagged issues — don't merge yet):** "
                     + ", ".join(f"#{i['number']} ({i['why'].replace('held — ','')})" for i in held))
    if agent_court:
        lines.append(f"\n_On me ({len(agent_court)}): {', '.join('#'+str(i['number'])+' ('+i['why']+')' for i in agent_court)}_")
    return "\n".join(lines)


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


def main() -> int:  # pragma: no cover — CLI + gh/discord/state I/O glue; pure logic covered in tests
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="sonichi/sutando")
    ap.add_argument("--owner", default="sonichi", help="GH login whose authored PRs are 'mine'")
    ap.add_argument("--channel", help="Discord channel id to post the digest to")
    ap.add_argument("--mention", help="Owner Discord id to @-mention")
    ap.add_argument("--state-file", default=None, help="dedup state path (default: <workspace>/state/pr-flag-state.json)")
    ap.add_argument("--dry-run", action="store_true", help="print digest, don't post or write state")
    args = ap.parse_args()

    prs = _fetch_prs(args.repo)
    # hold-list: PRs I've flagged content issues on (str number → reason). A held
    # PR is never shown as "ready", so the digest can't contradict my own review.
    holds = {}
    try:
        ws = subprocess.run(["bash", "scripts/sutando-config.sh", "workspace"],
                            capture_output=True, text=True, timeout=20).stdout.strip()
        hp = (Path(ws) / "state" / "pr-flag-holds.json") if ws else Path("state/pr-flag-holds.json")
        holds = json.loads(hp.read_text())
    except Exception:
        holds = {}
    items = classify_prs(prs, args.owner, holds)
    digest = render_digest(items, args.mention)
    h = state_hash(items)

    if args.dry_run:
        print(f"[dry-run] hash={h} items={len(items)}")
        print(digest or "(nothing needs the owner)")
        return 0

    # dedup: only post if the actionable set changed since last flag. The script
    # runs from the repo root (cron/loop cwd), so resolve the workspace via the
    # loader (relative invocation) and let subprocesses inherit cwd — no
    # __file__ path-walking (the workspace-resolution lint forbids that).
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

    if h == prev:
        print(f"pr-flag: no change (hash={h}); staying quiet.")
        return 0

    if items and args.channel:
        try:
            subprocess.run(
                ["python3", "src/discord-bridge.py", "send", args.channel, digest],
                timeout=60,
            )
            print(f"pr-flag: posted digest ({len(items)} PRs, hash={h}) to {args.channel}")
        except Exception as e:
            print(f"pr-flag: post failed: {e}", file=sys.stderr)
            return 0  # fail-open; don't advance state so we retry next run
    else:
        print(f"pr-flag: {len(items)} PRs need owner (hash={h}); no channel set → not posting.")

    try:
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps({"hash": h, "count": len(items)}))
    except Exception as e:
        print(f"pr-flag: state write failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
