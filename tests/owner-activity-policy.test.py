#!/usr/bin/env python3
"""Contract tests for the shared owner-activity state writer."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from owner_activity import write_owner_activity  # noqa: E402


class OwnerActivityPolicyTests(unittest.TestCase):
    def test_schema_summary_bound_and_optional_channel_id(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state" / "last-owner-activity.json"
            self.assertTrue(
                write_owner_activity(target, "discord", "x" * 100, 12345)
            )
            data = json.loads(target.read_text())
            self.assertEqual(data["channel"], "discord")
            self.assertEqual(data["summary"], "x" * 80)
            self.assertEqual(data["channel_id"], "12345")
            self.assertIsInstance(data["ts"], int)

            self.assertTrue(write_owner_activity(target, "slack", "again"))
            self.assertNotIn("channel_id", json.loads(target.read_text()))

    def test_real_writer_is_safe_across_same_process_threads(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "last-owner-activity.json"
            failures = []

            def write_many(worker: int) -> None:
                for i in range(200):
                    if not write_owner_activity(
                        target, "slack", f"worker-{worker}-{i}", worker
                    ):
                        failures.append((worker, i))

            threads = [
                threading.Thread(target=write_many, args=(worker,))
                for worker in range(12)
            ]
            for thread in threads:
                thread.start()
            while any(thread.is_alive() for thread in threads):
                if target.exists():
                    json.loads(target.read_text())
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [])
            json.loads(target.read_text())
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

    def test_failure_is_reported_without_raising(self):
        with tempfile.TemporaryDirectory() as td:
            parent_file = Path(td) / "not-a-directory"
            parent_file.write_text("occupied")
            errors = []
            ok = write_owner_activity(
                parent_file / "last-owner-activity.json",
                "telegram",
                "hello",
                on_error=errors.append,
            )
            self.assertFalse(ok)
            self.assertEqual(len(errors), 1)

            def broken_logger(_exc):
                raise RuntimeError("logger unavailable")

            self.assertFalse(
                write_owner_activity(
                    parent_file / "last-owner-activity.json",
                    "telegram",
                    "hello",
                    on_error=broken_logger,
                )
            )

    def test_python_adapters_delegate_instead_of_copying_policy(self):
        for filename in (
            "discord-bridge.py",
            "slack-bridge.py",
            "telegram-bridge.py",
        ):
            source = (REPO / "src" / filename).read_text()
            self.assertIn(
                "from owner_activity import write_owner_activity as "
                "_write_owner_activity_shared",
                source,
            )
            self.assertNotIn("OWNER_ACTIVITY_FILE.with_suffix", source)


if __name__ == "__main__":
    unittest.main()
