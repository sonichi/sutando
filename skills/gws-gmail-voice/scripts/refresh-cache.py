#!/usr/bin/env python3
"""Refresh the importance-scored inbox cache for voice triage.

Reads recent unread mail via `gws gmail +triage --format json --max 30`,
scores each message by importance heuristics, writes the top-3 to
`state/external-cache/inbox-important.json` for the `triage_email` voice
tool to read sub-50ms.

Run by ACT loop (see notes/probe-categories.md → cache_email_triage) or
ad-hoc. Idempotent; safe to run more often than needed.

Scoring rules (per feedback_triage_recency_not_importance.md):
- BLACKLIST domain match → score -100 (newsletter / digest noise)
- Subject keyword match (deadline / RSVP / CI failure / accepted) → +10 each
- Sender domain `.edu` → +5
- Sender is `Chi Wang <notifications@github.com>` (self CI) → +15
- Ties broken by recency (newer wins)
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_PATH = REPO_ROOT / "state" / "external-cache" / "inbox-important.json"
TMP_PATH = CACHE_PATH.with_suffix(".json.tmp")

# Domains whose mail is overwhelmingly newsletter / digest / promotional.
# Generic patterns only — specific brand names (foundercoho, odsc) overfit one
# user's inbox; the noreply/marketing/newsletter prefix patterns catch them
# anyway. Per Mini PR #704 review.
BLACKLIST_DOMAINS = (
    "linkedin.com",  # messaging digests
    "substack.com",
    "workspace-noreply@google.com",
    "noreply@medium.com",
    "newsletter@",
    "marketing@",
    "no-reply@",
    "noreply@",
    "notifications@openreview.net",  # academic notifs handled separately as +10
)

# Owner's email domain for self-notification scoring. OSS users set
# SUTANDO_OWNER_EMAIL_DOMAIN=their.tld to get their own self-CI bumps.
# Empty → skip the bump (no penalty, just no boost for that path).
OWNER_EMAIL_DOMAIN = os.environ.get("SUTANDO_OWNER_EMAIL_DOMAIN", "").strip().lower()

# Subjects matching these are high-importance.
SUBJECT_BUMP_PATTERNS = [
    re.compile(r"\bdeadline\b", re.I),
    re.compile(r"\bRSVP\b", re.I),
    re.compile(r"\baccepted\b", re.I),
    re.compile(r"\bconfirmed\b", re.I),
    re.compile(r"\bCI failure\b|\bRun failed\b", re.I),
    re.compile(r"\burgent\b|\baction required\b", re.I),
    re.compile(r"\binvitation\b|\binvited\b", re.I),
    re.compile(r"\bcamera[- ]ready\b|\breview\b", re.I),
]


def score_message(msg: dict) -> int:
    sender = msg.get("from", "").lower()
    subject = msg.get("subject", "")
    score = 0
    if any(b in sender for b in BLACKLIST_DOMAINS):
        score -= 100
    if "notifications@github.com" in sender:
        score += 15  # GitHub notifications (CI failures, PR comments) HIGH
    if OWNER_EMAIL_DOMAIN and f"@{OWNER_EMAIL_DOMAIN}" in sender:
        score += 15  # owner self-domain mail HIGH (set via $SUTANDO_OWNER_EMAIL_DOMAIN)
    if re.search(r"@[\w-]+\.edu\b", sender):
        score += 5
    if "openreview.net" in sender:
        score += 10  # academic submission notifications
    if "calendar" in sender or "invitation" in subject.lower():
        score += 8
    for pat in SUBJECT_BUMP_PATTERNS:
        if pat.search(subject):
            score += 10
    return score


def main() -> int:
    try:
        out = subprocess.run(
            ["gws", "gmail", "+triage", "--format", "json", "--max", "30"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"refresh-cache: gws call failed: {e}", file=sys.stderr)
        return 2
    if out.returncode != 0:
        print(f"refresh-cache: gws non-zero: {out.stderr[:200]}", file=sys.stderr)
        return 3
    match = re.search(r"^\{", out.stdout, re.M)
    if not match:
        print("refresh-cache: no JSON object in gws output", file=sys.stderr)
        return 4
    parsed = json.loads(out.stdout[match.start():])
    messages = parsed.get("messages", []) if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else [])

    scored = sorted(
        ((score_message(m), m) for m in messages),
        key=lambda sm: (-sm[0], -messages.index(sm[1])),  # ties: more-recent first (gws returns newest-first)
    )
    top_3 = [m for s, m in scored if s > -100][:3]  # filter blacklisted

    cache = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "top_3_important": top_3,
        "all_unread_count": len(messages),
        "query": parsed.get("query") if isinstance(parsed, dict) else "is:unread",
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TMP_PATH.write_text(json.dumps(cache, indent=2))
    os.replace(TMP_PATH, CACHE_PATH)
    print(f"refresh-cache: wrote {len(top_3)} top-important / {len(messages)} scanned → {CACHE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
