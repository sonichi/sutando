#!/usr/bin/env python3
"""src/codex_core_model.py: the Codex core's own model record, its name rule, Codex's model list and
the home it is read from. Core-only: nothing here writes the shared Codex home."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import codex_core_model as ccm  # noqa: E402


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = ccm.main(list(argv))
        return rc, out.getvalue().strip()


class Names(Case):
    def test_codex_slugs_pass_and_claude_names_flags_and_shell_text_do_not(self):
        for good in ("gpt-6-luna", "gpt-5.6-terra", "o4_mini"):
            self.assertTrue(ccm.valid(good), good)
        for bad in ("", None, "opus", "Sonnet[1m]", "default", "claude-opus-5", "-m", "gpt; rm", "a" * 81):
            self.assertFalse(ccm.valid(bad), bad)


class Record(Case):
    def test_save_keeps_the_previous_codex_pick_and_refuses_bad_input(self):
        first = ccm.save(self.state, "gpt-a", applied="next-start", accepted=False, now=0)
        self.assertEqual((first["previous"], first["previous_source"], first["ts"]), (None, "none", "1970-01-01T00:00:00Z"))
        second = ccm.save(self.state, "gpt-b", applied="next-turn", accepted=True)
        self.assertEqual((second["previous"], second["previous_source"], second["runtime"]), ("gpt-a", "last-record", "codex"))
        self.assertEqual(ccm.current(self.state), "gpt-b")
        with self.assertRaises(ValueError):
            ccm.save(self.state, "opus", applied="next-turn", accepted=True)
        with self.assertRaises(ValueError):
            ccm.save(self.state, "gpt-c", applied="now", accepted=True)
        self.assertEqual(ccm.current(self.state), "gpt-b", "a refused save writes nothing")

    def test_a_missing_unreadable_or_claude_record_names_no_model(self):
        self.assertEqual(ccm.current(self.state), "")
        self.state.mkdir()
        for text in ("not json", "[1]", json.dumps({"model": "opus"}), json.dumps({"model": "-x", "runtime": "codex"})):
            ccm.record_path(self.state).write_text(text)
            self.assertEqual(ccm.current(self.state), "", text)

    def test_newer_than_reads_only_a_record_written_after_the_moment(self):
        self.assertEqual(ccm.newer_than(self.state, 0), "", "no record")
        ccm.save(self.state, "gpt-a", applied="next-turn", accepted=True)
        mtime = ccm.record_path(self.state).stat().st_mtime
        self.assertEqual(ccm.newer_than(self.state, mtime - 1), "gpt-a")
        self.assertEqual(ccm.newer_than(self.state, mtime + 1), "")


class Choices(Case):
    def write(self, data):
        (self.root / "models_cache.json").write_text(data if isinstance(data, str) else json.dumps(data))

    def test_visible_models_by_priority_once_each(self):
        self.write({"models": [{"slug": "gpt-b", "display_name": "GPT  B", "priority": 2},
                               {"slug": "gpt-a", "visibility": "list", "priority": 1},
                               {"slug": "gpt-h", "visibility": "hide", "priority": 0},
                               {"slug": "gpt-b", "display_name": "dup", "priority": 3},
                               {"slug": "opus"}, "not a dict", {"slug": "gpt-n", "priority": "x"}]})
        self.assertEqual(ccm.choices(self.root), [("gpt-a", "gpt-a"), ("gpt-b", "GPT B"), ("gpt-n", "gpt-n")])

    def test_no_cache_or_a_broken_one_is_no_list(self):
        self.assertEqual(ccm.choices(self.root), [])
        for data in ("{", json.dumps([1]), json.dumps({"models": "x"})):
            self.write(data)
            self.assertEqual(ccm.choices(self.root), [], data)


class Home(Case):
    def test_the_configured_home_then_codex_home_then_the_default(self):
        import codex_home
        with mock.patch.object(codex_home, "configured", return_value=("CODEX_HOME", str(self.root / "cfg"))):
            self.assertEqual(ccm.home(REPO), self.root / "cfg")
        with mock.patch.object(codex_home, "configured", side_effect=RuntimeError("unreadable")), \
                mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "env")}):
            self.assertEqual(ccm.home(REPO), self.root / "env")
        with mock.patch.object(codex_home, "configured", return_value=("", "")), \
                mock.patch.dict(os.environ, {}, clear=True), mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertEqual(ccm.home(REPO), self.root / ".codex")


class Cli(Case):
    def test_check_current_save_and_choices(self):
        self.assertEqual(self.main("check", "gpt-a"), (0, ""))
        self.assertEqual(self.main("check", "opus"), (2, ""))
        self.assertEqual(self.main("current", "--state-dir", str(self.state)), (0, ""))
        self.assertEqual(self.main("save", "--state-dir", str(self.state), "--model", "gpt-a", "--applied", "next-start"),
                         (0, "OK -"))
        self.assertEqual(self.main("save", "--state-dir", str(self.state), "--model", "gpt-b", "--applied", "next-turn",
                                   "--accepted"), (0, "OK gpt-a"))
        self.assertTrue(json.loads(ccm.record_path(self.state).read_text())["accepted"])
        self.assertEqual(self.main("current", "--state-dir", str(self.state)), (0, "gpt-b"))
        rc, out = self.main("save", "--state-dir", str(self.state), "--model", "sonnet", "--applied", "next-turn")
        self.assertEqual(rc, 2)
        self.assertTrue(out.startswith("FAILED"))
        (self.root / "models_cache.json").write_text(json.dumps({"models": [{"slug": "gpt-a", "display_name": "A"}]}))
        self.assertEqual(self.main("choices", "--codex-home", str(self.root)), (0, "gpt-a=A"))
        with mock.patch.object(ccm, "home", return_value=self.root):
            self.assertEqual(self.main("choices"), (0, "gpt-a=A"))

    def test_an_unwritable_state_folder_is_exit_1(self):
        blocker = self.root / "file"
        blocker.write_text("")
        rc, out = self.main("save", "--state-dir", str(blocker), "--model", "gpt-a", "--applied", "next-turn")
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith("FAILED"))


if __name__ == "__main__":
    unittest.main()
