#!/usr/bin/env python3
"""
One-shot importer: data/voice-metrics.jsonl + data/call-metrics.jsonl →
conversation.sqlite (`sessions` + `tool_calls` + `session_events` tables).

Companion to scripts/import-conversation-log.py. The jsonl files are
preserved on disk (per Susan's "已有的不要删") but no longer have a writer
as of #603 — this script backfills the historical rows into sqlite so the
sessions tables represent the full history of voice + phone sessions.

Idempotency: by default, INSERT every row — re-running duplicates.
Pass --reload to TRUNCATE the 3 tables first and reimport.
Pass --dry-run to count rows without writing.

--reload deletes then reimports; a live writer racing the DELETE would lose
rows. It therefore requires the voice/phone services to be stopped — the
script aborts if it detects those processes. Pass --force to override.

Usage:
    python3 scripts/import-session-metrics.py
    python3 scripts/import-session-metrics.py --reload
    python3 scripts/import-session-metrics.py --voice /path/voice-metrics.jsonl
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

# DB + jsonl archives live under the resolved workspace (~/.sutando/workspace),
# the same tree the runtime writers use — not the repo root.
WORKSPACE = resolve_workspace(migrate=False)
DEFAULT_VOICE = WORKSPACE / "data" / "voice-metrics.jsonl"
DEFAULT_CALL = WORKSPACE / "data" / "call-metrics.jsonl"
DEFAULT_DB = Path(os.environ.get("SUTANDO_CONVERSATION_DB", WORKSPACE / "data" / "conversation.sqlite"))

# Process patterns that write conversation.sqlite live. --reload must not run
# while any of these is up (see module docstring).
_LIVE_WRITER_PATTERNS = [
    ("voice-agent.ts", "voice-agent"),
    ("conversation-server.ts", "phone conversation-server"),
    ("discord-voice-server.ts", "discord-voice"),
]


def live_writers() -> list[str]:
    """Return labels of voice/phone writer processes currently running."""
    found = []
    for pat, label in _LIVE_WRITER_PATTERNS:
        try:
            r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                found.append(label)
        except Exception:
            pass
    return found


def iso_to_unix(ts: str | None) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return None


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            ts_unix          REAL NOT NULL,
            source           TEXT NOT NULL,
            session_id       TEXT,
            call_sid         TEXT,
            caller           TEXT,
            is_owner         INTEGER,
            is_meeting       INTEGER,
            duration_ms      INTEGER NOT NULL,
            transcript_lines INTEGER,
            tool_count       INTEGER,
            pending_tasks    INTEGER,
            tool_calls       TEXT,
            events           TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(ts_unix);
        CREATE INDEX IF NOT EXISTS idx_sessions_source_ts ON sessions(source, ts_unix);
        CREATE INDEX IF NOT EXISTS idx_sessions_call_sid ON sessions(call_sid);

        CREATE TABLE IF NOT EXISTS tool_calls (
            ts_unix     REAL NOT NULL,
            source      TEXT NOT NULL,
            session_id  TEXT,
            call_sid    TEXT,
            name        TEXT NOT NULL,
            duration_ms INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_tool_calls_ts ON tool_calls(ts_unix);
        CREATE INDEX IF NOT EXISTS idx_tool_calls_name_ts ON tool_calls(name, ts_unix);
        CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id, ts_unix);

        CREATE TABLE IF NOT EXISTS session_events (
            ts_unix    REAL NOT NULL,
            source     TEXT NOT NULL,
            session_id TEXT,
            call_sid   TEXT,
            event_name TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_session_events_ts ON session_events(ts_unix);
        CREATE INDEX IF NOT EXISTS idx_session_events_name_ts ON session_events(event_name, ts_unix);
        CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events(session_id, ts_unix);
    """)
    # Best-effort UNIQUE INDEXes — fail silently if duplicates exist (run
    # scripts/dedup-conversation-store.py first, then re-import for full
    # idempotency via INSERT OR IGNORE).
    for stmt in (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_unique "
        "ON sessions(ts_unix, source, COALESCE(session_id,''), COALESCE(call_sid,''))",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_session_events_unique "
        "ON session_events(ts_unix, source, COALESCE(session_id,''), COALESCE(call_sid,''), event_name)",
    ):
        try:
            db.execute(stmt)
        except Exception:
            pass


def import_file(db: sqlite3.Connection, path: Path, source_hint: str) -> tuple[int, int, int]:
    """Returns (sessions, tool_calls, events) inserted."""
    if not path.exists():
        print(f"  skip {path}: file not found")
        return 0, 0, 0
    n_s = n_t = n_e = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_unix = iso_to_unix(m.get("timestamp")) or 0
            src = m.get("source") or source_hint
            session_id = m.get("sessionId")
            call_sid = m.get("callSid")
            db.execute(
                "INSERT OR IGNORE INTO sessions (ts_unix, source, session_id, call_sid, caller, is_owner, is_meeting, "
                "duration_ms, transcript_lines, tool_count, pending_tasks, tool_calls, events) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts_unix,
                    src,
                    session_id,
                    call_sid,
                    m.get("caller"),
                    1 if m.get("isOwner") else (0 if "isOwner" in m else None),
                    1 if m.get("isMeeting") else (0 if "isMeeting" in m else None),
                    int(m.get("durationMs") or 0),
                    m.get("transcriptLines"),
                    m.get("toolCount"),
                    m.get("pendingTasks"),
                    json.dumps(m.get("toolCalls")) if m.get("toolCalls") is not None else None,
                    json.dumps(m.get("events")) if m.get("events") is not None else None,
                ),
            )
            n_s += 1
            for tc in m.get("toolCalls") or []:
                db.execute(
                    "INSERT INTO tool_calls (ts_unix, source, session_id, call_sid, name, duration_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        iso_to_unix(tc.get("timestamp")) or ts_unix,
                        src,
                        session_id,
                        call_sid,
                        str(tc.get("name") or "unknown"),
                        tc.get("durationMs"),
                    ),
                )
                n_t += 1
            for ev in m.get("events") or []:
                db.execute(
                    "INSERT OR IGNORE INTO session_events (ts_unix, source, session_id, call_sid, event_name) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        iso_to_unix(ev.get("timestamp")) or ts_unix,
                        src,
                        session_id,
                        call_sid,
                        str(ev.get("event") or "unknown"),
                    ),
                )
                n_e += 1
    return n_s, n_t, n_e


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", type=Path, default=DEFAULT_VOICE)
    ap.add_argument("--call", type=Path, default=DEFAULT_CALL)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--reload", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="skip the live-writer guard on --reload")
    args = ap.parse_args()

    # dry-run: count jsonl lines only, never touch the DB.
    if args.dry_run:
        for path in (args.voice, args.call):
            n = sum(1 for _ in open(path)) if path.exists() else 0
            print(f"  {path}: {n} jsonl lines (= sessions)")
        return 0

    # --reload truncates then reimports — a live writer racing the DELETE
    # would have its rows deleted-without-reimport. Require services stopped.
    # (The jsonl sources are frozen post-#603, but conversation.sqlite still
    # has live writers via recordSession.)
    if args.reload and not args.force:
        live = live_writers()
        if live:
            print(f"error: --reload needs the voice/phone services stopped; still running: "
                  f"{', '.join(live)}. Stop them, or pass --force to override.", file=sys.stderr)
            return 1

    args.db.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None → autocommit; we manage the transaction explicitly.
    db = sqlite3.connect(str(args.db), isolation_level=None)
    db.execute("PRAGMA journal_mode = WAL")
    ensure_schema(db)

    # One IMMEDIATE transaction holds the write lock across DELETE + reimport,
    # so the operation is atomic and serialized against any writer.
    db.execute("BEGIN IMMEDIATE")
    try:
        if args.reload:
            for t in ("sessions", "tool_calls", "session_events"):
                n = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                db.execute(f"DELETE FROM {t}")
                print(f"reload: deleted {n} from {t}")

        print(f"importing voice metrics from {args.voice}")
        vs, vt, ve = import_file(db, args.voice, "voice")
        print(f"  sessions: {vs}   tool_calls: {vt}   events: {ve}")

        print(f"importing call metrics from {args.call}")
        cs, ct, ce = import_file(db, args.call, "phone")
        print(f"  sessions: {cs}   tool_calls: {ct}   events: {ce}")

        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        db.close()
        raise

    db.close()
    print(f"done.  totals: sessions={vs+cs}  tool_calls={vt+ct}  events={ve+ce}  → {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
