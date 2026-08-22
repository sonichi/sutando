#!/usr/bin/env python3
"""An unready proactive claim is released, not deleted.

Bridges claim `proactive-*.txt` by renaming it to `.sending`. On an empty read
they unlinked the claim, which discards a message still being written — the
file is already renamed, so nothing recovers it.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from proactive_recovery import recover_orphan_sending_files, release_claim  # noqa: E402
from delivery.readiness import read_ready_result  # noqa: E402

CONSUMERS = {
    "discord-bridge": REPO / "src" / "discord-bridge.py",
    "slack-bridge": REPO / "src" / "slack-bridge.py",
    "telegram-bridge": REPO / "src" / "telegram-bridge.py",
}


class ReleaseClaimTest(unittest.TestCase):
    def _claim(self, td: str, body: str) -> Path:
        src = Path(td) / "proactive-1785904100.txt"
        src.write_text(body)
        claim = src.with_suffix(".sending")
        src.rename(claim)
        return claim

    def test_unready_claim_returns_to_the_polling_stream(self):
        with tempfile.TemporaryDirectory() as td:
            claim = self._claim(td, "")
            self.assertTrue(release_claim(claim))
            self.assertFalse(claim.exists(), "claim was not renamed away")
            self.assertTrue(claim.with_suffix(".txt").exists(),
                            "claim was not returned to the .txt stream")

    def test_released_claim_delivers_once_written(self):
        """The message survives: released, then written, then readable."""
        with tempfile.TemporaryDirectory() as td:
            claim = self._claim(td, "")
            release_claim(claim)
            restored = claim.with_suffix(".txt")
            restored.write_text("the proactive message")
            self.assertEqual(read_ready_result(restored), "the proactive message")

    def test_release_preserves_content(self):
        with tempfile.TemporaryDirectory() as td:
            claim = self._claim(td, "half-written")
            release_claim(claim)
            self.assertEqual(claim.with_suffix(".txt").read_text(), "half-written")

    def test_release_will_not_clobber_a_newer_file(self):
        """A fresh proactive file under the same name must win."""
        with tempfile.TemporaryDirectory() as td:
            claim = self._claim(td, "")
            newer = claim.with_suffix(".txt")
            newer.write_text("newer message")
            self.assertFalse(release_claim(claim), "release clobbered a newer file")
            self.assertEqual(newer.read_text(), "newer message")

    def test_missing_claim_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(release_claim(Path(td) / "proactive-1.sending"))

    def test_unreleasable_claim_reports_and_does_not_crash(self):
        """A rename that fails for any other reason must not kill the poll loop."""
        import os
        with tempfile.TemporaryDirectory() as td:
            claim = self._claim(td, "")
            os.chmod(td, 0o500)  # read+execute: rename into this dir now fails
            try:
                if os.access(td, os.W_OK):
                    self.skipTest("directory still writable (running as root?)")
                self.assertFalse(release_claim(claim))
                self.assertTrue(claim.exists(), "claim lost on a failed release")
            finally:
                os.chmod(td, 0o700)

    def test_startup_recovery_still_works_on_a_released_claim(self):
        """Release and startup recovery must not fight over the same file."""
        with tempfile.TemporaryDirectory() as td:
            self._claim(td, "stranded")
            self.assertEqual(recover_orphan_sending_files(Path(td)), 1)


class DelegationTest(unittest.TestCase):
    def test_no_bridge_deletes_an_unready_proactive_claim(self):
        pat = re.compile(
            r"if\s+(?:not\s+text|text\s+is\s+None)\s*:\s*\n\s*\w*(?:claim|f)\w*"
            r"\.unlink\(",
        )
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                self.assertIsNone(
                    pat.search(path.read_text()),
                    f"{name}: unlinks an unready proactive claim — that discards a "
                    f"message still being written; use release_claim",
                )

    def test_every_bridge_imports_release_claim(self):
        # discord reaches release_claim through the 5b claim fence, which is
        # itself pinned to delegate (fence source + its behavioral suite).
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                text = path.read_text()
                if name == "discord-bridge":
                    self.assertIn("_proactive_fence().release", text)
                    fence = (REPO / "src" / "proactive_claim_fence.py").read_text()
                    self.assertIn("release_claim", fence)
                else:
                    self.assertIn(
                        "release_claim", text,
                        f"{name}: does not delegate to proactive_recovery.release_claim",
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
