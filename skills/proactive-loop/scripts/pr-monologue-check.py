#!/usr/bin/env python3
"""Refuse to post into a PR thread whose recent history is only me, unanswered.

A standing review deserves periodic re-verification, but re-verification posted into
silence is noise: nobody is reading it, and each repeat makes the next one less likely
to be read. This counts the TRAILING run of consecutive events authored by one login
across both comment surfaces (issue comments + reviews) and refuses at a threshold.

Exit 0 safe to post - 1 REFUSE, the run is named - 2 could not answer (NOT a green light).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

DEFAULT_THRESHOLD = 3
PR_URL_RE = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
# COMMENTED carries no gate, so a later one must not read as superseding an
# earlier CHANGES_REQUESTED that is still blocking.
GATING_STATES = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_bot(login: str) -> bool:
    """A CI bot commenting is not a human reading you; counting it as engagement
    resets the run and clears the very post this guard exists to stop."""
    return login.endswith("[bot]")


def merge_events(comments, reviews, keep_bots: bool = False):
    """One timeline across both surfaces. A review IS engagement, so a thread answered
    only by a review must not read as silence. Bots are dropped, not counted either way."""
    events = []
    for c in comments or []:
        ts = c.get("created_at")
        login = ((c.get("user") or {}).get("login")) or ""
        if ts:
            events.append({"ts": ts, "login": login, "kind": "comment"})
    for r in reviews or []:
        ts = r.get("submitted_at")
        login = ((r.get("user") or {}).get("login")) or ""
        if ts:
            events.append({"ts": ts, "login": login, "kind": "review"})
    if not keep_bots:
        events = [e for e in events if not is_bot(e["login"])]
    events.sort(key=lambda e: parse_ts(e["ts"]))
    return events


def trailing_run(events, me: str):
    """Count consecutive trailing events authored by `me`. Returns (run, span_days)."""
    run = 0
    for e in reversed(events):
        if e["login"] == me:
            run += 1
        else:
            break
    if run == 0:
        return 0, 0.0
    tail = events[-run:]
    span = (parse_ts(tail[-1]["ts"]) - parse_ts(tail[0]["ts"])).total_seconds() / 86400.0
    return run, span


def my_review_state(reviews, me: str):
    """My latest GATING review, or None. Bare `reviewDecision` names the PR's gate,
    not whose review holds it, so the only way to learn I am the blocker is to read
    my own rows here."""
    mine = [r for r in reviews or []
            if ((r.get("user") or {}).get("login")) == me
            and r.get("state") in GATING_STATES
            and r.get("submitted_at")]
    if not mine:
        return None
    latest = max(mine, key=lambda r: parse_ts(r["submitted_at"]))
    return {"state": latest["state"],
            "commit": (latest.get("commit_id") or ""),
            "submitted_at": latest["submitted_at"]}


def describe_my_review(review, head_sha: str) -> str:
    """One line naming my standing review and whether it still sits at head."""
    if review is None:
        return ""
    short, head_short = review["commit"][:8], (head_sha or "")[:8]
    # Unknown head is not a match: claiming "at head" without one would assert
    # the very thing that could not be read.
    at_head = bool(head_short) and review["commit"] == head_sha
    where = f"at head {head_short}" if at_head else (
        f"at {short}, but head is {head_short} — STALE" if head_short
        else f"at {short} (head unreadable, staleness unknown)")
    if review["state"] == "CHANGES_REQUESTED":
        return (f"  YOUR REVIEW BLOCKS THIS PR: CHANGES_REQUESTED {where} "
                f"({review['submitted_at']}).\n"
                "  Re-verify at head, then dismiss or replace it — a stale block "
                "turns a DO into a WAIT that nobody can see.")
    return f"  note: your standing review here is {review['state']} {where}."


def _gh_json(path: str, paginate: bool = True):
    # --paginate or the newest events are missing: GitHub returns the OLDEST 100
    # first, so an unpaginated read computes the trailing run from a stale end.
    cmd = ["gh", "api"] + (["--paginate"] if paginate else []) + [path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api failed for {path}: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


def fetch(repo: str, number: int):
    comments = _gh_json(f"repos/{repo}/issues/{number}/comments?per_page=100")
    reviews = _gh_json(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
    return comments, reviews


def fetch_head_sha(repo: str, number: int) -> str:
    """Head sha, or "" when it cannot be read. Never raises: this call is additive
    context, and a gate that starts refusing because of it would be a regression."""
    try:
        pr = _gh_json(f"repos/{repo}/pulls/{number}", paginate=False)
        return ((pr or {}).get("head") or {}).get("sha") or ""
    except (RuntimeError, ValueError):
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("number", help="PR number (needs --repo) or a full PR URL")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--me", required=True)
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    ap.add_argument("--count-bots", action="store_true",
                    help="treat bot comments as engagement (default: ignore them)")
    args = ap.parse_args(argv)
    # No default repo: a bare number in another repo reads an unrelated thread,
    # so the only verdict this gate could reach there is "safe".
    url = PR_URL_RE.match(args.number.strip())
    if url:
        if args.repo and args.repo != url.group(1):
            print(f"CANNOT ANSWER: --repo {args.repo} disagrees with the URL's "
                  f"{url.group(1)} — refusing rather than picking one", file=sys.stderr)
            return 2
        args.repo, args.number = url.group(1), int(url.group(2))
    else:
        if not args.number.isdigit():
            print(f"CANNOT ANSWER: {args.number!r} is neither a PR number nor a "
                  "github.com PR URL", file=sys.stderr)
            return 2
        args.number = int(args.number)
        if args.repo is None:
            print("CANNOT ANSWER: no --repo given, and a bare number could name a PR "
                  "in any repo. Pass --repo <owner/name>, or the full PR URL.",
                  file=sys.stderr)
            return 2

    if args.threshold < 1:
        print("threshold must be >= 1", file=sys.stderr)
        return 2
    try:
        comments, reviews = fetch(args.repo, args.number)
    except (RuntimeError, ValueError) as exc:
        print(f"CANNOT ANSWER: {exc}", file=sys.stderr)
        return 2

    # Emitted on every path: the run verdict is about the THREAD, so a caller who
    # reads only it can act on a PR it is itself blocking. No review, no head call.
    _mine = my_review_state(reviews, args.me)
    standing = describe_my_review(
        _mine, fetch_head_sha(args.repo, args.number)) if _mine else ""

    events = merge_events(comments, reviews, keep_bots=args.count_bots)
    run, span = trailing_run(events, args.me)
    if not events:
        print(f"{args.repo}#{args.number}: no comment/review activity yet — safe to post")
        if standing:
            print(standing)
        return 0
    if standing:
        print(standing)
    if run >= args.threshold:
        print(
            f"REFUSE {args.repo}#{args.number}: your last {run} events on this thread are ALL yours, "
            f"spanning {span:.1f}d, with no reply from anyone else.\n"
            f"  Posting again talks into silence. Re-solicit a human/stand, or leave it."
        )
        return 1
    print(f"{args.repo}#{args.number}: trailing run of yours = {run} "
          f"(threshold {args.threshold}) — safe to post")
    return 0


if __name__ == "__main__":
    sys.exit(main())
