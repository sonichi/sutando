#!/usr/bin/env python3
"""Every core reader of owner pending questions — dashboard, morning-briefing,
friction-detector, obsidian-mirror, session-handoff.sh — renders what
src/pending_questions_reader.py returns and opens no file of its own.

Run: python3 tests/pending-questions-core-readers-delegate.test.py
"""
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _item(ask_id, title, asked_at=None, snippet="", in_room=True):
    return {"id": title[:40], "ask_id": ask_id, "title": title, "snippet": snippet, "body": snippet,
            "asked_at": asked_at, "priority": "medium", "in_room": in_room}


FILE_NEEDLES = ('pending-questions.md"', "pending-questions.md'", "pending_questions_md",
                "pending_questions_ledger", "get_waiting_questions", "PQ_FILE")


def _no_file_read(path: Path, allow=()):
    src = path.read_text()
    return [n for n in FILE_NEEDLES if n in src and n not in allow]


class Dashboard(unittest.TestCase):
    def test_count_is_the_readers_count(self):
        dash = _load("dash_t", SRC / "dashboard.py")
        with mock.patch.object(dash.pending_questions_reader, "count", return_value={"open": 3, "done": 7}) as c:
            self.assertEqual(dash.get_pending_count(), {"open": 3, "done": 7})
        c.assert_called_once_with(dash.WORKSPACE_DIR)
        self.assertEqual(_no_file_read(SRC / "dashboard.py"), [])


class Briefing(unittest.TestCase):
    def test_titles_are_the_readers_items_clipped_and_notes_are_not_silent(self):
        mb = _load("mb_t", SRC / "morning-briefing.py")
        g = {"waiting": [_item("a", "[2026-08-01] " + "x" * 100), _item("b", "second?")],
             "done": 0, "notes": ["room database: not used (test)"], "store": None}
        err = io.StringIO()
        with mock.patch.object(mb.pending_questions_reader, "gather", return_value=g), redirect_stderr(err):
            got = mb.get_pending_questions()
        self.assertEqual(len(got), 2)
        self.assertEqual(len(got[0]), 60)
        self.assertNotIn("[2026-08-01]", got[0])
        self.assertEqual(got[1], "second?")
        self.assertIn("room database: not used (test)", err.getvalue())
        self.assertEqual(_no_file_read(SRC / "morning-briefing.py"), [])

    def test_the_spoken_line_makes_no_ranking_claim(self):
        mb = _load("mb_t2", SRC / "morning-briefing.py")
        line = mb.synthesize(None, [], [], [], ["first filed", "urgent but later"], None)
        self.assertIn("2 pending questions", line)
        self.assertNotIn("Top item", line)
        self.assertIn("One pending question waiting: only one",
                      mb.synthesize(None, [], [], [], ["only one"], None))


class Friction(unittest.TestCase):
    def test_stale_is_judged_on_the_readers_asked_at(self):
        fd = _load("fd_t", SRC / "friction-detector.py")
        now = time.time()
        items = [_item("old", "Old one", now - 3 * 86400), _item("new", "Fresh one", now - 3600),
                 _item("undated", "No date")]
        with mock.patch.object(fd.pending_questions_reader, "waiting", return_value=items):
            out = fd.check_pending_questions()
        self.assertEqual(out, ["Pending question unanswered (3d old): Old one",
                               "Pending question unanswered: No date"])
        self.assertEqual(_no_file_read(SRC / "friction-detector.py"), [])


class Obsidian(unittest.TestCase):
    def test_asks_are_rendered_from_the_reader_and_written_once(self):
        om = _load("om_t", SRC / "obsidian-mirror.py")
        vault, ws = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        items = [_item("ask-1", "Merge #12?", snippet="CI is green", in_room=False)]
        with mock.patch.object(om.pending_questions_reader, "waiting", return_value=items):
            self.assertTrue(om._mirror_asks(vault, ws))
            text = (vault / "Sutando" / "Agent" / "Asks.md").read_text()
            self.assertIn("## Merge #12? (not yet in the room)", text)
            self.assertIn("CI is green", text)
            self.assertIn("Ask id: ask-1", text)
            self.assertFalse(om._mirror_asks(vault, ws))
        with mock.patch.object(om.pending_questions_reader, "waiting", return_value=[]):
            self.assertFalse(om._mirror_asks(Path(tempfile.mkdtemp()), ws))
        self.assertEqual(_no_file_read(SRC / "obsidian-mirror.py"), [])


class SessionHandoff(unittest.TestCase):
    def test_the_section_runs_the_reader_cli_and_names_a_failure(self):
        src = (SRC / "session-handoff.sh").read_text()
        self.assertIn('pending_questions_reader.py" list --workspace "$WORKSPACE_DIR"', src)
        self.assertIn("pending-questions reader failed", src)
        self.assertEqual(_no_file_read(SRC / "session-handoff.sh"), [])

    def test_the_reader_cli_lists_a_held_question_from_an_empty_workspace(self):
        ws = Path(tempfile.mkdtemp())
        import pending_questions_store as pqs
        pqs.Outbox(ws).save(pqs.Question("ask-held", "Held one?"), "**Sent:** queued x via proactive-ask-held.txt at 2026-09-01T00:00:00Z")
        r = subprocess.run([sys.executable, str(SRC / "pending_questions_reader.py"), "list", "--workspace", str(ws)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("- [ask-held] Held one? (not yet in the room)", r.stdout)
        r = subprocess.run([sys.executable, str(SRC / "pending_questions_reader.py"), "list", "--json",
                            "--workspace", str(ws)], capture_output=True, text=True)
        self.assertEqual([i["ask_id"] for i in json.loads(r.stdout)], ["ask-held"])


if __name__ == "__main__":
    unittest.main()
