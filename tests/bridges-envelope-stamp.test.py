#!/usr/bin/env python3
"""#3014 writer census: telegram and slack stamp the HMAC envelope at their edges.

Both write with a bare `write_text` rather than sparrow's `write_task_file`, so
the `set_task_stamper()` seam never reaches them — they need an edge stamp, the
shape discord-bridge and cron-runner (#3041) use.

WIRING pins only, and deliberately so: importing either bridge would make this
the 27th offender of `lint-hermetic-bridge-tests` (they resolve channel config at
MODULE level, so an unisolated CLAUDE_CONFIG_DIR reads a real allowlist). The
SEMANTICS this wiring delivers — a stamped file that verifies, and fail-open when
stamping raises — are covered behaviourally by #3041's cron-runner tests, against a
writer that needs no bridge import. That PR is not merged yet, so until it lands the
behavioural proof for this shape lives there and not in this tree.

What each pin refuses: a stamp applied to something other than the bytes written,
a write that bypasses the stamped value, and stamping placed after the write.
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class SlackEdgeStampWiring(unittest.TestCase):
    def test_central_writer_stamps_the_content_it_writes(self):
        src = (REPO / "src" / "slack-bridge.py").read_text()
        self.assertIn("from task_envelope import stamp_text", src)
        self.assertIn("content = stamp_text(content, REPO)", src,
                      "the stamp must be applied to the variable that is written")
        self.assertIn("task_file.write_text(content)", src)
        self.assertLess(src.index("content = stamp_text(content, REPO)"),
                        src.index("task_file.write_text(content)"),
                        "stamping must precede the write")

    def test_stamp_is_fail_open(self):
        """A stamping error must cost the stamp, never the task."""
        src = (REPO / "src" / "slack-bridge.py").read_text()
        i = src.index("content = stamp_text(content, REPO)")
        window = src[max(0, i - 200):i + 120]
        self.assertIn("except Exception:", window,
                      "the stamp call must sit inside its own except-guard")


class TelegramEdgeStampWiring(unittest.TestCase):
    def test_stamps_the_content_it_then_writes(self):
        src = (REPO / "src" / "telegram-bridge.py").read_text()
        self.assertIn("from task_envelope import stamp_text", src)
        self.assertIn("_task_content = stamp_text(_task_content, REPO)", src,
                      "the stamp must be applied to the variable that is written")
        self.assertIn("task_file.write_text(_task_content)", src,
                      "the write must consume the stamped variable, not a fresh f-string")
        self.assertLess(src.index("_task_content = stamp_text(_task_content, REPO)"),
                        src.index("task_file.write_text(_task_content)"),
                        "stamping must precede the write")

    def test_stamp_is_fail_open(self):
        src = (REPO / "src" / "telegram-bridge.py").read_text()
        i = src.index("_task_content = stamp_text(_task_content, REPO)")
        self.assertIn("except Exception:", src[i:i + 140],
                      "the stamp call must sit inside its own except-guard")


if __name__ == "__main__":
    unittest.main(verbosity=2)
