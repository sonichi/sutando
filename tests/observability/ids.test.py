"""Twin of ids.test.ts — same format guarantees on the Python side."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from observability import ids  # noqa: E402

ULID_BODY = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
TRACE = re.compile(r"^tr_[0-9A-HJKMNP-TV-Z]{26}$")
USAGE = re.compile(r"^ux_[0-9A-HJKMNP-TV-Z]{26}$")
SPAN = re.compile(r"^sp_[0-9a-f]{16}$")


class IdsTest(unittest.TestCase):
    def test_prefixes_and_body(self) -> None:
        self.assertRegex(ids.new_trace_id(), TRACE)
        self.assertRegex(ids.new_usage_id(), USAGE)
        self.assertRegex(ids.ulid(), ULID_BODY)

    def test_span_id(self) -> None:
        self.assertRegex(ids.new_span_id(), SPAN)

    def test_time_sortable(self) -> None:
        early = ids.new_trace_id(1_000_000_000_000)
        late = ids.new_trace_id(1_700_000_000_000)
        self.assertLess(early, late)

    def test_collision_free(self) -> None:
        seen = {ids.new_trace_id() for _ in range(10_000)}
        self.assertEqual(len(seen), 10_000)

    def test_no_ambiguous_chars(self) -> None:
        for _ in range(200):
            self.assertNotRegex(ids.ulid(), re.compile(r"[ILOU]"))


if __name__ == "__main__":
    unittest.main()
