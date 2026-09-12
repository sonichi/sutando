#!/usr/bin/env python3
"""`--jsonl` carries each message's id and jump link; the text mode stays byte-for-byte as it was.
The owner's triage card asked for links on message rows (2026-09-12); the text lines carry no id.
"""
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "discord-read.py"

_spec = importlib.util.spec_from_file_location("discord_read_jsonl", SCRIPT)
dr = importlib.util.module_from_spec(_spec)
sys.modules["discord_read_jsonl"] = dr
try:
    _spec.loader.exec_module(dr)
except SystemExit:
    pass

MESSAGES = [
    {"id": "2000", "timestamp": "2026-09-12T18:00:01.000Z", "content": "second",
     "author": {"username": "Sutando-Pro"}},
    {"id": "1000", "timestamp": "2026-09-12T17:59:59.000Z", "content": "first é",
     "author": {"username": "susanliu_"}},
]


LONG_PARENT = "x" * 400   # > REPLY_CLIP (110): the rendered `reply` is truncated

# Its own fixture on purpose: adding a reply to MESSAGES would change the text
# mode output the guard above pins, which is exactly what that test is for.
REPLY_MESSAGES = [
    {"id": "2000", "timestamp": "2026-09-12T18:00:01.000Z", "content": "second",
     "author": {"username": "Sutando-Pro"},
     "referenced_message": {"id": "1000", "timestamp": "2026-09-12T17:59:59.000Z",
                            "content": LONG_PARENT, "author": {"username": "susanliu_"}}},
    {"id": "1000", "timestamp": "2026-09-12T17:59:59.000Z", "content": LONG_PARENT,
     "author": {"username": "susanliu_"}},
]


def _run(argv, guild="9001", messages=None):
    out = io.StringIO()
    with mock.patch.object(dr, "_load_token", return_value="tok"), \
         mock.patch.object(dr, "_fetch", return_value=list(MESSAGES if messages is None else messages)), \
         mock.patch.object(dr.discord_context_policy, "resolve_guild", return_value=guild) as rg, \
         contextlib.redirect_stdout(out):
        rc = dr.main(argv)
    return rc, out.getvalue(), rg


class JsonlMode(unittest.TestCase):
    def test_one_object_per_message_oldest_first_with_id_and_jump_link(self):
        rc, out, rg = _run(["123", "--operator", "--jsonl"])
        self.assertEqual(rc, 0)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["id"] for r in rows], ["1000", "2000"])
        self.assertEqual(rows[0]["author"], "susanliu_")
        self.assertEqual(rows[0]["text"], "first é")
        self.assertEqual(rows[0]["ts"], "2026-09-12T17:59:59")
        self.assertEqual(rows[0]["reply"], "")
        self.assertEqual(rows[0]["url"], "https://discord.com/channels/9001/123/1000")
        rg.assert_called_once_with("123", "tok")

    def test_a_dm_channel_links_through_at_me(self):
        _, out, _ = _run(["123", "--operator", "--jsonl"], guild=None)
        self.assertTrue(all(json.loads(l)["url"].startswith("https://discord.com/channels/@me/123/")
                            for l in out.splitlines()))

    def test_the_text_mode_is_unchanged_and_looks_up_no_guild(self):
        rc, out, rg = _run(["123", "--operator"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["[2026-09-12T17:59:59] susanliu_: first é",
                                            "[2026-09-12T18:00:01] Sutando-Pro: second"])
        rg.assert_not_called()

    def test_the_gate_still_runs_first_in_jsonl_mode(self):
        out = io.StringIO()
        with mock.patch.object(dr, "_load_token", return_value="tok"), \
             mock.patch.object(dr, "_fetch") as fetch, \
             mock.patch.object(dr.discord_context_policy, "gate", return_value="blocked by policy"), \
             contextlib.redirect_stdout(out):
            rc = dr.main(["123", "--serving", "456", "--jsonl"])
        self.assertEqual(rc, 2)
        fetch.assert_not_called()
        self.assertIn("BLOCKED", out.getvalue())



class ReplyToId(unittest.TestCase):
    def test_carries_the_parent_key_even_when_the_rendered_reply_is_clipped(self):
        rc, out, _ = _run(["123", "--operator", "--jsonl"], messages=REPLY_MESSAGES)
        self.assertEqual(rc, 0)
        rows = [json.loads(l) for l in out.strip().splitlines()]
        by_id = {r["id"]: r for r in rows}
        child, parent = by_id["2000"], by_id["1000"]

        # the edge is followable by KEY
        self.assertEqual(child["reply_to_id"], "1000")
        self.assertIn(child["reply_to_id"], by_id)

        # a message that replies to nothing carries an empty key, never a missing one
        self.assertEqual(parent["reply_to_id"], "")

        # why the key is needed: the rendered form is truncated, so a consumer
        # string-matching `reply` cannot recover the parent it names.
        self.assertIn("replying to susanliu_", child["reply"])
        rendered_body = child["reply"].split(": ", 1)[1]
        self.assertNotEqual(rendered_body, parent["text"])          # truncated
        self.assertLess(len(rendered_body), len(LONG_PARENT))

if __name__ == "__main__":
    unittest.main()
