#!/usr/bin/env python3
"""Coverage + behavior tests for the session-recap extract.py CLI.

Exercises the whole module — text_of / session_meta / transcripts_dir / pick /
main (list + dump across every --filter, max-chars truncation, and the error
exits) — so the recap extractor is guarded end-to-end, not just the pick()
sidechain fix.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
EXTRACT = REPO / "skills" / "session-recap" / "scripts" / "extract.py"


def _load():
    spec = importlib.util.spec_from_file_location("recap_extract", EXTRACT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, lines: list, mtime: float | None = None) -> None:
    path.write_text("\n".join(json.dumps(x) if not isinstance(x, str) else x
                              for x in lines) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class TestTextOf(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def test_variants(self):
        self.assertEqual(self.m.text_of("hi"), "hi")
        self.assertEqual(
            self.m.text_of([{"type": "text", "text": "a"},
                            {"type": "tool_use", "name": "x"},
                            {"type": "text", "text": "b"}]),
            "a b")
        self.assertEqual(self.m.text_of(None), "")
        self.assertEqual(self.m.text_of(42), "")


class TestSessionMeta(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def test_counts_and_first_user(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            _write(p, [
                "not json",  # malformed → skipped
                {"type": "custom-title", "title": "x"},
                {"type": "user", "timestamp": "2026-07-15T10:00:00Z",
                 "message": {"content": "  hello  world  "}},
                {"type": "assistant", "timestamp": "2026-07-15T10:01:00Z",
                 "message": {"content": [{"type": "text", "text": "hi"}]}},
                {"type": "user", "timestamp": "2026-07-15T10:02:00Z",
                 "message": {"content": ""}},  # empty → first_user unchanged
            ])
            meta = self.m.session_meta(p)
        self.assertEqual(meta["user_msgs"], 2)
        self.assertEqual(meta["assistant_msgs"], 1)
        self.assertEqual(meta["first_user"], "hello world")
        self.assertEqual(meta["start"], "2026-07-15T10:00:00Z")
        self.assertEqual(meta["end"], "2026-07-15T10:02:00Z")
        self.assertEqual(meta["file"], "s.jsonl")


class TestTranscriptsDir(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def test_resolves_from_workspace_and_slug(self):
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="/tmp/ws\n")
        with patch.object(self.m.subprocess, "run", return_value=fake):
            got = self.m.transcripts_dir()
        self.assertTrue(str(got).startswith("/tmp/ws/.claude-sutando/projects/"))
        self.assertIn(self.m.claude_project_slug(str(self.m.REPO)), str(got))

    def test_slug_dashes_all_non_alphanumerics(self):
        # Claude Code's project slug dashes spaces and dots too, not just "/".
        # A repo path like the desktop-bundled engine checkout must resolve to
        # the dir Claude Code actually writes.
        fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="/tmp/ws\n")
        bundled = Path("/Users/u/Library/Application Support/space.ag2.app/engine/sutando")
        with patch.object(self.m, "REPO", bundled), \
                patch.object(self.m.subprocess, "run", return_value=fake):
            got = self.m.transcripts_dir()
        self.assertEqual(
            got.name,
            "-Users-u-Library-Application-Support-space-ag2-app-engine-sutando")


class TestMainDumpAndList(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def _run(self, argv, tdir):
        with patch.object(self.m, "transcripts_dir", return_value=tdir):
            with patch.object(self.m.sys, "argv", ["extract.py"] + argv):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.m.main()
                return buf.getvalue()

    def _fixture(self, d: Path):
        prev = d / "prev.jsonl"
        cur = d / "cur.jsonl"
        _write(prev, [
            {"type": "user", "timestamp": "2026-07-15T09:00:00Z",
             "message": {"content": "prev question"}},
        ], mtime=1000)
        _write(cur, [
            "bad json line",
            {"type": "user", "timestamp": "2026-07-15T10:00:00Z",
             "message": {"content": "hello"}},
            {"type": "assistant", "timestamp": "2026-07-15T10:01:00Z",
             "message": {"content": [{"type": "text", "text": "world"}]}},
            {"type": "assistant", "timestamp": "2026-07-15T10:02:00Z",
             "message": {"content": [
                 {"type": "tool_use", "name": "Write",
                  "input": {"file_path": "/x/note.md"}},
                 {"type": "tool_use", "name": "Bash", "input": {}},
             ]}},
            {"type": "system", "timestamp": "2026-07-15T10:03:00Z",
             "content": "a system line"},
        ], mtime=2000)
        return prev, cur

    def test_list_mode(self):
        with tempfile.TemporaryDirectory() as td:
            self._fixture(Path(td))
            out = self._run(["list"], Path(td))
        rows = [json.loads(x) for x in out.splitlines() if x.strip()]
        self.assertEqual({r["file"] for r in rows}, {"prev.jsonl", "cur.jsonl"})

    def test_dump_current_dialog(self):
        with tempfile.TemporaryDirectory() as td:
            self._fixture(Path(td))
            out = self._run(["dump", "--session", "current", "--filter", "dialog"], Path(td))
        self.assertIn("USER: hello", out)
        self.assertIn("ASSISTANT: world", out)
        self.assertNotIn("TOOLS", out)  # dialog filter drops tool-only turns

    def test_dump_all_surfaces_tools_and_system(self):
        with tempfile.TemporaryDirectory() as td:
            self._fixture(Path(td))
            out = self._run(["dump", "--session", "current", "--filter", "all"], Path(td))
        self.assertIn("TOOLS: Write(/x/note.md), Bash", out)
        self.assertIn("SYSTEM: a system line", out)

    def test_dump_user_filter_only(self):
        with tempfile.TemporaryDirectory() as td:
            self._fixture(Path(td))
            out = self._run(["dump", "--session", "current", "--filter", "user"], Path(td))
        self.assertIn("USER: hello", out)
        self.assertNotIn("ASSISTANT", out)

    def test_dump_last_resolves_previous(self):
        with tempfile.TemporaryDirectory() as td:
            self._fixture(Path(td))
            out = self._run(["dump", "--session", "last"], Path(td))
        self.assertIn("prev question", out)

    def test_max_chars_truncation(self):
        with tempfile.TemporaryDirectory() as td:
            self._fixture(Path(td))
            out = self._run(["dump", "--session", "current", "--max-chars", "5"], Path(td))
        self.assertIn("truncated", out)

    def test_no_transcripts_exits(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                self._run(["list"], Path(td))

    def test_pick_uuid_and_nomatch(self):
        with tempfile.TemporaryDirectory() as td:
            prev, cur = self._fixture(Path(td))
            sessions = sorted(Path(td).glob("*.jsonl"),
                              key=lambda p: p.stat().st_mtime, reverse=True)
            self.assertEqual(self.m.pick(sessions, "prev").name, "prev.jsonl")
            with self.assertRaises(SystemExit):
                self.m.pick(sessions, "nope-uuid")

    def test_is_sidechain_oserror_and_empty(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "gone.jsonl"
            self.assertFalse(self.m.is_sidechain_transcript(missing))  # OSError path
            empty = Path(td) / "meta-only.jsonl"
            _write(empty, [{"type": "custom-title", "title": "x"}])
            self.assertFalse(self.m.is_sidechain_transcript(empty))  # no message events


if __name__ == "__main__":
    unittest.main()
