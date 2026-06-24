"""Tests for observability.channel.emit_channel — the single-source builder of
``channel.<surface>.<in|out>`` events for the chat bridges.

Locks the taxonomy (kind/source mapping), the actor shape, the data merge +
``channel_id`` setdefault, and the best-effort contract (a blowing-up sink must
not propagate out of emit_channel).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from observability import obs  # noqa: E402
from observability.channel import emit_channel  # noqa: E402


class Capture:
    type = "capture"

    def __init__(self) -> None:
        self.events: list[dict] = []

    def write(self, ev: dict) -> None:
        self.events.append(ev)


class EmitChannelTest(unittest.TestCase):
    def setUp(self) -> None:
        obs.reset_sinks()
        self.cap = Capture()
        obs.register_sink(self.cap)
        obs.set_sampler(lambda ev: True)  # keep ok events deterministically

    def tearDown(self) -> None:
        obs.reset_sinks()

    def test_kind_and_source_mapping(self) -> None:
        cases = {
            ("discord", "in"): "discord-bridge",
            ("discord", "out"): "discord-bridge",
            ("telegram", "in"): "telegram-bridge",
            ("telegram", "out"): "telegram-bridge",
            ("slack", "in"): "slack-bridge",
            ("slack", "out"): "slack-bridge",
        }
        for (surface, direction), source in cases.items():
            self.cap.events.clear()
            emit_channel(surface, direction, channel_id="C1")
            ev = self.cap.events[0]
            self.assertEqual(ev["kind"], f"channel.{surface}.{direction}")
            self.assertEqual(ev["source"], source)
            self.assertEqual(ev["outcome"], "ok")

    def test_unknown_surface_synthesizes_source(self) -> None:
        emit_channel("whatsapp", "in", channel_id="W1")
        self.assertEqual(self.cap.events[0]["source"], "whatsapp-bridge")
        self.assertEqual(self.cap.events[0]["kind"], "channel.whatsapp.in")

    def test_actor_shape(self) -> None:
        emit_channel("slack", "in", user_id="U9", channel_id="D5", access_tier="team")
        actor = self.cap.events[0]["actor"]
        self.assertEqual(actor, {"user_id": "U9", "channel": "slack", "access_tier": "team"})

    def test_default_tier_is_unknown_not_owner(self) -> None:
        # Fail-safe: an unspecified tier must NOT silently become "owner",
        # which would upgrade a non-owner reply in per-tier accounting.
        emit_channel("slack", "out", channel_id="C1")
        self.assertEqual(self.cap.events[0]["actor"]["access_tier"], "unknown")

    def test_tier_normalized_to_schema(self) -> None:
        # Bridge vocabulary (owner/team/other) → obs AccessTier
        # (owner/team/public/unknown). "other" must map to the schema's
        # "public"; anything unrecognized → "unknown" (never "owner").
        cases = {
            "owner": "owner",
            "team": "team",
            "other": "public",
            "public": "public",
            "unknown": "unknown",
            "bogus": "unknown",
        }
        for raw, expected in cases.items():
            self.cap.events.clear()
            emit_channel("slack", "in", channel_id="C1", access_tier=raw)
            self.assertEqual(self.cap.events[0]["actor"]["access_tier"], expected, raw)

    def test_outcome_passthrough(self) -> None:
        # A failed delivery must be emittable as outcome="error" (and obs never
        # samples error away).
        emit_channel("slack", "out", channel_id="C1", outcome="error")
        self.assertEqual(self.cap.events[0]["outcome"], "error")

    def test_user_id_coerced_to_str(self) -> None:
        emit_channel("telegram", "in", user_id=12345, channel_id=12345)
        self.assertEqual(self.cap.events[0]["actor"]["user_id"], "12345")

    def test_channel_id_injected_into_data(self) -> None:
        emit_channel("discord", "out", channel_id="222", data={"task_id": "task-1"})
        self.assertEqual(self.cap.events[0]["data"], {"task_id": "task-1", "channel_id": "222"})

    def test_channel_id_setdefault_does_not_clobber(self) -> None:
        emit_channel("discord", "in", channel_id="222", data={"channel_id": "explicit"})
        self.assertEqual(self.cap.events[0]["data"]["channel_id"], "explicit")

    def test_no_data_when_empty(self) -> None:
        emit_channel("slack", "out")  # no channel_id, no data
        self.assertNotIn("data", self.cap.events[0])

    def test_trace_id_passthrough(self) -> None:
        emit_channel("slack", "in", channel_id="C1", trace_id="tr_FIXED")
        self.assertEqual(self.cap.events[0]["trace_id"], "tr_FIXED")

    def test_best_effort_never_raises(self) -> None:
        class Bad:
            type = "bad"

            def write(self, ev: dict) -> None:
                raise RuntimeError("boom")

        obs.register_sink(Bad())
        emit_channel("discord", "in", channel_id="222")  # must not raise
        self.assertEqual(len(self.cap.events), 1)


if __name__ == "__main__":
    unittest.main()
