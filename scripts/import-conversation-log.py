#!/usr/bin/env python3
"""
One-shot importer: conversation.log (text) → conversation.sqlite (table `conversation`).

Format expected per line: `<ISO ts>|<role>|<text>`

Idempotency: by default, INSERT every line — re-running duplicates rows.
Pass --reload to TRUNCATE the table first and reimport from scratch.
Pass --dry-run to count rows without writing.

--reload deletes then reimports; a live writer racing the DELETE would lose
rows. It therefore requires the voice/phone services to be stopped — the
script aborts if it detects those processes. Pass --force to override.

Usage:
    python3 scripts/import-conversation-log.py
    python3 scripts/import-conversation-log.py --reload
    python3 scripts/import-conversation-log.py --src /path/to/conversation.log
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

# DB + conversation.log live under the resolved workspace (~/.sutando/workspace),
# the same tree the runtime writers use — not the repo root.
WORKSPACE = resolve_workspace(migrate=False)
DEFAULT_SRC = WORKSPACE / "conversation.log"
DEFAULT_DB = Path(os.environ.get("SUTANDO_CONVERSATION_DB", WORKSPACE / "data" / "conversation.sqlite"))

# Process patterns that write conversation.sqlite live. --reload must not run
# while any of these is up (see module docstring).
_LIVE_WRITER_PATTERNS = [
    ("voice-agent.ts", "voice-agent"),
    ("conversation-server.ts", "phone conversation-server"),
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


def parse_iso_to_unix(ts_str: str) -> float | None:
    try:
        # ISO with trailing Z (UTC)
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        return datetime.fromisoformat(ts_str).timestamp()
    except Exception:
        return None


def read_log(src: Path) -> tuple[list[tuple], int, int, dict[str, int]]:
    """Parse conversation.log → (rows, parsed, skipped_unparseable, role_counts)."""
    rows: list[tuple] = []
    parsed = 0
    skipped = 0
    role_counts: dict[str, int] = {}
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("|", 2)
            if len(parts) != 3:
                skipped += 1
                continue
            ts_str, role, text = parts
            ts_unix = parse_iso_to_unix(ts_str)
            if ts_unix is None:
                skipped += 1
                continue
            rows.append((ts_unix, role, text, None))
            parsed += 1
            role_counts[role] = role_counts.get(role, 0) + 1
    return rows, parsed, skipped, role_counts


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS conversation (
            ts_unix    REAL NOT NULL,
            role       TEXT NOT NULL,
            text       TEXT NOT NULL,
            session_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_conversation_ts ON conversation(ts_unix);
        CREATE INDEX IF NOT EXISTS idx_conversation_role_ts ON conversation(role, ts_unix);
        CREATE INDEX IF NOT EXISTS idx_conversation_session ON conversation(session_id, ts_unix);
    """)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC, help="conversation.log path")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="sqlite db path")
    ap.add_argument("--reload", action="store_true", help="DELETE existing rows before import")
    ap.add_argument("--dry-run", action="store_true", help="parse but don't write")
    ap.add_argument("--force", action="store_true",
                    help="skip the live-writer guard on --reload")
    args = ap.parse_args()

    if not args.src.exists():
        print(f"error: source not found: {args.src}", file=sys.stderr)
        return 1

    # dry-run: parse + report only, never touch the DB.
    if args.dry_run:
        _, parsed, skipped, role_counts = read_log(args.src)
        print(f"parsed: {parsed} rows  (skipped unparseable: {skipped})")
        print(f"roles:  {sorted(role_counts.items(), key=lambda x: -x[1])}")
        print("(dry-run; no writes)")
        return 0

    # --reload truncates then reimports — a live writer racing the DELETE
    # would have its rows deleted-without-reimport. Require services stopped.
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
            before = db.execute("SELECT COUNT(*) FROM conversation").fetchone()[0]
            db.execute("DELETE FROM conversation")
            print(f"reload: deleted {before} existing rows")
        # Read the log AFTER the DELETE (and under the write lock) so a row
        # appended just before this point is captured by the reimport rather
        # than deleted-without-reimport.
        rows, parsed, skipped, role_counts = read_log(args.src)
        print(f"parsed: {parsed} rows  (skipped unparseable: {skipped})")
        print(f"roles:  {sorted(role_counts.items(), key=lambda x: -x[1])}")
        db.executemany(
            "INSERT INTO conversation (ts_unix, role, text, session_id) VALUES (?, ?, ?, ?)",
            rows,
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        db.close()
        raise

    total = db.execute("SELECT COUNT(*) FROM conversation").fetchone()[0]
    db.close()
    print(f"wrote {parsed} rows → {args.db}  (table total now: {total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
