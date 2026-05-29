#!/usr/bin/env python3
"""Tests for scripts/dedup-conversation-store.py — row dedup + idempotent import.

Run: python3 tests/conversation-store-dedup.test.py
Exit: 0 on pass, 1 on fail.
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Load the dedup module without executing __main__.
_SCRIPT = ROOT / "scripts" / "dedup-conversation-store.py"


def _load_dedup():
    """Import dedup-conversation-store as a module (bypasses __main__ guard)."""
    spec = importlib.util.spec_from_file_location("dedup_conversation_store", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Point module's DEFAULT_DB at /dev/null so module-level code doesn't
    # touch any real workspace DB during import.
    os.environ.setdefault("SUTANDO_CONVERSATION_DB", "/dev/null")
    spec.loader.exec_module(mod)
    return mod


_dedup = _load_dedup()
_count_dupes = _dedup._count_dupes
_delete_dupes = _dedup._delete_dupes
_table_exists = _dedup._table_exists


def _make_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    db.executescript("""
        CREATE TABLE voice (
            ts_unix REAL NOT NULL,
            kind    TEXT NOT NULL,
            text    TEXT,
            session_id TEXT
        );
        CREATE TABLE conversation (
            ts_unix REAL NOT NULL,
            role    TEXT NOT NULL,
            text    TEXT NOT NULL,
            session_id TEXT
        );
    """)
    db.commit()
    return db


class TestCountDupes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dedup-test-"))
        self.db = _make_db(self.tmp / "conv.sqlite")

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_dupes_returns_zero(self):
        self.db.execute(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (?, ?, ?, ?)",
            (1.0, "user", "hello", "s1"),
        )
        self.db.commit()
        n = _count_dupes(self.db, "voice",
                         ["ts_unix", "kind", "COALESCE(text,'')", "COALESCE(session_id,'')"])
        self.assertEqual(n, 0)

    def test_identical_rows_counted_correctly(self):
        row = (1.0, "user", "hello", "s1")
        self.db.executemany(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (?, ?, ?, ?)",
            [row, row, row],
        )
        self.db.commit()
        n = _count_dupes(self.db, "voice",
                         ["ts_unix", "kind", "COALESCE(text,'')", "COALESCE(session_id,'')"])
        # 3 rows, 1 keeper → 2 duplicates
        self.assertEqual(n, 2)

    def test_different_rows_not_counted(self):
        self.db.executemany(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (?, ?, ?, ?)",
            [(1.0, "user", "a", "s1"), (2.0, "assistant", "b", "s1")],
        )
        self.db.commit()
        n = _count_dupes(self.db, "voice",
                         ["ts_unix", "kind", "COALESCE(text,'')", "COALESCE(session_id,'')"])
        self.assertEqual(n, 0)

    def test_null_session_id_treated_as_empty_string(self):
        # Two rows with NULL session_id should be treated as the same group.
        row = (3.0, "tool_call", "x", None)
        self.db.executemany(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (?, ?, ?, ?)",
            [row, row],
        )
        self.db.commit()
        n = _count_dupes(self.db, "voice",
                         ["ts_unix", "kind", "COALESCE(text,'')", "COALESCE(session_id,'')"])
        self.assertEqual(n, 1)


class TestDeleteDupes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dedup-test-"))
        self.db = _make_db(self.tmp / "conv.sqlite")

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_deletes_dupes_keeps_lowest_rowid(self):
        row = (1.0, "user", "hello", "s1")
        self.db.executemany(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (?, ?, ?, ?)",
            [row, row, row],
        )
        self.db.commit()
        deleted = _delete_dupes(self.db, "voice",
                                ["ts_unix", "kind", "COALESCE(text,'')", "COALESCE(session_id,'')"])
        self.db.commit()
        self.assertEqual(deleted, 2)
        remaining = self.db.execute("SELECT rowid FROM voice").fetchall()
        self.assertEqual(len(remaining), 1)
        # Lowest rowid kept (rowid=1)
        self.assertEqual(remaining[0][0], 1)

    def test_no_dupes_deletes_nothing(self):
        self.db.executemany(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (?, ?, ?, ?)",
            [(1.0, "user", "a", "s1"), (2.0, "user", "b", "s1")],
        )
        self.db.commit()
        deleted = _delete_dupes(self.db, "voice",
                                ["ts_unix", "kind", "COALESCE(text,'')", "COALESCE(session_id,'')"])
        self.db.commit()
        self.assertEqual(deleted, 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM voice").fetchone()[0], 2)


class TestTableExists(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dedup-test-"))
        self.db = _make_db(self.tmp / "conv.sqlite")

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_existing_table_returns_true(self):
        self.assertTrue(_table_exists(self.db, "voice"))

    def test_missing_table_returns_false(self):
        self.assertFalse(_table_exists(self.db, "nonexistent_table"))


class TestMainCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dedup-cli-test-"))
        db_path = self.tmp / "conv.sqlite"
        db = _make_db(db_path)
        row = (1.0, "user", "hello", "s1")
        db.executemany(
            "INSERT INTO voice (ts_unix, kind, text, session_id) VALUES (?, ?, ?, ?)",
            [row, row, row],
        )
        db.commit()
        db.close()
        self.db_path = db_path

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_main(self, *args):
        saved = sys.argv[:]
        sys.argv = ["dedup-conversation-store.py", "--db", str(self.db_path), "--force"] + list(args)
        try:
            return _dedup.main()
        finally:
            sys.argv = saved

    def test_dry_run_does_not_delete(self):
        rc = self._run_main()  # no --commit
        self.assertEqual(rc, 0)
        db = sqlite3.connect(str(self.db_path))
        count = db.execute("SELECT COUNT(*) FROM voice").fetchone()[0]
        db.close()
        self.assertEqual(count, 3, "dry-run must not delete rows")

    def test_commit_deletes_dupes(self):
        rc = self._run_main("--commit")
        self.assertEqual(rc, 0)
        db = sqlite3.connect(str(self.db_path))
        count = db.execute("SELECT COUNT(*) FROM voice").fetchone()[0]
        db.close()
        self.assertEqual(count, 1, "--commit must remove duplicate rows")

    def test_missing_db_exits_cleanly(self):
        saved = sys.argv[:]
        sys.argv = ["dedup-conversation-store.py", "--db", "/nonexistent/path.sqlite", "--force"]
        try:
            rc = _dedup.main()
        finally:
            sys.argv = saved
        self.assertEqual(rc, 0, "missing DB should exit 0 with 'nothing to do'")

    def test_idempotent_second_run(self):
        # First run with --commit clears dupes; second run finds nothing to do.
        self._run_main("--commit")
        db = sqlite3.connect(str(self.db_path))
        before = db.execute("SELECT COUNT(*) FROM voice").fetchone()[0]
        db.close()
        self._run_main("--commit")
        db = sqlite3.connect(str(self.db_path))
        after = db.execute("SELECT COUNT(*) FROM voice").fetchone()[0]
        db.close()
        self.assertEqual(before, after, "second --commit run must be idempotent")


if __name__ == "__main__":
    unittest.main()
