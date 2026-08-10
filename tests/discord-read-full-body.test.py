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
import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

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


def msg(content, author="Sutando-Mini", mid="1000"):
    # `id` is required: main() sorts the batch by int(m["id"]).
    return {"id": mid, "content": content, "author": {"username": author}}


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

    def _run_main(self, argv, messages):
        """Drive main() with the network stubbed, and capture what it prints.

        Asserting the wiring as a source literal (`"None if args.full else CLIP" in
        src`) is what the first draft of this test did. That form breaks on
        reformatting and passes on a call site that is present but wrong, so it
        tests the text rather than the behaviour. Driving main() is the version that
        actually fails when the flag is not threaded through.
        """
        buf = io.StringIO()
        with mock.patch.object(dr, "_fetch", return_value=messages), \
             mock.patch.object(dr, "_load_token", return_value="token"), \
             contextlib.redirect_stdout(buf):
            dr.main(argv)
        return buf.getvalue()

    def test_main_clips_by_default(self):
        out = self._run_main(["123"], [msg(LONG)])
        self.assertNotIn("NEEDLE_PAST_THE_CLIP", out)

    def test_main_under_full_prints_the_whole_body(self):
        """The end-to-end pin: fails if the flag exists but main() never passes it."""
        out = self._run_main(["123", "--full"], [msg(LONG)])
        self.assertIn("NEEDLE_PAST_THE_CLIP", out)

    def test_main_under_full_also_lifts_the_reply_target(self):
        ref = {"content": LONG, "author": {"username": "sonichi"}}
        m = {"id": "1001", "content": "2 merge", "author": {"username": "sonichi"},
             "referenced_message": ref}
        self.assertNotIn("NEEDLE_PAST_THE_CLIP", self._run_main(["123"], [m]))
        self.assertIn("NEEDLE_PAST_THE_CLIP", self._run_main(["123", "--full"], [m]))

    def test_argparse_accepts_it(self):
        args = dr._parse_args(["123", "--full"])
        self.assertTrue(args.full)
        self.assertFalse(dr._parse_args(["123"]).full)


if __name__ == "__main__":
    unittest.main(verbosity=2)
