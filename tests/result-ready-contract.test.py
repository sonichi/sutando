#!/usr/bin/env python3
"""Contract for src/delivery/readiness.py and delegation by every delivery consumer.

Readiness of a task-result file has one owner. Each consumer binds its own
results directory and keeps only provider-specific delivery.
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from delivery.readiness import is_ready_body, read_ready_result  # noqa: E402

# Every consumer that decides "is this result ready to deliver?".
CONSUMERS = {
    "discord-bridge": REPO / "src" / "discord-bridge.py",
    "slack-bridge": REPO / "src" / "slack-bridge.py",
    "telegram-bridge": REPO / "src" / "telegram-bridge.py",
    "remote_gateway_bridge": (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow"
                              / "remote_gateway_bridge.py"),
}


class ContractTest(unittest.TestCase):
    def _write(self, td: str, text: str | None) -> Path:
        p = Path(td) / "task-abc.txt"
        if text is not None:
            p.write_text(text)
        return p

    def test_missing_file_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(read_ready_result(self._write(td, None)))

    def test_zero_byte_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(read_ready_result(self._write(td, "")))

    def test_whitespace_only_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(read_ready_result(self._write(td, "\n \t\n")))

    def test_directory_is_not_ready(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "adir"
            d.mkdir()
            self.assertIsNone(read_ready_result(d))

    def test_invalid_utf8_is_not_ready(self):
        """A partial write can land mid-character; decoding must not raise."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "task-abc.txt"
            p.write_bytes(b"answer \xff\xfe")
            self.assertIsNone(read_ready_result(p))

    def test_body_is_returned_stripped(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_ready_result(self._write(td, "  hi \n")), "hi")

    def test_marker_only_body_is_ready(self):
        """[no-send] is a real body — marker handling belongs to result_markers."""
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_ready_result(self._write(td, "[no-send]")), "[no-send]")

    def test_reading_does_not_consume_the_file(self):
        """Not-ready must be retryable: the file survives for the next pass."""
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "")
            self.assertIsNone(read_ready_result(p))
            self.assertTrue(p.exists(), "an unready result file was consumed")
            p.write_text("the answer")
            self.assertEqual(read_ready_result(p), "the answer")

    def test_is_ready_body(self):
        for value in ("", "   ", "\n", None):
            self.assertFalse(is_ready_body(value), repr(value))
        self.assertTrue(is_ready_body("x"))


class DelegationTest(unittest.TestCase):
    """No consumer may re-implement the readiness check."""

    def test_every_consumer_imports_the_owner(self):
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                self.assertTrue(path.exists(), f"{name}: missing at {path}")
                self.assertRegex(
                    path.read_text(),
                    r"from (?:delivery\.readiness|\.result_ready) import read_ready_result",
                    f"{name}: does not import read_ready_result from the shared owner",
                )

    def test_no_consumer_hand_rolls_the_result_guard(self):
        """Catches a copy reintroduced under any local variable name."""
        pat = re.compile(
            r"(\w+)\s*=\s*\w*(?:result_file|rfile)\w*\.read_text\(\)\.strip\(\)",
        )
        for name, path in CONSUMERS.items():
            with self.subTest(consumer=name):
                hits = pat.findall(path.read_text())
                self.assertEqual(
                    hits, [],
                    f"{name}: reads a result file directly ({hits}) — readiness "
                    f"belongs to src/delivery/readiness.read_ready_result",
                )

    def test_sparrow_bundle_matches_src(self):
        pkg = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "result_ready.py")
        self.assertTrue(pkg.exists(), "result_ready.py not bundled into ag2-sparrow")
        self.assertEqual(
            pkg.read_text(), (REPO / "src" / "delivery" / "readiness.py").read_text(),
            "ag2-sparrow copy drifted from src/ — run tools/sync_from_src.py",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
