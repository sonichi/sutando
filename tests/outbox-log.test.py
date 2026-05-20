"""Unit tests for src/outbox_log.py.

Run: `python3 tests/outbox-log.test.py`
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import outbox_log  # noqa: E402


class TestOutboxAppend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        os.environ["SUTANDO_WORKSPACE"] = str(self.workspace)
        # Some helper modules cache resolve_workspace via module-level constants;
        # outbox_log does NOT cache (calls _outbox_path() fresh on each append)
        # so we don't need to reload.

    def tearDown(self):
        os.environ.pop("SUTANDO_WORKSPACE", None)
        self.tmp.cleanup()

    def _read(self) -> list[dict]:
        path = self.workspace / "state" / "outbox.log"
        if not path.is_file():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_append_writes_one_jsonl_entry(self):
        outbox_log.append(
            channel_type="discord_channel",
            recipient="C09XYZ",
            body="Hello world",
            core_id="2",
        )
        rows = self._read()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["channel_type"], "discord_channel")
        self.assertEqual(r["recipient"], "C09XYZ")
        self.assertEqual(r["core_id"], "2")
        self.assertEqual(r["body_preview"], "Hello world")
        self.assertEqual(r["body_len"], len("Hello world"))
        self.assertIn("ts", r)
        self.assertIn("iso_ts", r)

    def test_append_collapses_newlines_in_preview(self):
        outbox_log.append(
            channel_type="discord_dm",
            recipient="1025828152183885925",
            body="line one\nline two\nline three",
        )
        r = self._read()[0]
        self.assertIn("¶", r["body_preview"])
        self.assertNotIn("\n", r["body_preview"])
        # body_len preserves the original (pre-collapse) length.
        self.assertEqual(r["body_len"], len("line one\nline two\nline three"))

    def test_append_truncates_long_preview(self):
        body = "x" * 500
        outbox_log.append(channel_type="telegram", recipient="123", body=body)
        r = self._read()[0]
        self.assertLessEqual(len(r["body_preview"]), 200)
        self.assertTrue(r["body_preview"].endswith("…"))
        self.assertEqual(r["body_len"], 500)

    def test_append_preserves_utf8(self):
        body = "中文测试 🎉"
        outbox_log.append(channel_type="discord_dm", recipient="1025828152183885925", body=body)
        r = self._read()[0]
        self.assertEqual(r["body_preview"], body)
        # Raw file should contain UTF-8, not \uXXXX escapes.
        raw = (self.workspace / "state" / "outbox.log").read_text(encoding="utf-8")
        self.assertIn("中文测试", raw)
        self.assertIn("🎉", raw)

    def test_optional_fields_emitted_when_provided(self):
        outbox_log.append(
            channel_type="slack_channel",
            recipient="C09XYZ",
            body="...",
            task_id="task-1779253146729",
            recipient_label="#dev",
        )
        r = self._read()[0]
        self.assertEqual(r["task_id"], "task-1779253146729")
        self.assertEqual(r["recipient_label"], "#dev")

    def test_optional_fields_omitted_when_absent(self):
        outbox_log.append(channel_type="telegram", recipient="123", body="x")
        r = self._read()[0]
        self.assertNotIn("task_id", r)
        self.assertNotIn("recipient_label", r)

    def test_core_id_falls_back_to_env(self):
        os.environ["SUTANDO_CORE_ID"] = "test-core"
        try:
            outbox_log.append(channel_type="telegram", recipient="123", body="x")
            r = self._read()[0]
            self.assertEqual(r["core_id"], "test-core")
        finally:
            os.environ.pop("SUTANDO_CORE_ID", None)

    def test_core_id_unknown_when_neither_provided(self):
        os.environ.pop("SUTANDO_CORE_ID", None)
        outbox_log.append(channel_type="telegram", recipient="123", body="x")
        r = self._read()[0]
        self.assertEqual(r["core_id"], "unknown")

    def test_append_never_raises_on_bad_workspace(self):
        # Point workspace at a path the test user cannot write to.
        os.environ["SUTANDO_WORKSPACE"] = "/nonexistent/cannot/create/here"
        # Must not raise.
        outbox_log.append(channel_type="telegram", recipient="123", body="x")

    def test_append_appends_not_overwrites(self):
        for i in range(3):
            outbox_log.append(channel_type="telegram", recipient=str(i), body=f"msg {i}")
        rows = self._read()
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["recipient"] for r in rows], ["0", "1", "2"])

    def test_read_recent_returns_last_n(self):
        for i in range(10):
            outbox_log.append(channel_type="telegram", recipient=str(i), body=f"m{i}")
        rows = outbox_log.read_recent(limit=3)
        self.assertEqual([r["recipient"] for r in rows], ["7", "8", "9"])

    def test_read_recent_empty_when_no_file(self):
        self.assertEqual(outbox_log.read_recent(), [])

    def test_read_recent_skips_malformed_lines(self):
        outbox_log.append(channel_type="telegram", recipient="ok", body="x")
        # Corrupt the file by injecting a non-JSON line.
        path = self.workspace / "state" / "outbox.log"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("not-json-at-all\n")
        outbox_log.append(channel_type="telegram", recipient="ok2", body="y")
        rows = outbox_log.read_recent()
        self.assertEqual([r["recipient"] for r in rows], ["ok", "ok2"])


if __name__ == "__main__":
    unittest.main()
