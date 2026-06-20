"""Twin of sink-jsonl.test.ts."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from observability.sink import JsonlFileSink  # noqa: E402


def sample_event() -> dict:
    return {
        "schema": 1,
        "ts": round(time.time(), 3),
        "trace_id": "tr_TEST00000000000000000000",
        "node": "mac-studio",
        "source": "filewatcher",
        "source_file": "tasks/task-9.txt",
        "actor": {"user_id": "u1", "channel": "discord", "access_tier": "owner"},
        "kind": "file.change",
        "outcome": "ok",
        "data": {"op": "created"},
    }


class JsonlFileSinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_one_compact_line_at_daily_path(self) -> None:
        ev = sample_event()
        JsonlFileSink(self.dir).write(ev)

        files = list(self.dir.iterdir())
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, re.compile(r"^events-\d{4}-\d{2}-\d{2}\.jsonl$"))

        lines = [l for l in files[0].read_text().split("\n") if l]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], json.dumps(ev, ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(json.loads(lines[0]), ev)

    def test_appends_without_clobber(self) -> None:
        sink = JsonlFileSink(self.dir)
        sink.write(sample_event())
        sink.write(sample_event())
        files = list(self.dir.iterdir())
        lines = [l for l in files[0].read_text().split("\n") if l]
        self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
