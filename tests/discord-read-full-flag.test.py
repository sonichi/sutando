#!/usr/bin/env python3
"""A clipped body is indistinguishable from a short one, so a text check over this
reader's output reports a phrase absent when it was delivered.

Measured 2026-08-10. Both greps ran against the SAME delivered message:

    'Taking the #310 correction'   (opening)         -> 2 matches
    'semantically empty'           (~600 chars in)   -> 0 matches

Acting on that zero produced a duplicate post to a peer. The failure direction is
what makes it costly: the reader turns "delivered" into "looks dropped", and the
response to a dropped message is to send it again.

`--full` disables both clips. The defaults are unchanged — the clip is right for
scanning a channel, and wrong for verifying a specific string reached it.

These cases drive `main()` with the fetch stubbed, so they exercise the SHIPPED
flag-to-clip wiring. Setting `CLIP = None` by hand instead would test Python's
slicing and stay green if the flag were never wired up.
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

TAIL = "PHRASE_PAST_THE_CLIP"
LONG = ("x" * 400) + TAIL
MESSAGES = [{
    "id": "100", "timestamp": "2026-08-10T05:00:00.000000+00:00",
    "content": LONG, "author": {"username": "Sutando-Pro"},
    "referenced_message": {"content": LONG, "author": {"username": "Sutando-Mini"}},
}]


def run(argv):
    """Run main() with the network and token stubbed; return stdout."""
    buf = io.StringIO()
    with mock.patch.object(dr, "_load_token", return_value="tok"), \
         mock.patch.object(dr, "_fetch", return_value=list(MESSAGES)), \
         contextlib.redirect_stdout(buf):
        rc = dr.main(argv)
    assert rc == 0, f"main() returned {rc}"
    return buf.getvalue()


class FullFlag(unittest.TestCase):
    def setUp(self):
        self._clip, self._reply_clip = dr.CLIP, dr.REPLY_CLIP

    def tearDown(self):
        dr.CLIP, dr.REPLY_CLIP = self._clip, self._reply_clip

    def test_default_clips_and_hides_the_tail(self):
        """The control. If TAIL shows up here the fixture is too short and every
        assertion below is vacuous."""
        self.assertNotIn(TAIL, run(["123"]))

    def test_full_reveals_the_tail_in_the_body(self):
        self.assertIn(TAIL, run(["123", "--full"]))

    def test_full_also_lifts_the_reply_clip(self):
        out = run(["123", "--full"])
        self.assertIn("replying to Sutando-Mini", out)
        # Body and reply target both carry TAIL, so require two occurrences —
        # one match could come from the body alone and say nothing about REPLY_CLIP.
        self.assertGreaterEqual(out.count(TAIL), 2)

    def test_default_leaves_the_reply_clipped(self):
        out = run(["123"])
        self.assertIn("replying to Sutando-Mini", out)
        self.assertEqual(out.count(TAIL), 0)

    def test_flag_defaults_off(self):
        self.assertFalse(dr._parse_args(["123"]).full)
        self.assertTrue(dr._parse_args(["123", "--full"]).full)

    def test_short_bodies_are_identical_either_way(self):
        """Additive: --full must not alter anything that was not being clipped."""
        short = [dict(MESSAGES[0], content="2 merge", referenced_message=None)]
        with mock.patch.object(dr, "_load_token", return_value="tok"), \
             mock.patch.object(dr, "_fetch", return_value=short):
            buf1, buf2 = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf1):
                dr.main(["123"])
            with contextlib.redirect_stdout(buf2):
                dr.main(["123", "--full"])
        self.assertEqual(buf1.getvalue(), buf2.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
