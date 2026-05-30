"""Tests for src/proactive_delivery.py — recover_orphan_sending_files()
(#1335 sub-PR-2).

Closes the duplication that was previously inline in discord-bridge.py,
telegram-bridge.py, and slack-bridge.py. Each call has been replaced
with a thin wrapper that delegates to the shared helper.

Python-only — no TS counterpart (proactive delivery is bridge-side).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from proactive_delivery import recover_orphan_sending_files


class TestRecoverOrphanSendingFiles(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.results = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def _write(self, name: str, content: str = "body") -> Path:
        p = self.results / name
        p.write_text(content)
        return p

    def test_recovers_proactive_sending_to_txt(self) -> None:
        self._write("proactive-1.sending", "orphaned")
        recovered = recover_orphan_sending_files(self.results)
        self.assertEqual(recovered, 1)
        self.assertFalse((self.results / "proactive-1.sending").exists())
        self.assertTrue((self.results / "proactive-1.txt").exists())
        self.assertEqual((self.results / "proactive-1.txt").read_text(), "orphaned")

    def test_skips_non_proactive_sending_files(self) -> None:
        # Only proactive-*.sending should be touched. Other files left alone.
        self._write("task-1.sending", "not proactive")
        self._write("task-1.txt", "task body")
        self._write("proactive-2.sending", "yes")
        recovered = recover_orphan_sending_files(self.results)
        self.assertEqual(recovered, 1)  # only proactive-2
        self.assertTrue((self.results / "task-1.sending").exists())
        self.assertTrue((self.results / "proactive-2.txt").exists())

    def test_skips_collision_when_txt_exists(self) -> None:
        # Defensive: if both .sending AND .txt exist, don't clobber the .txt.
        self._write("proactive-3.sending", "old orphan")
        self._write("proactive-3.txt", "new content")
        recovered = recover_orphan_sending_files(self.results)
        self.assertEqual(recovered, 0)
        # Both files should still exist:
        self.assertTrue((self.results / "proactive-3.sending").exists())
        self.assertTrue((self.results / "proactive-3.txt").exists())
        # .txt content untouched:
        self.assertEqual((self.results / "proactive-3.txt").read_text(), "new content")

    def test_returns_zero_when_dir_missing(self) -> None:
        missing = Path("/tmp/proactive-delivery-test-missing-dir-xyz123")
        if missing.exists():
            missing.rmdir()
        recovered = recover_orphan_sending_files(missing)
        self.assertEqual(recovered, 0)

    def test_returns_zero_when_no_sending_files(self) -> None:
        self._write("task-5.txt", "regular task")
        self._write("proactive-5.txt", "already delivered")
        recovered = recover_orphan_sending_files(self.results)
        self.assertEqual(recovered, 0)

    def test_idempotent_second_call_noops(self) -> None:
        self._write("proactive-6.sending", "first")
        self.assertEqual(recover_orphan_sending_files(self.results), 1)
        # Second call: orphan already recovered, no .sending files left:
        self.assertEqual(recover_orphan_sending_files(self.results), 0)

    def test_recovers_multiple_orphans(self) -> None:
        self._write("proactive-7.sending", "a")
        self._write("proactive-8.sending", "b")
        self._write("proactive-9.sending", "c")
        recovered = recover_orphan_sending_files(self.results)
        self.assertEqual(recovered, 3)
        self.assertTrue((self.results / "proactive-7.txt").exists())
        self.assertTrue((self.results / "proactive-8.txt").exists())
        self.assertTrue((self.results / "proactive-9.txt").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
