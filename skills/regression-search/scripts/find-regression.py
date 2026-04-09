#!/usr/bin/env python3
"""find-regression.py — locate the call(s) where a feature regressed.

Scans results/calls/calls.jsonl for calls touching a given keyword, classifies
each as working/broken from transcript heuristics, and prints a timeline.

Usage:
    python3 skills/regression-search/scripts/find-regression.py "record"
    python3 skills/regression-search/scripts/find-regression.py "summon" --since 2026-04-01
    python3 skills/regression-search/scripts/find-regression.py "play" --json --show-snippet

See skills/regression-search/SKILL.md for design notes.
"""

import argparse
import json
import re
import sys
from pathlib import Path

CALLS_FILE = Path(__file__).resolve().parent.parent.parent.parent / "results" / "calls" / "calls.jsonl"

# (label, regex) pairs — label is what shows up in the reasons column
REFUSAL_PATTERNS = [
    ("refusal", r"\bi\s*can'?t\b"),
    ("refusal", r"\bi'?m\s*not\s*able\b"),
    ("refusal", r"\bi'?m\s*unable\b"),
    ("refusal", r"\bunable\s*to\b"),
    ("refusal", r"\bsorry,?\s*i\s*(can'?t|cannot)\b"),
]

ERROR_PATTERNS = [
    ("error", r"\berror\b"),
    ("error", r"\bfailed\b"),
    ("error", r"\bdidn'?t\s*work\b"),
    ("error", r"\bcouldn'?t\b"),
    ("error", r"\bsomething\s*went\s*wrong\b"),
    ("error", r"\bnot\s*working\b"),
]

SILENCE_PATTERN = re.compile(r"sutando:\s*\(silence\)", re.IGNORECASE)


def parse_transcript(transcript: str) -> list[tuple[str, str]]:
    """Split a transcript into (role, text) pairs.

    Roles seen in calls.jsonl: 'Sutando' (assistant), 'Recipient'/'User' (caller).
    Lines that don't start with a role are appended to the previous turn.
    """
    turns: list[tuple[str, str]] = []
    for line in transcript.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(Sutando|Recipient|User|Caller):\s*(.*)$", line)
        if m:
            role = "sutando" if m.group(1) == "Sutando" else "user"
            turns.append((role, m.group(2)))
        elif turns:
            role, prev = turns[-1]
            turns[-1] = (role, f"{prev} {line}")
    return turns


def classify_call(turns: list[tuple[str, str]], keyword: str) -> tuple[str, list[str]]:
    """Return ('working' | 'broken' | 'mentioned', reasons[]).

    'mentioned' means the keyword appears but neither classification fired —
    fall back to working unless we see explicit failure signals.
    """
    keyword_lc = keyword.lower()
    reasons: list[str] = []
    keyword_in_user_turn = False
    keyword_in_sutando_turn = False
    last_user_turn = ""
    repeat_count = 1

    for i, (role, text) in enumerate(turns):
        text_lc = text.lower()
        if keyword_lc in text_lc:
            if role == "user":
                keyword_in_user_turn = True
            else:
                keyword_in_sutando_turn = True

        if role == "user":
            # Detect user repeating themselves verbatim (Sutando ignored them)
            if text_lc == last_user_turn and len(text_lc) > 6:
                repeat_count += 1
                if repeat_count >= 2 and keyword_lc in text_lc:
                    reasons.append(f"user repeated request {repeat_count}x")
            else:
                repeat_count = 1
            last_user_turn = text_lc

        if role == "sutando" and keyword_in_user_turn:
            # Look for refusals/errors immediately after the user mentioned the feature
            for label, pat in REFUSAL_PATTERNS:
                if re.search(pat, text_lc):
                    if label not in reasons:
                        reasons.append(label)
                    break
            for label, pat in ERROR_PATTERNS:
                if re.search(pat, text_lc):
                    if label not in reasons:
                        reasons.append(label)
                    break
            if SILENCE_PATTERN.search(f"sutando: {text}") and "silence" not in reasons:
                reasons.append("silence")

    if not (keyword_in_user_turn or keyword_in_sutando_turn):
        return "no-match", reasons
    if reasons:
        return "broken", reasons
    return "working", reasons


def find_snippet(turns: list[tuple[str, str]], keyword: str) -> str:
    """Return a short context snippet around the first keyword hit."""
    keyword_lc = keyword.lower()
    for i, (role, text) in enumerate(turns):
        if keyword_lc in text.lower():
            snippet = f"{role}: {text}"
            return snippet[:120] + ("..." if len(snippet) > 120 else "")
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keyword", help="Feature keyword to search for")
    parser.add_argument("--since", help="Only include calls on or after YYYY-MM-DD")
    parser.add_argument("--json", action="store_true", help="JSON output for scripting")
    parser.add_argument("--show-snippet", action="store_true", help="Show transcript snippet for each call")
    args = parser.parse_args()

    if not CALLS_FILE.exists():
        print(f"calls file not found: {CALLS_FILE}", file=sys.stderr)
        return 2

    results = []
    with CALLS_FILE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                call = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = call.get("timestamp", "")
            if args.since and ts < args.since:
                continue
            transcript = call.get("transcript", "")
            turns = parse_transcript(transcript)
            verdict, reasons = classify_call(turns, args.keyword)
            if verdict == "no-match":
                continue
            entry = {
                "callSid": call.get("callSid", ""),
                "timestamp": ts,
                "verdict": verdict,
                "reasons": reasons,
            }
            if args.show_snippet:
                entry["snippet"] = find_snippet(turns, args.keyword)
            results.append(entry)

    results.sort(key=lambda r: r["timestamp"])

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    # Human-readable timeline
    if not results:
        print(f"No calls match '{args.keyword}'.")
        return 0

    broken = sum(1 for r in results if r["verdict"] == "broken")
    working = len(results) - broken
    print(f"'{args.keyword}': {len(results)} matched ({broken} broken, {working} working)")
    print("=" * 60)
    for r in results:
        marker = "✗" if r["verdict"] == "broken" else "✓"
        ts_short = r["timestamp"][:16].replace("T", " ")
        line = f"  {marker} {ts_short}  {r['callSid'][-12:]}"
        if r["verdict"] == "broken":
            line += f"  [{', '.join(r['reasons'][:2])}]"
        print(line)
        if args.show_snippet and r.get("snippet"):
            print(f"      {r['snippet']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
