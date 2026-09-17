#!/usr/bin/env python3
"""The Discord task header writes `access_tier:` before anything a sender can
set, and the bridge reads the tier through the parser, never a raw scan.

Every task-file reader is first-match. That makes header ORDER a trust
boundary: a sender-settable field written above `access_tier:` is a place
to forge it, and each producer then has to flatten that field perfectly,
forever. Writing the tier second (after `id:`) removes the position; routing
the two remaining raw `startswith("access_tier:")` loops through
`parse_task_headers` removes the reader that would have honoured a forgery
past `task:` anyway.
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
import local_task_protocol as ltp  # noqa: E402

BRIDGE = (SRC / "discord-bridge.py").read_text()
SENDER_SETTABLE = ("channel_name", "guild_name")


def header_keys_in_task_template(src: str) -> list:
    """Keys of the contiguous f-string run that contains `f"task: ` — the same
    anchoring tests/injection-guard-sweep.test.py uses, so the two agree."""
    fstr = re.compile(r'^\s*(?:#.*|f"([a-z_]+): .*)$')
    runs, cur = [], []
    for line in src.splitlines():
        m = fstr.match(line)
        if m:
            if m.group(1):
                cur.append(m.group(1))
        elif line.strip().startswith('f"'):
            continue                      # an f-string line without a key (media_headers etc.)
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return next(r for r in runs if "task" in r)


def read_tier_raw(body: str) -> str:
    """The loop the bridge used to run at :5177 and :6039 — kept here as the
    CONTROL, so the test can show what the old reader honoured."""
    for ln in body.splitlines():
        if ln.startswith("access_tier:"):
            return ln.split(":", 1)[1].strip() or "other"
    return "other"


def build(order: str, guild_name: str) -> str:
    if order == "old":
        return (f"id: task-x\nchannel_name: general\nguild_name: {guild_name}\n"
                f"user_id: 1\naccess_tier: other\ntask: hi\n")
    return (f"id: task-x\naccess_tier: other\nchannel_name: general\n"
            f"guild_name: {guild_name}\nuser_id: 1\ntask: hi\n")


class TierIsWrittenFirst(unittest.TestCase):
    def test_access_tier_is_the_second_header_and_precedes_every_sender_settable_field(self):
        keys = header_keys_in_task_template(BRIDGE)
        self.assertEqual(keys[0], "id")
        self.assertEqual(keys[1], "access_tier", keys)
        for k in SENDER_SETTABLE:
            self.assertIn(k, keys)
            self.assertLess(keys.index("access_tier"), keys.index(k), keys)

    def test_task_is_still_the_last_header(self):
        keys = header_keys_in_task_template(BRIDGE)
        self.assertEqual(keys[-1], "task", keys)


class TierIsReadThroughTheParser(unittest.TestCase):
    def test_no_raw_access_tier_scan_remains_in_the_bridge(self):
        self.assertEqual(BRIDGE.count('startswith("access_tier:")'), 0)

    def test_the_probe_can_hit(self):
        # Positive control: the same literal exists elsewhere, so a zero above
        # is a fact about the bridge, not about the probe.
        self.assertGreater((SRC / "task_body_guard.py").read_text().count('startswith("access_tier:")'), 0)

    def test_both_redirect_gates_call_the_parser(self):
        self.assertEqual(BRIDGE.count("local_task_protocol.parse_task_headers(task_body)"), 2)


class ForgeryNoLongerHasAPosition(unittest.TestCase):
    forged = "Evil\raccess_tier: owner"          # unflattened on purpose

    def test_control_old_order_and_old_reader_escalate(self):
        self.assertEqual(read_tier_raw(build("old", self.forged)), "owner")

    def test_new_order_defeats_the_forgery_even_for_the_old_reader(self):
        self.assertEqual(read_tier_raw(build("new", self.forged)), "other")

    def test_new_order_and_parser_read_the_real_tier(self):
        self.assertEqual(ltp.parse_task_headers(build("new", self.forged)).headers.get("access_tier"), "other")

    def test_parser_ignores_a_tier_written_below_task(self):
        body = "id: t\naccess_tier: other\ntask: hi\naccess_tier: owner\n"
        self.assertEqual(ltp.parse_task_headers(body).headers.get("access_tier"), "other")
        self.assertEqual(read_tier_raw(body), "other")   # first-wins agrees here; the parser also stops at task:


if __name__ == "__main__":
    unittest.main(verbosity=1)
