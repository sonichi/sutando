#!/usr/bin/env python3
"""
Tests for scripts/dedup-conversation-store.py (closes #941 Part 1).

Guards:
  1. Duplicate rows in voice/phone/discord_voice are removed; distinct rows survive.
  2. Duplicate rows in sessions are removed; distinct rows survive.
  3. Duplicate rows in session_events are removed; distinct rows survive.
  4. Dry-run mode reports duplicates without changing the DB.
  5. No-op on a clean DB (zero duplicates → zero rows removed).
  6. The lowest rowid survives for each duplicate group.
  7. Tables absent from the DB are silently skipped (no crash).
"""

import sqlite3
import sys
import tempfile
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

# Patch workspace_default before importing dedup module
import types
_fake_ws = types.ModuleType("workspace_default")
_fake_ws.resolve_workspace = lambda migrate=False: Path(tempfile.mkdtemp())
sys.modules.setdefault("workspace_default", _fake_ws)

import dedup_conversation_store as dedup  # type: ignore

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}")


def fail(label: str, reason: str) -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}: {reason}")


def _make_db(path: Path) -> sqlite3.Connection:
    """Create a minimal DB with all 5 tables."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE voice (
            id INTEGER PRIMARY KEY,
            ts_unix REAL NOT NULL,
            kind TEXT NOT NULL,
            text TEXT,
            duration_ms INTEGER,
            session_id TEXT
        );
        CREATE TABLE phone (
            id INTEGER PRIMARY KEY,
            ts_unix REAL NOT NULL,
            kind TEXT NOT NULL,
            text TEXT,
            duration_ms INTEGER,
            session_id TEXT
        );
        CREATE TABLE discord_voice (
            id INTEGER PRIMARY KEY,
            ts_unix REAL NOT NULL,
            kind TEXT NOT NULL,
            text TEXT,
            duration_ms INTEGER,
            session_id TEXT
        );
        CREATE TABLE sessions (
            ts_unix REAL NOT NULL,
            source TEXT NOT NULL,
            session_id TEXT,
            call_sid TEXT,
            caller TEXT,
            is_owner INTEGER,
            is_meeting INTEGER,
            duration_ms INTEGER NOT NULL,
            transcript_lines INTEGER,
            tool_count INTEGER,
            pending_tasks INTEGER
        );
        CREATE TABLE session_events (
            ts_unix REAL NOT NULL,
            source TEXT NOT NULL,
            session_id TEXT,
            call_sid TEXT,
            event_name TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


def test_surface_table_dups_removed():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = Path(f.name)
    try:
        conn = _make_db(db_path)
        # Insert 3 rows: 2 duplicates + 1 distinct
        conn.executemany(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (?, ?, ?, ?)",
            [
                (1000.0, "user", "hello", "s1"),   # original
                (1000.0, "user", "hello", "s1"),   # dup
                (2000.0, "agent", "world", "s1"),  # distinct
            ],
        )
        conn.commit()
        conn.close()

        dedup.run(db_path=db_path, dry_run=False, force=True)

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT ts_unix, kind, text, session_id FROM voice ORDER BY ts_unix").fetchall()
        conn.close()

        if len(rows) == 2 and rows[0] == (1000.0, "user", "hello", "s1") and rows[1] == (2000.0, "agent", "world", "s1"):
            ok("surface dup removed, distinct row survives")
        else:
            fail("surface dup removed, distinct row survives", f"got {rows}")
    finally:
        db_path.unlink(missing_ok=True)


def test_sessions_dups_removed():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = Path(f.name)
    try:
        conn = _make_db(db_path)
        conn.executemany(
            "INSERT INTO sessions (ts_unix, source, session_id, duration_ms) VALUES (?, ?, ?, ?)",
            [
                (3000.0, "voice", "sess-1", 60000),
                (3000.0, "voice", "sess-1", 60000),  # dup
                (4000.0, "phone", "sess-2", 120000),
            ],
        )
        conn.commit()
        conn.close()

        dedup.run(db_path=db_path, dry_run=False, force=True)

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT ts_unix, source, session_id FROM sessions ORDER BY ts_unix").fetchall()
        conn.close()

        if len(rows) == 2:
            ok("sessions dup removed")
        else:
            fail("sessions dup removed", f"expected 2 rows, got {len(rows)}: {rows}")
    finally:
        db_path.unlink(missing_ok=True)


def test_session_events_dups_removed():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = Path(f.name)
    try:
        conn = _make_db(db_path)
        conn.executemany(
            "INSERT INTO session_events (ts_unix, source, session_id, event_name) VALUES (?, ?, ?, ?)",
            [
                (5000.0, "voice", "s1", "session_started"),
                (5000.0, "voice", "s1", "session_started"),  # dup
                (6000.0, "voice", "s1", "session_ended"),
            ],
        )
        conn.commit()
        conn.close()

        dedup.run(db_path=db_path, dry_run=False, force=True)

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT event_name FROM session_events ORDER BY ts_unix").fetchall()
        conn.close()

        if len(rows) == 2:
            ok("session_events dup removed")
        else:
            fail("session_events dup removed", f"expected 2 rows, got {len(rows)}")
    finally:
        db_path.unlink(missing_ok=True)


def test_dry_run_does_not_modify():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = Path(f.name)
    try:
        conn = _make_db(db_path)
        conn.executemany(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (?, ?, ?, ?)",
            [(1.0, "user", "hi", "s1"), (1.0, "user", "hi", "s1")],
        )
        conn.commit()
        conn.close()

        dedup.run(db_path=db_path, dry_run=True, force=True)

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM voice").fetchone()[0]
        conn.close()

        if count == 2:
            ok("dry-run does not remove rows")
        else:
            fail("dry-run does not remove rows", f"row count changed to {count}")
    finally:
        db_path.unlink(missing_ok=True)


def test_noop_on_clean_db():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = Path(f.name)
    try:
        conn = _make_db(db_path)
        conn.executemany(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (?, ?, ?, ?)",
            [(1.0, "user", "a", "s1"), (2.0, "user", "b", "s1")],
        )
        conn.commit()
        conn.close()

        # Capture count report by running dedup
        dedup.run(db_path=db_path, dry_run=False, force=True)

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM voice").fetchone()[0]
        conn.close()

        if count == 2:
            ok("no-op on clean db")
        else:
            fail("no-op on clean db", f"row count changed from 2 to {count}")
    finally:
        db_path.unlink(missing_ok=True)


def test_lowest_rowid_survives():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = Path(f.name)
    try:
        conn = _make_db(db_path)
        conn.execute(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (?, ?, ?, ?)",
            (1.0, "user", "msg", "s1"),
        )
        first_rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (?, ?, ?, ?)",
            (1.0, "user", "msg", "s1"),
        )
        conn.commit()
        conn.close()

        dedup.run(db_path=db_path, dry_run=False, force=True)

        conn = sqlite3.connect(str(db_path))
        surviving_rowid = conn.execute("SELECT rowid FROM voice").fetchone()[0]
        conn.close()

        if surviving_rowid == first_rowid:
            ok("lowest rowid survives")
        else:
            fail("lowest rowid survives", f"expected rowid {first_rowid}, got {surviving_rowid}")
    finally:
        db_path.unlink(missing_ok=True)


def test_absent_table_skipped():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = Path(f.name)
    try:
        # Create a DB with only the voice table — other tables absent
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE voice (
                id INTEGER PRIMARY KEY,
                ts_unix REAL NOT NULL,
                kind TEXT NOT NULL,
                text TEXT,
                duration_ms INTEGER,
                session_id TEXT
            )
        """)
        conn.commit()
        conn.close()

        # Should not crash even with missing phone/discord_voice/sessions/session_events
        try:
            dedup.run(db_path=db_path, dry_run=False, force=True)
            ok("absent tables skipped without crash")
        except Exception as e:
            fail("absent tables skipped without crash", str(e))
    finally:
        db_path.unlink(missing_ok=True)


# Run all tests
test_surface_table_dups_removed()
test_sessions_dups_removed()
test_session_events_dups_removed()
test_dry_run_does_not_modify()
test_noop_on_clean_db()
test_lowest_rowid_survives()
test_absent_table_skipped()

print(f"\nResults: {PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
