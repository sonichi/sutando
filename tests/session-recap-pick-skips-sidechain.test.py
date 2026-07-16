#!/usr/bin/env python3
"""Regression guard: extract.py pick('last'/'current') skips subagent transcripts.

Claude Code writes subagent (Task/Agent) conversations as their own *.jsonl in
the same project dir, with message events marked ``isSidechain: true``. They can
sort newer (by mtime) than the previous MAIN session, so a naive
`sessions[1]` for --session last returned the subagent transcript instead of the
real previous session — the boot-recap then summarized a subagent run, not the
prior session. After the fix, pick() resolves current/last against main
transcripts only.
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXTRACT = REPO / "skills" / "session-recap" / "scripts" / "extract.py"


def _load():
    spec = importlib.util.spec_from_file_location("recap_extract", EXTRACT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, sidechain: bool, mtime: float) -> None:
    # A meta line (no isSidechain) then one message event carrying the flag —
    # mirrors real transcripts where the first line is custom-title/last-prompt.
    lines = [
        '{"type": "custom-title", "title": "x"}',
        '{"type": "user", "isSidechain": %s, "message": {"content": "hi"}}'
        % ("true" if sidechain else "false"),
    ]
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (mtime, mtime))


class TestPickSkipsSidechain(unittest.TestCase):
    def setUp(self):
        if not EXTRACT.exists():
            self.skipTest("extract.py not found")
        self.mb = _load()

    def _sorted(self, d: Path):
        return sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)

    def test_last_skips_a_newer_subagent_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            prev = d / "prev-main.jsonl"
            sub = d / "subagent.jsonl"
            cur = d / "current-main.jsonl"
            # mtime order (Codex scenario): current newest, subagent second, prev oldest
            _write(prev, sidechain=False, mtime=1000)
            _write(sub, sidechain=True, mtime=2000)
            _write(cur, sidechain=False, mtime=3000)
            sessions = self._sorted(d)
            # naive sessions[1] would be the subagent — the bug we fixed
            self.assertEqual(sessions[1].name, "subagent.jsonl")
            # current resolves to the active main; last skips the subagent
            self.assertEqual(self.mb.pick(sessions, "current").name, "current-main.jsonl")
            self.assertEqual(self.mb.pick(sessions, "last").name, "prev-main.jsonl")

    def test_only_one_main_errors_on_last(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write(d / "only-main.jsonl", sidechain=False, mtime=3000)
            _write(d / "subagent.jsonl", sidechain=True, mtime=2000)
            sessions = self._sorted(d)
            with self.assertRaises(SystemExit):
                self.mb.pick(sessions, "last")

    def test_is_sidechain_classifier(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            main = d / "m.jsonl"
            sub = d / "s.jsonl"
            _write(main, sidechain=False, mtime=1000)
            _write(sub, sidechain=True, mtime=1000)
            self.assertFalse(self.mb.is_sidechain_transcript(main))
            self.assertTrue(self.mb.is_sidechain_transcript(sub))


if __name__ == "__main__":
    unittest.main()
