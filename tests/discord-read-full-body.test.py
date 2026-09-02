#!/usr/bin/env python3
"""`--full` must lift both clips; the default must keep clipping.
A send-verification grep cannot see text past CLIP, so a false negative duplicates.
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

# Needle must start past CLIP=200, or the default case finds it and proves nothing.
LONG = "A" * 260 + "NEEDLE_PAST_THE_CLIP" + "B" * 300


def msg(content, author="Sutando-Mini", mid="1000"):
    # `id` is required: main() sorts the batch by int(m["id"]).
    return {"id": mid, "content": content, "author": {"username": author}}


class FullBody(unittest.TestCase):
    def test_the_needle_is_invisible_at_the_default_clip(self):
        """A grep cannot find delivered text past the clip."""
        rendered = dr._render(msg(LONG))
        self.assertEqual(len(rendered), dr.CLIP)
        self.assertNotIn("NEEDLE_PAST_THE_CLIP", rendered)

    def test_full_makes_it_visible(self):
        rendered = dr._render(msg(LONG), None)
        self.assertIn("NEEDLE_PAST_THE_CLIP", rendered)
        self.assertEqual(len(rendered), len(LONG))

    def test_the_default_is_unchanged(self):
        """Removing the clip outright would pass the case above and break every scan."""
        self.assertEqual(dr._render(msg(LONG)), LONG[:dr.CLIP])
        self.assertEqual(dr._render(msg("short")), "short")

    def test_a_body_shorter_than_the_clip_is_untouched_either_way(self):
        for clip in (dr.CLIP, None):
            self.assertEqual(dr._render(msg("brief"), clip), "brief")

    def test_clip_zero_clips_to_nothing(self):
        """`clip` is public on two functions; a falsy test made 0 mean "no clip"."""
        for fn_arg in (0, None, dr.CLIP):
            got = dr._render(msg(LONG), fn_arg)
            want = len(LONG) if fn_arg is None else fn_arg
            self.assertEqual(len(got), want, f"clip={fn_arg}")

    def test_reply_targets_clip_independently_and_full_lifts_both(self):
        ref = {"content": LONG, "author": {"username": "sonichi"}}
        m = {"content": "2 merge", "author": {"username": "sonichi"},
             "referenced_message": ref}
        self.assertNotIn("NEEDLE_PAST_THE_CLIP", dr._reply_context(m))
        self.assertIn("NEEDLE_PAST_THE_CLIP", dr._reply_context(m, None))

    def test_forwards_stay_exempt_from_the_clip(self):
        """Forwards were already exempt; --full must not become the only way."""
        fwd = {"content": "", "author": {"username": "sonichi"},
               "message_snapshots": [{"message": {"content": LONG,
                                                  "author": {"username": "x"}}}]}
        self.assertIn("NEEDLE_PAST_THE_CLIP", dr._render(fwd))

    def _run_main(self, argv, messages):
        """Drive main() with the network stubbed; a source-string assertion on the
        call site passes on a call that is present but wrong."""
        buf = io.StringIO()
        with mock.patch.object(dr, "_fetch", return_value=messages), \
             mock.patch.object(dr, "_load_token", return_value="token"), \
             contextlib.redirect_stdout(buf):
            dr.main(argv)
        return buf.getvalue()

    def test_main_clips_by_default(self):
        out = self._run_main(["123", "--operator"], [msg(LONG)])
        self.assertNotIn("NEEDLE_PAST_THE_CLIP", out)

    def test_main_under_full_prints_the_whole_body(self):
        """Fails if the flag exists but main() never passes it through."""
        out = self._run_main(["123", "--full", "--operator"], [msg(LONG)])
        self.assertIn("NEEDLE_PAST_THE_CLIP", out)

    def test_main_under_full_also_lifts_the_reply_target(self):
        ref = {"content": LONG, "author": {"username": "sonichi"}}
        m = {"id": "1001", "content": "2 merge", "author": {"username": "sonichi"},
             "referenced_message": ref}
        self.assertNotIn("NEEDLE_PAST_THE_CLIP", self._run_main(["123", "--operator"], [m]))
        self.assertIn("NEEDLE_PAST_THE_CLIP", self._run_main(["123", "--full", "--operator"], [m]))

    def test_argparse_accepts_it(self):
        args = dr._parse_args(["123", "--full"])
        self.assertTrue(args.full)
        self.assertFalse(dr._parse_args(["123"]).full)


if __name__ == "__main__":
    unittest.main(verbosity=2)
