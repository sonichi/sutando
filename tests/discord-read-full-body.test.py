#!/usr/bin/env python3
"""A 200-char clip makes this reader unsound as a send-verification instrument.

`discord-read.py` clips ordinary bodies to `CLIP = 200` so a channel scroll stays
scannable. That is right for scanning and wrong for the other thing the reader is
used for: checking whether a message you sent actually landed.

Measured 2026-08-10. A peer verified a send by grepping the channel for a phrase
from its own message, got 0, concluded the send had dropped, and re-sent. Both
greps below ran against the SAME delivered message:

    "Taking the #310 correction"   (opening)         -> 2 hits
    "semantically empty"           (~600 chars in)   -> 0 hits

The phrase was past the clip, so it could not appear no matter what pattern was
used. A false negative in this direction produces a DUPLICATE delivery, which is
the failure the verification was meant to prevent — the check's failure mode was
the bug.

`--full` makes the read faithful when it is being used as an instrument. The
default is unchanged: clipping still applies, because the scan case is the common
one and 200 chars is the right length for it.
"""
import ast
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "discord-read.py"

_spec = importlib.util.spec_from_file_location("discord_read", SCRIPT)
dr = importlib.util.module_from_spec(_spec)
sys.modules["discord_read"] = dr
try:
    _spec.loader.exec_module(dr)
except SystemExit:
    pass

# The needle must start AFTER the 200-char clip, not merely inside a long body:
# at offset 150 it ended at 170 and was visible by default, which looked like a
# code defect and was a fixture error.
LONG = "A" * 260 + "NEEDLE_PAST_THE_CLIP" + "B" * 300


def msg(content, author="Sutando-Mini"):
    return {"content": content, "author": {"username": author}}


class FullBody(unittest.TestCase):
    def test_the_needle_is_invisible_at_the_default_clip(self):
        """The defect, stated as the peer hit it: a grep cannot find delivered text."""
        rendered = dr._render(msg(LONG))
        self.assertEqual(len(rendered), dr.CLIP)
        self.assertNotIn("NEEDLE_PAST_THE_CLIP", rendered)

    def test_full_makes_it_visible(self):
        rendered = dr._render(msg(LONG), None)
        self.assertIn("NEEDLE_PAST_THE_CLIP", rendered)
        self.assertEqual(len(rendered), len(LONG))

    def test_the_default_is_unchanged(self):
        """Control: the clip must still apply when nothing is asked for. A fix that
        simply removed the clip would pass the test above and break every scan."""
        self.assertEqual(dr._render(msg(LONG)), LONG[:dr.CLIP])
        self.assertEqual(dr._render(msg("short")), "short")

    def test_a_body_shorter_than_the_clip_is_untouched_either_way(self):
        for clip in (dr.CLIP, None):
            self.assertEqual(dr._render(msg("brief"), clip), "brief")

    def test_reply_targets_clip_independently_and_full_lifts_both(self):
        ref = {"content": LONG, "author": {"username": "sonichi"}}
        m = {"content": "2 merge", "author": {"username": "sonichi"},
             "referenced_message": ref}
        self.assertNotIn("NEEDLE_PAST_THE_CLIP", dr._reply_context(m))
        self.assertIn("NEEDLE_PAST_THE_CLIP", dr._reply_context(m, None))

    def test_forwards_stay_exempt_from_the_clip(self):
        """Forwards were already exempt; --full must not be the only way to see them."""
        fwd = {"content": "", "author": {"username": "sonichi"},
               "message_snapshots": [{"message": {"content": LONG,
                                                  "author": {"username": "x"}}}]}
        self.assertIn("NEEDLE_PAST_THE_CLIP", dr._render(fwd))

    def test_the_flag_is_actually_wired_into_both_call_sites(self):
        """The function can be correct while main() never passes the flag through —
        the parse-only version of this change would leave the CLI unaffected."""
        src = SCRIPT.read_text()
        self.assertIn('"--full"', src)
        self.assertIn("None if args.full else CLIP", src)
        self.assertIn("None if args.full else REPLY_CLIP", src)

    def test_argparse_accepts_it(self):
        args = dr._parse_args(["123", "--full"])
        self.assertTrue(args.full)
        self.assertFalse(dr._parse_args(["123"]).full)


if __name__ == "__main__":
    unittest.main(verbosity=2)
