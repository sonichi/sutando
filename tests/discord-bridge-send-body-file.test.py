#!/usr/bin/env python3
"""`send --body-file` keeps a message body off the shell, without reading host config.
Run: python3 tests/discord-bridge-send-body-file.test.py"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import contextlib
import io
import json
import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

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

    def test_addressed_reviewer_body_reaches_discord_and_honors_post_gate(self):
        from channels.discord.client import DiscordRestClient
        body = "<@123456789> Re-review https://github.com/example/project/pull/1: routing fixed."
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "review.txt"
            path.write_text(body + "\n", encoding="utf-8")
            for refusal in (None, "posting refused"):
                with self.subTest(refusal=refusal):
                    posted, validated = [], []

                    def transport(req, timeout):
                        posted.append((req.full_url, json.loads(req.data)))
                        return 200, {"id": "receipt-123"}

                    def validator(channel, payload):
                        validated.append((channel, payload))
                        return refusal

                    client = DiscordRestClient("test-token", transport=transport,
                                               validator=validator)
                    output = io.StringIO()
                    with patch.object(db, "_rest_client", return_value=client), contextlib.redirect_stdout(output):
                        reply_to, argv = db._parse_send_argv(["--body-file", str(path)])
                        if refusal:
                            with self.assertRaises(SystemExit) as caught:
                                db._send_via_rest("987654321", db._send_cli_body(argv), reply_to)
                            self.assertEqual(caught.exception.code, 1)
                        else:
                            db._send_via_rest("987654321", db._send_cli_body(argv), reply_to)
                    self.assertEqual(validated, [("987654321", {"content": body})])
                    if refusal:
                        self.assertEqual(posted, [])
                        self.assertNotIn("message_id receipt-123", output.getvalue())
                    else:
                        self.assertEqual(len(posted), 1)
                        self.assertTrue(posted[0][0].endswith("/channels/987654321/messages"))
                        self.assertEqual(posted[0][1]["content"], body)
                        self.assertIn("message_id receipt-123", output.getvalue())

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
