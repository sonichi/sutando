#!/usr/bin/env python3
"""`dedup_same_relay_asker`, the check a relay ask's fold rests on, in src/.

A relay ask carries no `channel_id`, and its `user_id` is the account it runs for rather
than who asked, so the only proof two relay asks share a destination and a sender is
`source_room_id` plus `asked_by`, present and equal on both. The bridge-level test
(gateway-relay-fold-asked-by.test.py) drives the packaged copy through the bridge; this
one pins the canonical src/ helper the package is synced from.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from result_markers import dedup_same_relay_asker  # noqa: E402

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"


def _relay(tid, room=ROOM, asker=ALICE, channel=None):
    # Header shape the bridge writes for a relay ask: no channel_id, `task:` last.
    lines = [f"id: {tid}", "source: matrixrtc-voice", "user_id: @owner:example.org"]
    if channel:
        lines.append(f"channel_id: {channel}")
    if room:
        lines.append(f"source_room_id: {room}")
    if asker:
        lines.append(f"asked_by: {asker}")
    lines.append("task: what is on my calendar")
    return "\n".join(lines) + "\n"


class SameRelayAsker(unittest.TestCase):
    def test_same_room_and_same_asker_folds(self):
        self.assertTrue(dedup_same_relay_asker(_relay("ask-2"), _relay("ask-1")))

    def test_a_different_asker_does_not(self):
        self.assertFalse(dedup_same_relay_asker(_relay("ask-2", asker=BOB), _relay("ask-1")))

    def test_a_different_room_does_not(self):
        self.assertFalse(dedup_same_relay_asker(_relay("ask-2", room=OTHER_ROOM), _relay("ask-1")))

    def test_a_missing_field_on_either_side_verifies_nothing(self):
        self.assertFalse(dedup_same_relay_asker(_relay("ask-2", asker=None), _relay("ask-1")))
        self.assertFalse(dedup_same_relay_asker(_relay("ask-2"), _relay("ask-1", room=None)))
        self.assertFalse(dedup_same_relay_asker(None, _relay("ask-1")))

    def test_a_task_with_a_channel_is_not_a_relay_ask(self):
        # Channel tasks keep the existing cross-channel and cross-sender checks.
        self.assertFalse(dedup_same_relay_asker(_relay("ask-2", channel=ROOM), _relay("ask-1")))
        self.assertFalse(dedup_same_relay_asker(_relay("ask-2"), _relay("ask-1", channel=ROOM)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
