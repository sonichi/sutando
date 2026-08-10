#!/usr/bin/env python3
"""`src/dm-result.py` must honor the skip markers, not just the file markers.

`poll_dm_fallback` (discord-bridge) shells out to dm-result.py for any
`results/task-*.txt` that no channel consumer claimed within 90s. dm-result.py
already parses `[file:|send:|attach:]` — its own comment gives the reason:
"without parsing these markers it would deliver the literal text ... in the DM".

It did NOT parse the SKIP markers, and that argument is stronger for them: a
`[no-send]` body is not merely ugly in a DM, it is a body whose whole point is
that it must not be delivered. Being the LAST consumer in the chain, a marker
this script ignores becomes exactly the DM the marker existed to prevent.

Each case asserts the marker path is taken WITHOUT reaching the network — the
test never provides a token, so any real send attempt would be visible.

Run: python3 tests/dm-result-skip-markers.test.py
"""
from __future__ import annotations

import importlib.util
import io
import contextlib
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

_spec = importlib.util.spec_from_file_location("dm_result", REPO / "src" / "dm-result.py")
dm = importlib.util.module_from_spec(_spec)
sys.modules["dm_result"] = dm
_spec.loader.exec_module(dm)


class SkipMarkers(unittest.TestCase):
    def _run(self, text: str):
        """Invoke main() with `text`, recording whether delivery was attempted."""
        called = {"voice": False, "send": False}

        def fake_voice():
            called["voice"] = True
            return False

        def fake_send(_t):
            called["send"] = True
            return True

        orig_voice, orig_send, orig_argv = dm.voice_connected, dm.send_dm, sys.argv
        dm.voice_connected, dm.send_dm = fake_voice, fake_send
        sys.argv = ["dm-result.py", text]
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                try:
                    dm.main()
                except SystemExit as e:
                    called["exit"] = e.code
        finally:
            dm.voice_connected, dm.send_dm, sys.argv = orig_voice, orig_send, orig_argv
        return called, out.getvalue()

    # --- the markers that must suppress delivery -----------------------------

    def test_no_send_is_not_delivered(self):
        called, out = self._run("[no-send]\nInternally handled, nothing for the owner.")
        self.assertFalse(called["send"], "a [no-send] body was delivered to the owner's DM")
        self.assertFalse(called["voice"], "returned before the voice probe, as intended")
        self.assertIn("no-send", out)

    def test_replied_is_not_delivered(self):
        called, _ = self._run("[REPLIED]\nAlready answered in the channel.")
        self.assertFalse(called["send"], "a [REPLIED] body was delivered again")

    def test_deduped_is_not_delivered(self):
        called, _ = self._run("[deduped: task-123]\nSuperseded.")
        self.assertFalse(called["send"], "a [deduped:] body was delivered")

    # --- controls: ordinary bodies MUST still be delivered -------------------

    def test_plain_body_is_delivered(self):
        called, _ = self._run("Here is the answer you asked for.")
        self.assertTrue(called["send"], "an ordinary result stopped being delivered")

    def test_body_merely_mentioning_the_marker_is_delivered(self):
        # The marker only counts at the START of the body. Prose that discusses
        # it must not be silently swallowed — that would be a new failure mode
        # in the opposite direction (cf. PR #2481 for [dm-only]).
        called, _ = self._run("I used the [no-send] marker on that task earlier.")
        self.assertTrue(called["send"], "prose merely mentioning [no-send] was suppressed")

    def test_file_marker_still_delivered(self):
        # The pre-existing attachment-marker handling must be unaffected.
        called, _ = self._run("[file: /tmp/x.png] here is the chart")
        self.assertTrue(called["send"], "file-marker handling regressed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
