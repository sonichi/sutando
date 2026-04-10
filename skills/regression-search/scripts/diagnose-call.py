#!/usr/bin/env python3
"""diagnose-call.py — deep diagnosis of a single call.

Looks up a call by SID (full or last-N suffix), reads the transcript and any
metrics from call-metrics.jsonl, and prints what went wrong: silences,
refusals, errors, repeated user requests, and hangup-style endings.

Usage:
    python3 skills/regression-search/scripts/diagnose-call.py de1f04733fc2
    python3 skills/regression-search/scripts/diagnose-call.py CA701fc412977901fd7778357da33d91f8 --metrics
    python3 skills/regression-search/scripts/diagnose-call.py de1f04733fc2 --json

Companion to find-regression.py — find candidates with one, drill in with the
other. Closes the second half of #188.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent.parent.parent
CALLS_FILE = REPO / "results" / "calls" / "calls.jsonl"
METRICS_FILE = REPO / "data" / "call-metrics.jsonl"

REFUSAL_RE = re.compile(
    r"\b(i\s*can'?t|i'?m\s*not\s*able|i'?m\s*unable|unable\s*to|sorry,?\s*i\s*(can'?t|cannot))\b",
    re.IGNORECASE,
)
ERROR_RE = re.compile(
    r"\b(error|failed|didn'?t\s*work|couldn'?t|something\s*went\s*wrong|not\s*working)\b",
    re.IGNORECASE,
)
SILENCE_RE = re.compile(r"\(silence\)", re.IGNORECASE)


def parse_transcript(transcript: str) -> list[tuple[str, str]]:
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


def find_call(sid_query: str) -> Optional[dict]:
    """Match by full SID or by suffix (12 chars is enough to disambiguate)."""
    if not CALLS_FILE.exists():
        return None
    candidates = []
    with CALLS_FILE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                call = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = call.get("callSid", "")
            if sid == sid_query or sid.endswith(sid_query):
                candidates.append(call)
    if not candidates:
        return None
    if len(candidates) > 1:
        print(f"⚠ {len(candidates)} calls match suffix '{sid_query}' — using most recent", file=sys.stderr)
        candidates.sort(key=lambda c: c.get("timestamp", ""))
    return candidates[-1]


def find_metrics(call_sid: str) -> Optional[dict]:
    if not METRICS_FILE.exists():
        return None
    with METRICS_FILE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if m.get("callSid") == call_sid:
                return m
    return None


def analyze_turns(turns: list[tuple[str, str]]) -> dict:
    """Walk the transcript and report what's notable."""
    sutando_turns = sum(1 for r, _ in turns if r == "sutando")
    user_turns = sum(1 for r, _ in turns if r == "user")

    refusals: list[str] = []
    errors: list[str] = []
    silences: list[int] = []
    repeated_user: list[str] = []

    last_user = ""
    repeat = 1
    for i, (role, text) in enumerate(turns):
        if role == "sutando":
            if REFUSAL_RE.search(text):
                refusals.append(_truncate(text))
            if ERROR_RE.search(text):
                errors.append(_truncate(text))
            if SILENCE_RE.search(text):
                silences.append(i)
        elif role == "user":
            if text.lower() == last_user and len(text) > 6:
                repeat += 1
                if repeat >= 2:
                    repeated_user.append(_truncate(text))
            else:
                repeat = 1
            last_user = text.lower()

    # Ending — hangup-style endings: short single-word user, no Sutando follow-up
    ending_kind = "normal"
    if turns:
        last_role, last_text = turns[-1]
        if last_role == "user" and len(last_text) <= 12:
            ending_kind = f"abrupt user end: '{last_text}'"
        elif last_role == "sutando" and SILENCE_RE.search(last_text):
            ending_kind = "ended with sutando silence"

    return {
        "total_turns": len(turns),
        "sutando_turns": sutando_turns,
        "user_turns": user_turns,
        "refusals": refusals,
        "errors": errors,
        "silences": len(silences),
        "repeated_user": repeated_user,
        "ending": ending_kind,
    }


def _truncate(s: str, n: int = 120) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + "..."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sid", help="Call SID (full or last 12 chars)")
    parser.add_argument("--metrics", action="store_true", help="Also show data/call-metrics.jsonl event timeline if available")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--full-transcript", action="store_true", help="Print the full transcript")
    args = parser.parse_args()

    call = find_call(args.sid)
    if not call:
        print(f"No call matches '{args.sid}' in {CALLS_FILE}", file=sys.stderr)
        return 1

    sid = call.get("callSid", "")
    ts = call.get("timestamp", "")
    transcript = call.get("transcript", "")
    turns = parse_transcript(transcript)
    analysis = analyze_turns(turns)

    metrics = find_metrics(sid) if args.metrics else None

    if args.json:
        out = {
            "callSid": sid,
            "timestamp": ts,
            "analysis": analysis,
            "metrics": metrics,
        }
        if args.full_transcript:
            out["transcript"] = transcript
        print(json.dumps(out, indent=2))
        return 0

    # Human-readable
    print(f"Call {sid}")
    print(f"  ts: {ts}")
    print(f"  turns: {analysis['total_turns']} ({analysis['sutando_turns']} sutando, {analysis['user_turns']} user)")
    print(f"  ending: {analysis['ending']}")
    print()

    issues_found = False

    if analysis["refusals"]:
        issues_found = True
        print(f"  ✗ {len(analysis['refusals'])} refusal(s):")
        for r in analysis["refusals"][:3]:
            print(f"      {r}")
    if analysis["errors"]:
        issues_found = True
        print(f"  ✗ {len(analysis['errors'])} error(s):")
        for e in analysis["errors"][:3]:
            print(f"      {e}")
    if analysis["silences"]:
        issues_found = True
        print(f"  ✗ {analysis['silences']} silence turn(s)")
    if analysis["repeated_user"]:
        issues_found = True
        print(f"  ✗ {len(analysis['repeated_user'])} repeated user request(s):")
        for r in analysis["repeated_user"][:3]:
            print(f"      {r}")

    if not issues_found:
        print("  ✓ no obvious issues from transcript heuristics")

    if metrics:
        print()
        print(f"Metrics (data/call-metrics.jsonl):")
        print(f"  duration: {metrics.get('durationMs', 0) // 1000}s")
        print(f"  isOwner: {metrics.get('isOwner')}, isMeeting: {metrics.get('isMeeting')}")
        print(f"  tool calls: {metrics.get('toolCount', 0)}")
        if metrics.get("toolCalls"):
            for tc in metrics["toolCalls"][:5]:
                print(f"    - {tc.get('name')} ({tc.get('durationMs', 0)}ms)")
        print(f"  events: {len(metrics.get('events', []))}")
    elif args.metrics:
        print()
        print("  (no entry in data/call-metrics.jsonl — call predates PR #223 or metrics file missing)")

    if args.full_transcript:
        print()
        print("Transcript:")
        print(transcript)

    return 0 if not issues_found else 1


if __name__ == "__main__":
    sys.exit(main())
