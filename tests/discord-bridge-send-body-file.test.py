#!/usr/bin/env python3
"""`send --body-file` keeps a message body off the shell.
Run: python3 tests/discord-bridge-send-body-file.test.py"""
from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("db", REPO / "src" / "discord-bridge.py")
db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db)

# The exact shape that truncates: a backtick pair followed by an apostrophe.
HAZARD = "He approved `qingyun-wu`'s #2909 — an apostrophe closes the quote."


class SendBodyFile(unittest.TestCase):
    def test_body_is_delivered_verbatim(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "b.txt"
            p.write_text(HAZARD + "\n", encoding="utf-8")
            self.assertEqual(db._send_cli_body(["--body-file", str(p)]), HAZARD)

    def test_default_path_is_unchanged(self):
        # Every existing `send <channel> some words` invocation must behave the same.
        self.assertEqual(db._send_cli_body(["hello", "world"]), "hello world")

    def test_positional_text_alongside_the_flag_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "b.txt"
            p.write_text("body", encoding="utf-8")
            with self.assertRaises(SystemExit):
                db._send_cli_body(["also this", "--body-file", str(p)])

    def test_empty_file_is_refused_rather_than_sent_blank(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "blank.txt"
            p.write_text("   \n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                db._send_cli_body(["--body-file", str(p)])

    def test_unreadable_path_fails_loudly(self):
        # A missing file must not degrade into an empty body.
        with self.assertRaises(SystemExit):
            db._send_cli_body(["--body-file", "/nonexistent/nope.txt"])

    def test_flag_without_a_path_is_refused(self):
        with self.assertRaises(SystemExit):
            db._send_cli_body(["--body-file"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
