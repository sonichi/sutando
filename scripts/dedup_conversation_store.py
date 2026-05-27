#!/usr/bin/env python3
"""
Dedup migration for conversation.sqlite (closes #941 Part 1).

Removes duplicate rows from the per-surface event tables, sessions, and
session_events before adding UNIQUE INDEXes to those tables.  Duplicate
rows are produced when import scripts are re-run without --reload (the
documented behaviour), or when multiple writers race on the same session.

Natural-key tuples used to detect duplicates:
  voice / phone / discord_voice  (ts_unix, session_id, kind, text)
  sessions                        (ts_unix, source, session_id)
  session_events                  (ts_unix, source, session_id, event_name)

The lowest rowid wins — it preserves insertion order as closely as possible.

Usage:
    python3 scripts/dedup-conversation-store.py             # dry-run
    python3 scripts/dedup-conversation-store.py --commit    # apply
    python3 scripts/dedup-conversation-store.py --db /path  # override DB path

Safety:
  - Defaults to dry-run; --commit required to write.
  - Aborts if voice-agent or phone-conversation-server processes are alive
    (same guard used by --reload in the import scripts).
  - Wraps every table in a transaction; rolls back on any error.
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from workspace_default import resolve_workspace  # noqa: E402

WORKSPACE = resolve_workspace(migrate=False)
DEFAULT_DB = WORKSPACE / "data" / "conversation.sqlite"

# Natural keys: the minimal set of columns whose combination must be unique.
# For the per-surface tables ts_unix alone is NOT sufficient (two tool_call
# rows in the same millisecond differ by text); text alone is not sufficient;
# (ts_unix, session_id, kind, text) together form a practical unique tuple.
SURFACE_TABLES = ("voice", "phone", "discord_voice")
SURFACE_KEY = "ts_unix, session_id, kind, text"

SESSION_KEY = "ts_unix, source, session_id"
EVENT_KEY = "ts_unix, source, session_id, event_name"


def _is_alive(pattern: str) -> bool:
    try:
        out = subprocess.check_output(["pgrep", "-f", pattern], text=True)
        return bool(out.strip())
    except subprocess.CalledProcessError:
        return False


def _check_services(force: bool) -> None:
    running = []
    if _is_alive("voice-agent"):
        running.append("voice-agent")
    if _is_alive("conversation-server"):
        running.append("conversation-server (phone)")
    if running and not force:
        print(
            f"Error: live service(s) detected: {', '.join(running)}.\n"
            "Stop them before deduplicating, or pass --force to override.",
            file=sys.stderr,
        )
        sys.exit(1)


def _count_dups(cur: sqlite3.Cursor, table: str, key: str) -> int:
    cur.execute(
        f"SELECT SUM(cnt - 1) FROM "
        f"(SELECT COUNT(*) AS cnt FROM {table} GROUP BY {key} HAVING cnt > 1)"
    )
    row = cur.fetchone()
    return int(row[0] or 0)


def _dedup_table(
    cur: sqlite3.Cursor,
    table: str,
    key: str,
    dry_run: bool,
) -> int:
    """Delete duplicate rows, keeping the lowest rowid per key group.
    Returns the number of rows removed."""
    total = _count_dups(cur, table, key)
    if total == 0:
        return 0
    if dry_run:
        return total
    # Keep the row with the smallest rowid in each duplicate group.
    cur.execute(
        f"DELETE FROM {table} WHERE rowid NOT IN "
        f"(SELECT MIN(rowid) FROM {table} GROUP BY {key})"
    )
    return cur.rowcount


def run(db_path: Path, dry_run: bool, force: bool) -> None:
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        sys.exit(0)

    _check_services(force)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    total_removed = 0
    try:
        cur.execute("BEGIN")
        for table in SURFACE_TABLES:
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            if not cur.fetchone():
                print(f"  {table}: (table absent — skip)")
                continue
            removed = _dedup_table(cur, table, SURFACE_KEY, dry_run)
            total_removed += removed
            label = "would remove" if dry_run else "removed"
            print(f"  {table}: {label} {removed} duplicate row(s)")

        for table, key in [("sessions", SESSION_KEY), ("session_events", EVENT_KEY)]:
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            if not cur.fetchone():
                print(f"  {table}: (table absent — skip)")
                continue
            removed = _dedup_table(cur, table, key, dry_run)
            total_removed += removed
            label = "would remove" if dry_run else "removed"
            print(f"  {table}: {label} {removed} duplicate row(s)")

        if dry_run:
            conn.rollback()
            print(
                f"\nDry-run: {total_removed} duplicate row(s) found. "
                "Re-run with --commit to apply."
            )
        else:
            conn.commit()
            print(f"\nCommitted: {total_removed} duplicate row(s) removed.")
    except Exception as exc:
        conn.rollback()
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Apply the deduplication (default: dry-run only).",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help=f"Override DB path (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the live-service check.",
    )
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DEFAULT_DB
    run(db_path=db_path, dry_run=not args.commit, force=args.force)


if __name__ == "__main__":
    main()
