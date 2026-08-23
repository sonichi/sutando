#!/usr/bin/env python3
"""`send --body-file` keeps a message body off the shell, without reading host config.
Run: python3 tests/discord-bridge-send-body-file.test.py"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent

# ISOLATE BEFORE THE IMPORT. discord-bridge resolves channel config at module
# scope, so a later assignment is useless — it would read the operator's home.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-send-body-file-")
os.environ.pop("CLAUDE_HOME", None)
os.environ["SUTANDO_TEST_MODE"] = "1"
_cfg = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}', encoding="utf-8")
(_cfg / ".env").write_text("DISCORD_BOT_TOKEN=test-token\n", encoding="utf-8")

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
        self.assertEqual(db._send_cli_body(["hello", "world"]), "hello world")

    def test_the_flag_LATER_in_a_body_stays_prose(self):
        # The compatibility guarantee: an existing message mentioning the flag
        # must still send as text, not read a file named by the next word.
        argv = ["please", "document", "--body-file", "usage"]
        self.assertEqual(db._send_cli_body(argv), "please document --body-file usage")

    def test_a_fifo_is_refused_rather_than_blocking_forever(self):
        with tempfile.TemporaryDirectory() as td:
            fifo = pathlib.Path(td) / "pipe"
            os.mkfifo(fifo)
            with self.assertRaises(SystemExit):
                db._send_cli_body(["--body-file", str(fifo)])

    def test_an_oversize_file_is_refused_before_being_read(self):
        with tempfile.TemporaryDirectory() as td:
            big = pathlib.Path(td) / "big.txt"
            big.write_bytes(b"x" * (db.MAX_BODY_BYTES + 1))
            with self.assertRaises(SystemExit):
                db._send_cli_body(["--body-file", str(big)])

    def test_trailing_argument_after_the_path_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "b.txt"
            p.write_text("body", encoding="utf-8")
            with self.assertRaises(SystemExit):
                db._send_cli_body(["--body-file", str(p), "extra"])

    def test_empty_file_is_refused_rather_than_sent_blank(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "blank.txt"
            p.write_text("   \n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                db._send_cli_body(["--body-file", str(p)])

    def test_unreadable_path_fails_loudly(self):
        with self.assertRaises(SystemExit):
            db._send_cli_body(["--body-file", "/nonexistent/nope.txt"])

    def test_flag_without_a_path_is_refused(self):
        with self.assertRaises(SystemExit):
            db._send_cli_body(["--body-file"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
