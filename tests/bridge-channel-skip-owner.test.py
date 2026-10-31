#!/usr/bin/env python3
"""A skip directly after `[channel:]` is a skip on the Discord, Slack and Telegram bridges too.

The gateway fixed this in #5340/#5346: an owner result `[channel: X]\\n[no-send]\\n...` was
posted into X as literal text, because the plain parse keeps a skip after `[channel:]` as
body (so the team guard can withhold a non-owner's redirect-plus-skip). Each bridge now has
one delivery verdict, `DELIVERY_SKIP_AFTER_CHANNEL`, read at every delivery decision on a
guarded or owner-authored body and by its guard call's suppression journal.

  a) Telegram send_reply (proactive and task paths): channel + skip sends nothing
  b) Telegram control: channel + prose still sends the prose (redirect dropped, as before)
  c) every bridge defines the verdict once, True
  d) every delivery parse site reads it, and no plain parse_markers() call is left
  e) Discord's and Slack's guard calls journal on the same verdict

Slack's reply loop is driven end to end in tests/slack-bridge-reply-outbox.test.py, and the
guard's journal flag in tests/team-guard-suppress-journaled-silent-close.test.py (case k).
Run: python3 tests/bridge-channel-skip-owner.test.py
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "src"), str(REPO / "packages" / "ag2-sparrow")]
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="bridge-skip-cfg-")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-not-real")

BRIDGES = {name: REPO / "src" / f"{name}-bridge.py" for name in ("discord", "slack", "telegram")}
FLAG = "skip_after_channel=DELIVERY_SKIP_AFTER_CHANNEL"


def _load_telegram():
    spec = importlib.util.spec_from_file_location("tgb_channel_skip", BRIDGES["telegram"])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TelegramSendReply(unittest.TestCase):
    def setUp(self):
        self.tg = _load_telegram()
        self.sent: list = []
        self.tg.api = lambda method, **kw: self.sent.append((method, kw)) or {"ok": True}

    def test_a_skip_after_channel_sends_nothing(self):
        for skip in ("[no-send]", "[REPLIED]"):
            with self.subTest(skip=skip):
                self.sent.clear()
                self.tg.send_reply(1, f"[channel: 1530802402603700415]\n{skip}\nnot for anyone")
                self.assertEqual(self.sent, [], "a skip posts nothing, never the marker as text")

    def test_control_a_redirect_with_prose_still_sends_the_prose(self):
        self.tg.send_reply(1, "[channel: 1530802402603700415]\nthe actual answer")
        texts = [kw.get("text") for m, kw in self.sent if m == "sendMessage"]
        self.assertEqual(texts, ["the actual answer"])


class EveryDeliverySiteReadsTheVerdict(unittest.TestCase):
    def source(self, name):
        return BRIDGES[name].read_text(encoding="utf-8")

    def test_each_bridge_defines_the_verdict_once_as_true(self):
        for name in BRIDGES:
            with self.subTest(bridge=name):
                self.assertEqual(re.findall(r"^DELIVERY_SKIP_AFTER_CHANNEL = (\w+)$",
                                            self.source(name), re.M), ["True"])

    def test_no_delivery_parse_reads_the_plain_verdict(self):
        expected = {"discord": 3, "slack": 2, "telegram": 2}
        for name, count in expected.items():
            with self.subTest(bridge=name):
                code = [ln for ln in self.source(name).splitlines() if not ln.lstrip().startswith("#")]
                calls = [ln for ln in code if "parse_markers(" in ln and "import" not in ln]
                self.assertEqual(sum(FLAG in ln for ln in calls), count, calls)
                self.assertEqual([ln for ln in calls if FLAG not in ln], [],
                                 "a plain parse_markers() call would post a skip after [channel:] as text")

    def test_the_guard_journals_on_the_same_verdict(self):
        for name in ("discord", "slack"):
            with self.subTest(bridge=name):
                src = self.source(name)
                start = src.index("guard_result_for_tier(") + len("guard_result_for_tier")
                depth, end = 0, start
                for end, ch in enumerate(src[start:], start):  # the call's own closing paren
                    depth += {"(": 1, ")": -1}.get(ch, 0)
                    if depth == 0:
                        break
                self.assertIn(FLAG, src[start:end + 1], f"{name}'s guard call")


if __name__ == "__main__":
    unittest.main(verbosity=1)
