#!/usr/bin/env python3
"""
Dedup migration for conversation.sqlite (#941 Part 1).

The import scripts (import-conversation-log.py, import-session-metrics.py)
INSERT without dedup guards, so re-running them duplicates rows. This script
finds and removes content-identical duplicate rows, keeping the lowest rowid.

Usage:
    python3 scripts/dedup-conversation-store.py          # dry-run (count only)
    python3 scripts/dedup-conversation-store.py --commit  # actually delete dupes
    python3 scripts/dedup-conversation-store.py --force   # skip liveness check

After running --commit with no errors, the UNIQUE INDEXes added in #941 Part 2
can be applied without error.
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from workspace_default import resolve_workspace  # noqa: E402

WORKSPACE = resolve_workspace(migrate=False)
DEFAULT_DB = Path(
    os.environ.get(
        "SUTANDO_CONVERSATION_DB",
        str(WORKSPACE / "data" / "conversation.sqlite"),
    )
)

LIVE_PROCESS_PATTERNS = [
    "voice-agent",
    "conversation-server",
    "phone-conversation",
]

# (table, dedup key columns) — keep the row with the lowest rowid per group.
# COALESCE wraps nullable columns so two NULLs compare equal (NULLs are
# never equal in SQLite's default comparators).
TABLES: list[tuple[str, list[str]]] = [
    # Live voice-agent tables (conversation-store.ts)
    ("voice",          ["ts_unix", "kind", "COALESCE(text,'')", "COALESCE(session_id,'')"]),
    ("phone",          ["ts_unix", "kind", "COALESCE(text,'')", "COALESCE(session_id,'')"]),
    ("discord_voice",  ["ts_unix", "kind", "COALESCE(text,'')", "COALESCE(session_id,'')"]),
    ("sessions",       ["ts_unix", "source", "COALESCE(session_id,'')", "COALESCE(call_sid,'')"]),
    ("session_events", ["ts_unix", "source", "COALESCE(session_id,'')", "COALESCE(call_sid,'')", "event_name"]),
    # Legacy importer table (import-conversation-log.py)
    ("conversation",   ["ts_unix", "role", "text", "COALESCE(session_id,'')"]),
    # tool_calls table (import-session-metrics.py) — no rowid guard needed;
    # tool_call records lack a stable unique key (same tool can fire at same ms),
    # so we skip dedup for this table.
]


def _live_processes() -> list[str]:
    try:
        out = subprocess.check_output(["pgrep", "-fl"] + LIVE_PROCESS_PATTERNS[:1], text=True)
    except subprocess.CalledProcessError:
        out = ""
    hits: list[str] = []
    try:
        out = subprocess.check_output(["pgrep", "-fl", "voice-agent\\|conversation-server\\|phone-conversation"], text=True)
    except subprocess.CalledProcessError:
        out = ""
    for pattern in LIVE_PROCESS_PATTERNS:
        try:
            result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                hits.append(pattern)
        except Exception:
            pass
    return hits


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _count_dupes(db: sqlite3.Connection, table: str, key_cols: list[str]) -> int:
    key_expr = ", ".join(key_cols)
    row = db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE rowid NOT IN "
        f"(SELECT MIN(rowid) FROM {table} GROUP BY {key_expr})"
    ).fetchone()
    return row[0] if row else 0


def _delete_dupes(db: sqlite3.Connection, table: str, key_cols: list[str]) -> int:
    key_expr = ", ".join(key_cols)
    cur = db.execute(
        f"DELETE FROM {table} WHERE rowid NOT IN "
        f"(SELECT MIN(rowid) FROM {table} GROUP BY {key_expr})"
    )
    return cur.rowcount


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true", help="Actually delete duplicate rows (default: dry-run)")
    ap.add_argument("--force", action="store_true", help="Skip liveness check (dangerous if live writers exist)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to conversation.sqlite")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"DB not found: {args.db}")
        print("Nothing to do.")
        return 0

    if not args.force:
        live = _live_processes()
        if live:
            print(f"ERROR: live processes detected: {', '.join(live)}")
            print("Stop them first or pass --force to override.")
            return 1

    db = sqlite3.connect(str(args.db))
    db.execute("PRAGMA journal_mode=WAL")

    total_dupes = 0
    any_table_missing = False

    for table, key_cols in TABLES:
        if not _table_exists(db, table):
            print(f"  {table:<20} skipped (table absent)")
            any_table_missing = True
            continue
        n = _count_dupes(db, table, key_cols)
        total_dupes += n
        if not args.commit:
            print(f"  {table:<20} {n:>6} duplicate row(s) would be removed")
        else:
            deleted = _delete_dupes(db, table, key_cols)
            print(f"  {table:<20} {deleted:>6} duplicate row(s) removed")

    db.commit()
    db.close()

    if not args.commit:
        print()
        if total_dupes == 0:
            print("No duplicates found — nothing to do.")
        else:
            print(f"Total: {total_dupes} duplicate row(s) found.")
            print("Re-run with --commit to delete them.")
        if any_table_missing:
            print("(Some tables absent — run voice-agent once to create them.)")
    else:
        print()
        print(f"Done. {total_dupes} row(s) removed total.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
