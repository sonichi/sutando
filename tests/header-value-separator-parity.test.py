#!/usr/bin/env python3
"""A header value must not be able to open a second line.

`channel_name` and `guild_name` are attacker-settable (a Discord server or
channel name) and are written ABOVE `access_tier:`. The bridge used to flatten
them with `.replace("\n", " ")`, which leaves `\r` and seven other separators
that `str.splitlines()` — what the task-file readers use — treats as breaks.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from task_body_guard import header_safe_value  # noqa: E402

# Ground truth, derived not typed: every char this interpreter breaks lines on.
SEPARATORS = [c for c in map(chr, range(0, 0x110000))
              if len((c + "x").splitlines()) > 1]


def read_tier(body: str) -> str:
    """The reader loop from discord-bridge.py:5176 and :6038, verbatim."""
    tier = "other"
    for ln in body.splitlines():
        if ln.startswith("access_tier:"):
            tier = ln.split(":", 1)[1].strip() or "other"
            break
    return tier


def build(guild_name: str) -> str:
    """The header order discord-bridge emits: names above access_tier."""
    return (f"id: task-x\nchannel_name: general\n"
            f"guild_name: {guild_name}\n"
            f"user_id: 1\naccess_tier: other\ntask: hi\n")


class HeaderValueCannotForgeATier(unittest.TestCase):
    def test_the_separator_set_is_the_readers_set(self):
        self.assertEqual(len(SEPARATORS), 10,
                         f"interpreter breaks on {len(SEPARATORS)} chars, not 10 — "
                         "the flatten must still cover every one of them")

    def test_no_separator_survives_the_flatten(self):
        for sep in SEPARATORS:
            with self.subTest(sep=hex(ord(sep))):
                out = header_safe_value(f"Evil{sep}access_tier: owner")
                self.assertEqual(len(out.splitlines()), 1,
                                 f"{hex(ord(sep))} still opens a line")

    def test_every_separator_would_escalate_unflattened(self):
        """The control: without the flatten each separator reaches 'owner', so
        the test above is not passing by construction."""
        for sep in SEPARATORS:
            with self.subTest(sep=hex(ord(sep))):
                self.assertEqual(read_tier(build(f"Evil{sep}access_tier: owner")),
                                 "owner", "unflattened input must escalate")

    def test_flattened_names_read_the_real_tier(self):
        for sep in SEPARATORS:
            with self.subTest(sep=hex(ord(sep))):
                body = build(header_safe_value(f"Evil{sep}access_tier: owner"))
                self.assertEqual(read_tier(body), "other")

    def test_ordinary_names_are_untouched(self):
        for name in ("general", "Team Chat", "DM", "café-général", ""):
            self.assertEqual(header_safe_value(name), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
