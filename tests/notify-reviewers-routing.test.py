#!/usr/bin/env python3
"""notify_reviewers routing: a refusal must name what is missing, and a broken
config must never render as an unreachable person."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py"

MATRIX_OK = {"stand": "@sutando-x:ag2.space", "room": "!r:ag2.space"}
MATRIX_NO_ROOM = {"stand": "@sutando-y:ag2.space"}
DISCORD_OK = {"discord_id": "111", "home_channel": "222"}
DISCORD_NO_CHANNEL = {"discord_id": "333"}
NO_ROUTE = {"human": "someone"}


def run(roster: dict, reviewers: str, config_dir: str | None = None):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(roster, f)
        path = f.name
    env = dict(os.environ, SUTANDO_SCI_ROSTER=path)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    p = subprocess.run([sys.executable, str(SCRIPT), "--reviewers", reviewers,
                        "--message", "hello"], capture_output=True, text=True, env=env)
    os.unlink(path)
    return p


def config_with(channel: str, allow: list) -> str:
    d = tempfile.mkdtemp(prefix="cfg-notify-")
    chan = pathlib.Path(d, "channels", "discord")
    chan.mkdir(parents=True)
    (chan / "access.json").write_text(json.dumps({"groups": {channel: {"allowFrom": allow}}}))
    return d


class RoutingTest(unittest.TestCase):
    def test_discord_row_produces_a_send_plan(self):
        cfg = config_with("222", ["111"])
        p = run({"r": DISCORD_OK}, "r", cfg)
        self.assertIn("PLAN:", p.stdout)
        # The channel that was validated, and the mention that triggers a Stand.
        self.assertIn("222", p.stdout)
        self.assertIn("<@111>", p.stdout)

    def test_refusals_name_the_missing_field_not_a_generic_shape(self):
        p = run({"a": MATRIX_NO_ROOM, "b": DISCORD_NO_CHANNEL, "c": NO_ROUTE}, "a,b,c")
        self.assertIn("no 'room'", p.stderr)
        self.assertIn("no 'home_channel'", p.stderr)
        self.assertIn("no addressable route at all", p.stderr)
        # The three reasons must be distinct: one shared string is the defect
        # this test exists for -- it made every refusal read as "unreachable".
        self.assertEqual(3, len({ln for ln in p.stderr.splitlines() if "UNUSABLE" in ln}))

    def test_absent_from_channel_is_a_positive_absence(self):
        cfg = config_with("222", ["999"])          # 111 is not on the list
        p = run({"r": DISCORD_OK}, "r", cfg)
        self.assertIn("ABSENT from channel 222", p.stderr)
        self.assertNotIn("PLAN:", p.stdout)

    def test_unreadable_access_map_is_unverified_never_absent(self):
        # The safety property: a broken config must not be reported as a person
        # who cannot be reached. It sends, and says plainly it did not check.
        p = run({"r": DISCORD_OK}, "r", tempfile.mkdtemp(prefix="cfg-empty-"))
        self.assertIn("UNVERIFIED", p.stderr)
        self.assertNotIn("ABSENT", p.stderr)
        self.assertIn("PLAN:", p.stdout)

    def test_matrix_row_still_routes_through_room_ops(self):
        p = run({"r": MATRIX_OK}, "r")
        self.assertNotIn("UNUSABLE", p.stderr)
        self.assertNotIn("bot2bot-post", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
