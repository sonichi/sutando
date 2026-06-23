#!/usr/bin/env python3
"""Inspect Sutando's conversation.sqlite.

Examples:
  python3 scripts/inspect-conversation-sqlite.py
  python3 scripts/inspect-conversation-sqlite.py --session-id session_1780531538944
  python3 scripts/inspect-conversation-sqlite.py --limit 50 --db /path/to/workspace/data/conversation.sqlite
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Iterable


def default_db_path() -> Path:
    try:
        repo = Path(__file__).resolve().parent.parent
        ws = subprocess.check_output(
            ["bash", "scripts/sutando-config.sh", "workspace"],
            cwd=str(repo), text=True, stderr=subprocess.DEVNULL,
        ).strip()
        return Path(ws) / "data" / "conversation.sqlite"
    except Exception:
        return Path.home() / ".sutando" / "workspace" / "data" / "conversation.sqlite"


def expand_path(raw: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def rows(conn: sqlite3.Connection, sql: str, params: Iterable[object] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, tuple(params)))


def print_table(title: str, data: list[sqlite3.Row]) -> None:
    print()
    print(f"== {title} ==")
    if not data:
        print("(none)")
        return

    headers = list(data[0].keys())
    widths = {h: len(h) for h in headers}
    rendered: list[dict[str, str]] = []
    for row in data:
        item: dict[str, str] = {}
        for h in headers:
            value = "" if row[h] is None else str(row[h]).replace("\n", " ")
            if len(value) > 120:
                value = value[:117] + "..."
            item[h] = value
            widths[h] = min(120, max(widths[h], len(value)))
        rendered.append(item)

    print("  ".join(h.ljust(widths[h]) for h in headers))
    print("  ".join("-" * widths[h] for h in headers))
    for item in rendered:
        print("  ".join(item[h].ljust(widths[h]) for h in headers))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(default_db_path()), help="Path to conversation.sqlite")
    parser.add_argument("--limit", type=int, default=20, help="Rows per section")
    parser.add_argument("--session-id", help="Show rows for one session_id")
    args = parser.parse_args()

    db_path = expand_path(args.db)
    if not db_path.exists():
        print(f"conversation sqlite not found: {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    print(f"DB: {db_path}")

    if args.session_id:
        print_table(
            f"voice rows for {args.session_id}",
            rows(
                conn,
                """
                SELECT
                  id,
                  datetime(ts_unix,'unixepoch','localtime') AS time,
                  kind,
                  text,
                  duration_ms
                FROM voice
                WHERE session_id = ?
                ORDER BY id
                LIMIT ?
                """,
                (args.session_id, args.limit),
            ),
        )
        print_table(
            f"session events for {args.session_id}",
            rows(
                conn,
                """
                SELECT
                  datetime(ts_unix,'unixepoch','localtime') AS time,
                  source,
                  session_id,
                  event_name
                FROM session_events
                WHERE session_id = ?
                ORDER BY ts_unix
                LIMIT ?
                """,
                (args.session_id, args.limit),
            ),
        )
    else:
        print_table(
            "recent voice rows",
            rows(
                conn,
                """
                SELECT
                  id,
                  datetime(ts_unix,'unixepoch','localtime') AS time,
                  kind,
                  text,
                  duration_ms,
                  session_id
                FROM voice
                ORDER BY id DESC
                LIMIT ?
                """,
                (args.limit,),
            ),
        )
        print_table(
            "recent sessions",
            rows(
                conn,
                """
                SELECT
                  datetime(ts_unix,'unixepoch','localtime') AS time,
                  source,
                  session_id,
                  duration_ms,
                  transcript_lines,
                  tool_count,
                  pending_tasks
                FROM sessions
                ORDER BY ts_unix DESC
                LIMIT ?
                """,
                (args.limit,),
            ),
        )
        print_table(
            "recent session events",
            rows(
                conn,
                """
                SELECT
                  datetime(ts_unix,'unixepoch','localtime') AS time,
                  source,
                  session_id,
                  event_name
                FROM session_events
                ORDER BY ts_unix DESC
                LIMIT ?
                """,
                (args.limit,),
            ),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
