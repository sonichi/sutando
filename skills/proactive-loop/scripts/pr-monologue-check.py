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
import subprocess
import sys
from datetime import datetime, timezone

DEFAULT_THRESHOLD = 3


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


def _gh_json(path: str):
    # --paginate or the newest events are missing: GitHub returns the OLDEST 100
    # first, so an unpaginated read computes the trailing run from a stale end.
    proc = subprocess.run(["gh", "api", "--paginate", path], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api failed for {path}: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


def fetch(repo: str, number: int):
    comments = _gh_json(f"repos/{repo}/issues/{number}/comments?per_page=100")
    reviews = _gh_json(f"repos/{repo}/pulls/{number}/reviews?per_page=100")
    return comments, reviews


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("number", type=int)
    ap.add_argument("--repo", default="sonichi/sutando")
    ap.add_argument("--me", required=True)
    ap.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    ap.add_argument("--count-bots", action="store_true",
                    help="treat bot comments as engagement (default: ignore them)")
    args = ap.parse_args(argv)

    if args.threshold < 1:
        print("threshold must be >= 1", file=sys.stderr)
        return 2
    try:
        comments, reviews = fetch(args.repo, args.number)
    except (RuntimeError, ValueError) as exc:
        print(f"CANNOT ANSWER: {exc}", file=sys.stderr)
        return 2

    events = merge_events(comments, reviews, keep_bots=args.count_bots)
    run, span = trailing_run(events, args.me)
    if not events:
        print(f"#{args.number}: no comment/review activity yet — safe to post")
        return 0
    if run >= args.threshold:
        print(
            f"REFUSE #{args.number}: your last {run} events on this thread are ALL yours, "
            f"spanning {span:.1f}d, with no reply from anyone else.\n"
            f"  Posting again talks into silence. Re-solicit a human/stand, or leave it."
        )
        return 1
    print(f"#{args.number}: trailing run of yours = {run} (threshold {args.threshold}) — safe to post")
    return 0


if __name__ == "__main__":
    sys.exit(main())
