"""Twin of obs.test.ts."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from observability import obs  # noqa: E402


class Capture:
    type = "capture"

    def __init__(self) -> None:
        self.events: list[dict] = []

    def write(self, ev: dict) -> None:
        self.events.append(ev)


def base_input() -> dict:
    return {
        "source": "voice-agent",
        "actor": {"user_id": "u1", "channel": "voice", "access_tier": "owner"},
        "kind": "tool.call",
        "outcome": "ok",
    }


class EmitTest(unittest.TestCase):
    def setUp(self) -> None:
        obs.reset_sinks()
        self.cap = Capture()
        obs.register_sink(self.cap)

    def tearDown(self) -> None:
        obs.reset_sinks()

    def test_stamps_and_keeps_fields(self) -> None:
        obs.emit({**base_input(), "source_file": "tasks/task-1.txt", "data": {"tool_name": "Read"}})
        self.assertEqual(len(self.cap.events), 1)
        ev = self.cap.events[0]
        self.assertEqual(ev["schema"], 1)
        self.assertIsInstance(ev["ts"], float)
        self.assertRegex(ev["trace_id"], re.compile(r"^tr_"))
        self.assertTrue(ev["node"])
        self.assertEqual(ev["source"], "voice-agent")
        self.assertEqual(ev["source_file"], "tasks/task-1.txt")
        self.assertEqual(ev["data"], {"tool_name": "Read"})

    def test_supplied_trace_id(self) -> None:
        obs.emit({**base_input(), "trace_id": "tr_FIXED"})
        self.assertEqual(self.cap.events[0]["trace_id"], "tr_FIXED")

    def test_never_throws_other_sinks_receive(self) -> None:
        class Bad:
            type = "bad"

            def write(self, ev: dict) -> None:
                raise RuntimeError("boom")

        obs.register_sink(Bad())
        obs.emit(base_input())  # must not raise
        self.assertEqual(len(self.cap.events), 1)

    def test_sampling_drops_ok(self) -> None:
        obs.set_sampler(lambda ev: False)
        obs.emit(base_input())
        self.assertEqual(len(self.cap.events), 0)

    def test_never_samples_error_denied_usage(self) -> None:
        obs.set_sampler(lambda ev: False)
        obs.emit({**base_input(), "outcome": "error"})
        obs.emit({**base_input(), "outcome": "denied"})
        obs.emit({**base_input(), "kind": "usage.recorded"})
        self.assertEqual(len(self.cap.events), 3)

    def test_omits_unsupplied_optionals(self) -> None:
        obs.emit(base_input())
        ev = self.cap.events[0]
        for k in ("span_id", "duration_ms", "usage", "source_file"):
            self.assertNotIn(k, ev)


if __name__ == "__main__":
    unittest.main()
