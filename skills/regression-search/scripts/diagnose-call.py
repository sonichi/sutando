#!/usr/bin/env python3
"""diagnose-call.py — deep diagnosis of a single call.

Looks up a call by SID (full or last-N suffix), reads the transcript and any
metrics from data/conversation.sqlite, and prints what went wrong: silences,
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
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

# conversation.sqlite is per-user runtime state — it lives under the resolved
# workspace ($SUTANDO_WORKSPACE), the same tree the runtime writers use, not
# the repo checkout. Honor SUTANDO_CONVERSATION_DB, matching diagnose.py and
# the import-* scripts.
DB_FILE = Path(os.environ.get(
    "SUTANDO_CONVERSATION_DB",
    resolve_workspace(migrate=False) / "data" / "conversation.sqlite"))

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


def _ts_iso(ts_unix) -> str:
    return datetime.fromtimestamp(
        ts_unix or 0, tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")


def _build_transcript(conn: sqlite3.Connection, session_id: str) -> str:
    """Reconstruct a `Role: text` transcript from the per-surface tables.

    Post-#1051 (per-surface schema): utterances + tool calls live in the
    voice / phone / discord_voice tables under the `kind` column. We union
    all three so a session_id from any surface resolves. Tool-call rows
    (kind='tool_call') are skipped — the transcript is human dialogue only.
    """
    if not session_id:
        return ""
    rows = conn.execute(
        """
        SELECT kind, text FROM (
          SELECT ts_unix, kind, text, session_id FROM voice
          UNION ALL
          SELECT ts_unix, kind, text, session_id FROM phone
          UNION ALL
          SELECT ts_unix, kind, text, session_id FROM discord_voice
        )
        WHERE session_id = ? AND kind != 'tool_call'
        ORDER BY ts_unix
        """,
        (session_id,),
    ).fetchall()
    lines = []
    for kind, text in rows:
        k = (kind or "").lower()
        prefix = "Sutando" if k in ("assistant", "sutando", "agent", "model") else "User"
        lines.append(f"{prefix}: {text or ''}")
    return "\n".join(lines)


def find_call(sid_query: str) -> Optional[dict]:
    """Match by full SID or by suffix (12 chars is enough to disambiguate).

    Returns a dict with callSid / timestamp / transcript keys, reconstructed
    from the sessions + conversation tables of data/conversation.sqlite.
    """
    if not DB_FILE.exists():
        return None
    conn = sqlite3.connect(str(DB_FILE))
    try:
        rows = conn.execute(
            "SELECT ts_unix, source, session_id, call_sid FROM sessions ORDER BY ts_unix"
        ).fetchall()
        candidates = []
        for ts_unix, source, session_id, call_sid in rows:
            sid = call_sid or session_id or ""
            if sid == sid_query or sid.endswith(sid_query):
                candidates.append({
                    "ts_unix": ts_unix,
                    "source": source,
                    "session_id": session_id,
                    "callSid": sid,
                })
        if not candidates:
            return None
        if len(candidates) > 1:
            print(f"⚠ {len(candidates)} calls match suffix '{sid_query}' — using most recent", file=sys.stderr)
            candidates.sort(key=lambda c: c["ts_unix"] or 0)
        chosen = candidates[-1]
        return {
            "callSid": chosen["callSid"],
            "timestamp": _ts_iso(chosen["ts_unix"]),
            "transcript": _build_transcript(conn, chosen["session_id"]),
        }
    finally:
        conn.close()


def find_metrics(call_sid: str) -> Optional[dict]:
    if not DB_FILE.exists():
        return None
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT ts_unix, source, session_id, call_sid, caller, is_owner, "
            "is_meeting, duration_ms, transcript_lines, tool_count, pending_tasks "
            "FROM sessions "
            "WHERE call_sid = ? OR session_id = ? ORDER BY ts_unix",
            (call_sid, call_sid),
        ).fetchone()
        if not row:
            return None
        # Tool calls now live as surface-table rows with kind='tool_call'
        # (per #1052 — the redundant sessions.tool_calls JSON column is gone).
        # Rebuild the list from the matching surface table by session_id.
        sid = row["session_id"]
        tool_call_rows = conn.execute(
            "SELECT ts_unix, text AS name, duration_ms FROM ("
            "  SELECT ts_unix, text, duration_ms, session_id, kind FROM voice"
            "  UNION ALL"
            "  SELECT ts_unix, text, duration_ms, session_id, kind FROM phone"
            "  UNION ALL"
            "  SELECT ts_unix, text, duration_ms, session_id, kind FROM discord_voice"
            ") WHERE session_id = ? AND kind = 'tool_call' ORDER BY ts_unix",
            (sid,),
        ).fetchall()
        tool_calls = [
            {
                "name": r["name"],
                "durationMs": r["duration_ms"],
                "timestamp": _ts_iso(r["ts_unix"]),
            }
            for r in tool_call_rows
        ]
        # Lifecycle events now live in session_events (per #1052 — the
        # redundant sessions.events JSON column is gone). Query by
        # session_id OR call_sid since callers may pass either.
        event_rows = conn.execute(
            "SELECT ts_unix, event_name FROM session_events "
            "WHERE session_id = ? OR call_sid = ? ORDER BY ts_unix",
            (sid, row["call_sid"]),
        ).fetchall()
        events = [
            {"event": r["event_name"], "timestamp": _ts_iso(r["ts_unix"])}
            for r in event_rows
        ]
    finally:
        conn.close()
    return {
        "timestamp": _ts_iso(row["ts_unix"]),
        "callSid": row["call_sid"] or row["session_id"],
        "sessionId": row["session_id"],
        "source": row["source"],
        "caller": row["caller"],
        "isOwner": bool(row["is_owner"]) if row["is_owner"] is not None else None,
        "isMeeting": bool(row["is_meeting"]) if row["is_meeting"] is not None else None,
        "durationMs": row["duration_ms"] or 0,
        "transcriptLines": row["transcript_lines"] or 0,
        "toolCount": row["tool_count"] or 0,
        "pendingTasks": row["pending_tasks"] or 0,
        "toolCalls": tool_calls,
        "events": events,
    }


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
    parser.add_argument("--metrics", action="store_true", help="Also show data/conversation.sqlite session metrics if available")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--full-transcript", action="store_true", help="Print the full transcript")
    args = parser.parse_args()

    call = find_call(args.sid)
    if not call:
        print(f"No call matches '{args.sid}' in {DB_FILE}", file=sys.stderr)
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
        print(f"Metrics (data/conversation.sqlite):")
        print(f"  duration: {metrics.get('durationMs', 0) // 1000}s")
        print(f"  isOwner: {metrics.get('isOwner')}, isMeeting: {metrics.get('isMeeting')}")
        print(f"  tool calls: {metrics.get('toolCount', 0)}")
        if metrics.get("toolCalls"):
            for tc in metrics["toolCalls"][:5]:
                print(f"    - {tc.get('name')} ({tc.get('durationMs', 0)}ms)")
        print(f"  events: {len(metrics.get('events', []))}")
    elif args.metrics:
        print()
        print("  (no entry in data/conversation.sqlite — call predates PR #223 or db missing)")

    if args.full_transcript:
        print()
        print("Transcript:")
        print(transcript)

    return 0 if not issues_found else 1


if __name__ == "__main__":
    sys.exit(main())
