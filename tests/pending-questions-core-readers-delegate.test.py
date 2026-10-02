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


def _g(items, unavailable=False, reason=None, done=0, link=None, notes=()):
    return {"waiting": items, "done": None if unavailable else done, "unavailable": unavailable, "reason": reason,
            "link": link, "notes": list(notes), "store": None}


OUTAGE = _g([_item("held", "Held one?", in_room=False)], unavailable=True, reason="room down")


class Dashboard(unittest.TestCase):
    def test_count_is_the_readers_count(self):
        dash = _load("dash_t", SRC / "dashboard.py")
        c_ok = {"open": 3, "done": 7, "unavailable": False, "reason": None}
        with mock.patch.object(dash.pending_questions_reader, "count", return_value=c_ok) as c:
            self.assertEqual(dash.get_pending_count(), c_ok)
        c.assert_called_once_with(dash.WORKSPACE_DIR, dash.skill_roots.declared(dash.pending_questions_reader.DECLARATION, dash.WORKSPACE_DIR))
        self.assertEqual(_no_file_read(SRC / "dashboard.py"), [])

    def test_an_unreachable_room_renders_a_question_mark_with_the_reason_never_zero(self):
        dash = _load("dash_t2", SRC / "dashboard.py")
        unknown = {"open": None, "done": None, "unavailable": True, "reason": "room down"}
        self.assertEqual(dash.pending_tile(unknown), ("?", "unknown — room unreachable (room down)"))
        self.assertEqual(dash.pending_tile({"open": 0, "done": 1, "unavailable": False, "reason": None}), ("0", ""))
        html_src = (SRC / "dashboard.py").read_text()
        self.assertIn("pending_tile(pending)[0]", html_src, "the stat renders through the tile helper")
        self.assertNotIn("{pending['open']}", html_src)


class Briefing(unittest.TestCase):
    def test_the_count_and_link_come_from_the_reader_and_notes_are_not_silent(self):
        mb = _load("mb_t", SRC / "morning-briefing.py")
        g = _g([_item("a", "x" * 100), _item("b", "second?")], link="https://r/#/db",
               notes=["room database: not used (test)"])
        err = io.StringIO()
        with mock.patch.object(mb.pending_questions_reader, "gather", return_value=g), redirect_stderr(err):
            got = mb.get_pending_questions()
        self.assertEqual(got, {"count": 2, "link": "https://r/#/db", "unavailable": False, "reason": None})
        self.assertIn("room database: not used (test)", err.getvalue())
        self.assertEqual(_no_file_read(SRC / "morning-briefing.py"), [])

    def test_an_unreachable_room_is_unknown_in_the_count_and_the_narrative(self):
        mb = _load("mb_t2", SRC / "morning-briefing.py")
        with mock.patch.object(mb.pending_questions_reader, "gather", return_value=OUTAGE), redirect_stderr(io.StringIO()):
            got = mb.get_pending_questions()
        self.assertEqual((got["count"], got["unavailable"], got["reason"]), (None, True, "room down"))
        line = mb.synthesize(None, [], [], [], got, [])
        self.assertIn("Pending questions: unknown — the room was unreachable (room down).", line)
        self.assertNotIn("Everything looks clean", line)


class Friction(unittest.TestCase):
    def test_stale_is_judged_on_the_readers_asked_at(self):
        fd = _load("fd_t", SRC / "friction-detector.py")
        now = time.time()
        items = [_item("old", "Old one", now - 3 * 86400), _item("new", "Fresh one", now - 3600),
                 _item("undated", "No date")]
        with mock.patch.object(fd.pending_questions_reader, "gather", return_value=_g(items)):
            out = fd.check_pending_questions()
        self.assertEqual(out, ["Pending question unanswered (3d old): Old one",
                               "Pending question unanswered: No date"])
        self.assertEqual(_no_file_read(SRC / "friction-detector.py"), [])

    def test_an_unreachable_room_is_one_unknown_issue_not_a_clean_pass(self):
        fd = _load("fd_t2", SRC / "friction-detector.py")
        with mock.patch.object(fd.pending_questions_reader, "gather", return_value=OUTAGE):
            out = fd.check_pending_questions()
        self.assertEqual(out, ["Pending questions: UNKNOWN — room unreachable (room down); 1 held locally"])


class Obsidian(unittest.TestCase):
    def test_asks_are_rendered_from_the_reader_and_written_once(self):
        om = _load("om_t", SRC / "obsidian-mirror.py")
        vault, ws = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        items = [_item("ask-1", "Merge #12?", snippet="CI is green", in_room=False)]
        with mock.patch.object(om.pending_questions_reader, "gather", return_value=_g(items)):
            self.assertTrue(om._mirror_asks(vault, ws))
            text = (vault / "Sutando" / "Agent" / "Asks.md").read_text()
            self.assertIn("## Merge #12? (not yet in the room)", text)
            self.assertIn("CI is green", text)
            self.assertIn("Ask id: ask-1", text)
            self.assertFalse(om._mirror_asks(vault, ws))
        with mock.patch.object(om.pending_questions_reader, "gather", return_value=_g([])):
            self.assertFalse(om._mirror_asks(Path(tempfile.mkdtemp()), ws))
        self.assertEqual(_no_file_read(SRC / "obsidian-mirror.py"), [])

    def test_an_unreachable_room_leaves_the_mirror_untouched(self):
        om = _load("om_t2", SRC / "obsidian-mirror.py")
        vault, ws = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        dest = vault / "Sutando" / "Agent" / "Asks.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("# Pending questions\n\n## the full list from before\n")
        with mock.patch.object(om.pending_questions_reader, "gather", return_value=OUTAGE), redirect_stderr(io.StringIO()):
            self.assertFalse(om._mirror_asks(vault, ws))
        self.assertIn("the full list from before", dest.read_text(), "a partial read never overwrites the mirror")


class SessionHandoff(unittest.TestCase):
    def test_the_section_runs_the_reader_cli_and_names_a_failure(self):
        src = (SRC / "session-handoff.sh").read_text()
        self.assertIn('pending_questions_reader.py" list --workspace "$WORKSPACE_DIR"', src)
        self.assertIn("pending-questions reader failed", src)
        self.assertEqual(_no_file_read(SRC / "session-handoff.sh"), [])

    def _cli(self, ws, *args, env=None):
        import os
        e = {**os.environ, **(env or {})}
        return subprocess.run([sys.executable, str(SRC / "pending_questions_reader.py"), *args, "--workspace", str(ws)],
                              capture_output=True, text=True, env=e)

    def test_the_reader_cli_lists_a_held_question_from_an_empty_workspace(self):
        ws = Path(tempfile.mkdtemp())
        sys.path.insert(0, str(REPO / "skills" / "pending-questions" / "scripts"))
        import pending_questions_outbox as pqo
        q = {"ask_id": "ask-held", "question": "Held one?", "context": None, "asked_at": 0, "default_action": None,
             "reason": None, "options": [], "priority": "Medium"}
        pqo.Outbox(ws).save(q, "**Sent:** queued x via proactive-ask-held.txt at 2026-09-01T00:00:00Z")
        r = self._cli(ws, "list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("- [ask-held] Held one? (not yet in the room)", r.stdout)
        r = self._cli(ws, "list", "--json")
        self.assertEqual([i["ask_id"] for i in json.loads(r.stdout)], ["ask-held"])
        self.assertEqual(json.loads(self._cli(ws, "count").stdout),
                         {"open": 1, "done": 0, "pending_close": 0, "unavailable": False, "reason": None})

    def test_the_reader_cli_says_unknown_for_an_unreachable_room(self):
        """The handoff echoes this stdout: it must carry the word UNKNOWN, not a zero."""
        ws = Path(tempfile.mkdtemp())
        (ws / "state").mkdir()
        (ws / "state" / "pending-questions-store-history").write_text("ask-x\n")  # a row was confirmed before
        r = self._cli(ws, "list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pending questions: UNKNOWN — room unreachable", r.stdout)
        self.assertNotIn("0 pending questions", r.stdout)
        c = json.loads(self._cli(ws, "count").stdout)
        self.assertEqual((c["open"], c["done"], c["unavailable"]), (None, None, True))
        j = json.loads(self._cli(ws, "list", "--json").stdout)
        self.assertEqual((j["unavailable"], j["waiting"]), (True, []))


if __name__ == "__main__":
    unittest.main()
