#!/usr/bin/env python3
"""Tests for skills/import-claude-context/scripts/progress.py — the LLM-free
recount that keeps status.json current while the coordinator summarises.

Pins: sessions = index sessions minus `skipped_empty` in state.json, merged
summaries counted (partials not), roll-ups / entities / staged manifest
detected, the phase ladder summarizing -> rolling-up -> staged, status.json
rewritten with counts only, and a data dir without index.json refused.

Run: python3 tests/import-claude-context-progress.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "import-claude-context" / "scripts"
os.environ.setdefault("SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER", "1")

SLUG_A = "-Users-o-Projects-alpha"
SLUG_B = "-Users-o-Projects-beta"
U1 = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
U2 = "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
U3 = "33333333-cccc-4ccc-8ccc-cccccccccccc"
U4 = "44444444-dddd-4ddd-8ddd-dddddddddddd"   # aborted: extract.py skipped it as empty


def _load():
    spec = importlib.util.spec_from_file_location("ici_progress", SCRIPTS / "progress.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _j(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def make_data(data: Path) -> None:
    _j(data / "index.json", {
        "generated_at": "2026-09-10T00:00:00Z", "root": "/Users/o/.claude/projects",
        "counts": {"sessions": 4, "projects": 2, "empty": 1, "conversations": 3},
        "projects": {
            SLUG_A: {"slug": SLUG_A, "session_count": 3, "sessions": [
                {"uuid": U1, "title": "Widget build"}, {"uuid": U2, "title": "Widget polish"},
                {"uuid": U4, "title": "aborted session"}]},
            SLUG_B: {"slug": SLUG_B, "session_count": 1, "sessions": [{"uuid": U3, "title": "Beta launch"}]},
            "-Users-o-Projects-empty-dir": {"slug": "-Users-o-Projects-empty-dir", "session_count": 0,
                                            "sessions": []},
        }})
    _j(data / "state.json", {"sessions": {
        f"{SLUG_A}/{U1}": {"extracted_at": "2026-09-10T00:01:00Z", "chunks": 2},
        f"{SLUG_A}/{U4}": {"extracted_at": "2026-09-10T00:01:00Z", "chunks": 0, "skipped_empty": True},
    }, "projects": {}})


class TestProgress(unittest.TestCase):
    def setUp(self):
        self.m = _load()
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        make_data(self.data)

    def _status(self):
        return json.loads((self.data / "status.json").read_text())

    def test_phase_ladder_and_counts_from_disk(self):
        r = self.m.progress(self.data)
        self.assertEqual(r["phase"], "summarizing")
        self.assertEqual((r["sessions"], r["skipped_empty"], r["summarized"]), (3, 1, 0))
        self.assertEqual((r["projects"], r["rolled_up"], r["entities"], r["staged"]), (2, 0, False, False))
        st = self._status()
        self.assertEqual(st["phase"], "summarizing")
        self.assertEqual({k: v for k, v in st.items() if k not in ("phase", "updated_at")},
                         {k: v for k, v in r.items() if k != "phase"})

        _j(self.data / "summaries" / SLUG_A / f"{U1}.json", {"summary": "s"})
        _j(self.data / "summaries" / SLUG_A / f"{U2}.1.json", {"partial": True})   # not counted
        _j(self.data / "summaries" / SLUG_B / f"{U3}.json", {"summary": "s"})
        r = self.m.progress(self.data)
        self.assertEqual((r["phase"], r["summarized"]), ("summarizing", 2))

        _j(self.data / "summaries" / SLUG_A / f"{U2}.json", {"summary": "s"})
        r = self.m.progress(self.data)
        self.assertEqual((r["phase"], r["summarized"], r["sessions"]), ("rolling-up", 3, 3))
        self.assertEqual(self._status()["phase"], "rolling-up")

        _j(self.data / "projects" / f"{SLUG_A}.json", {"name": "Alpha"})
        _j(self.data / "entities.json", {"people": []})
        r = self.m.progress(self.data)
        self.assertEqual((r["phase"], r["rolled_up"], r["entities"]), ("rolling-up", 1, True))

        _j(self.data / "staged" / "manifest.json", {"projects": [SLUG_A]})
        r = self.m.progress(self.data)
        self.assertEqual((r["phase"], r["staged"]), ("staged", True))
        self.assertEqual(self._status()["phase"], "staged")

    def test_no_index_is_refused(self):
        (self.data / "index.json").unlink()
        with self.assertRaises(SystemExit):
            self.m.progress(self.data)
        self.assertFalse((self.data / "status.json").exists())

    def test_cli_counts_only(self):
        _j(self.data / "summaries" / SLUG_A / f"{U1}.json", {"summary": "s"})
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.m.main(["--data-dir", str(self.data), "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual((doc["phase"], doc["summarized"], doc["sessions"]), ("summarizing", 1, 3))
        for leak in ("Widget", "aborted", SLUG_A, U1, str(self.data)):
            self.assertNotIn(leak, buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.m.main(["--out-dir", str(self.data)])
        self.assertIn("summarizing: 1/3 sessions summarised (1 empty skipped), 0/2 projects rolled up", buf.getvalue())


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)
