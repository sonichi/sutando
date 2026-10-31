#!/usr/bin/env python3
"""say and mention refuse a room argument that is not a room id or alias, before sending."""
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/agent-room-ops"))
spec = importlib.util.spec_from_file_location("room_ops_room_arg_test", ROOT / "skills/agent-room-ops/room_ops.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli._main(argv)
    return rc, json.loads(out.getvalue())


class RoomArgTests(unittest.TestCase):
    def test_say_with_the_message_in_the_room_slot_never_sends(self):
        with patch.object(cli._say, "say") as say:
            rc, res = run(["say", "Got it, on it now.", "!room:test.invalid"])
        self.assertEqual(rc, 1)
        self.assertIs(res["ok"], False)
        self.assertIn("argument order", res["reason"])
        say.assert_not_called()

    def test_mention_with_the_room_first_never_sends(self):
        with patch.object(cli._mention, "mention") as mention:
            rc, res = run(["mention", "!room:test.invalid", "peer", "ping"])
        self.assertEqual(rc, 1)
        self.assertIs(res["ok"], False)
        mention.assert_not_called()

    def test_a_room_id_with_whitespace_is_refused(self):
        self.assertIsNotNone(cli._room_arg_problem("!room id:test.invalid"))
        self.assertIsNotNone(cli._room_arg_problem(None))

    def test_room_ids_and_aliases_pass(self):
        for room in ("!room:test.invalid", "#team:test.invalid"):
            self.assertIsNone(cli._room_arg_problem(room))

    def test_send_and_react_with_a_swapped_room_never_send(self):
        with patch.object(cli._media, "send_media") as send, patch.object(cli._react, "react") as react:
            self.assertEqual(run(["send", "notes.txt", "!room:test.invalid"])[0], 1)
            self.assertEqual(run(["react", "$event:test.invalid", "!room:test.invalid", "--key", "+1"])[0], 1)
        send.assert_not_called()
        react.assert_not_called()

    def test_only_room_ids_and_aliases_pass(self):
        for value in ("@peer:test.invalid", "ok", "$event:test.invalid", "", "room"):
            with self.subTest(value=value):
                self.assertIsNotNone(cli._room_arg_problem(value))


if __name__ == "__main__":
    unittest.main()
