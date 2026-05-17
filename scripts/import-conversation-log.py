#!/usr/bin/env python3
"""
One-shot importer: conversation.log (text) → conversation.sqlite (table `conversation`).

Format expected per line: `<ISO ts>|<role>|<text>`

Idempotency: by default, INSERT every line — re-running duplicates rows.
Pass --reload to TRUNCATE the table first and reimport from scratch.
Pass --dry-run to count rows without writing.

Usage:
    python3 scripts/import-conversation-log.py
    python3 scripts/import-conversation-log.py --reload
    python3 scripts/import-conversation-log.py --src /path/to/conversation.log
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_SRC = REPO / "conversation.log"
DEFAULT_DB = Path(os.environ.get("SUTANDO_CONVERSATION_DB", REPO / "data" / "conversation.sqlite"))


def parse_iso_to_unix(ts_str: str) -> float | None:
    try:
        # ISO with trailing Z (UTC)
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str).timestamp()
    except Exception:
        return None


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS conversation (
            ts_unix    REAL NOT NULL,
            role       TEXT NOT NULL,
            text       TEXT NOT NULL,
            session_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ts ON conversation(ts_unix);
        CREATE INDEX IF NOT EXISTS idx_role_ts ON conversation(role, ts_unix);
        CREATE INDEX IF NOT EXISTS idx_session ON conversation(session_id, ts_unix);
    """)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC, help="conversation.log path")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="sqlite db path")
    ap.add_argument("--reload", action="store_true", help="DELETE existing rows before import")
    ap.add_argument("--dry-run", action="store_true", help="parse but don't write")
    args = ap.parse_args()

    if not args.src.exists():
        print(f"error: source not found: {args.src}", file=sys.stderr)
        return 1

    args.db.parent.mkdir(parents=True, exist_ok=True)

    parsed = 0
    skipped_unparseable = 0
    role_counts: dict[str, int] = {}

    with open(args.src, "r", encoding="utf-8", errors="replace") as f:
        rows = []
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) != 3:
                skipped_unparseable += 1
                continue
            ts_str, role, text = parts
            ts_unix = parse_iso_to_unix(ts_str)
            if ts_unix is None:
                skipped_unparseable += 1
                continue
            rows.append((ts_unix, role, text, None))
            parsed += 1
            role_counts[role] = role_counts.get(role, 0) + 1

    print(f"parsed: {parsed} rows  (skipped unparseable: {skipped_unparseable})")
    print(f"roles:  {sorted(role_counts.items(), key=lambda x: -x[1])}")

    if args.dry_run:
        print("(dry-run; no writes)")
        return 0

    db = sqlite3.connect(str(args.db))
    db.execute("PRAGMA journal_mode = WAL")
    ensure_schema(db)
    if args.reload:
        before = db.execute("SELECT COUNT(*) FROM conversation").fetchone()[0]
        db.execute("DELETE FROM conversation")
        print(f"reload: deleted {before} existing rows")
    db.executemany(
        "INSERT INTO conversation (ts_unix, role, text, session_id) VALUES (?, ?, ?, ?)",
        rows,
    )
    db.commit()
    total = db.execute("SELECT COUNT(*) FROM conversation").fetchone()[0]
    db.close()
    print(f"wrote {parsed} rows → {args.db}  (table total now: {total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
