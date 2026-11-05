#!/usr/bin/env python3
"""src/agent_triggers.py — the AgentTrigger registry and the playbook dispatch rule.

The registry maps each event type to its default playbook; resolve picks the
task's own playbook when it names a real skill, else the event's default, else
none (today's message handling). A playbook id never resolves outside skills/.

Run: python3 tests/agent-triggers.test.py
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import agent_triggers as at  # noqa: E402
import local_task_protocol as ltp  # noqa: E402


class Registry(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(at.EVENT_PLAYBOOKS["room.joined"], "room-onboard")
        self.assertEqual(at.EVENT_PLAYBOOKS["room.mention"], "reply")
        self.assertEqual(at.EVENT_TYPES, frozenset(at.EVENT_PLAYBOOKS))
        for pb in at.EVENT_PLAYBOOKS.values():
            self.assertEqual(at.playbook_value(pb), pb, "a default must be a valid id")

    def test_headers_are_registered(self):
        for key in ("event_type", "agent_trigger_id", "playbook"):
            self.assertIn(key, ltp.KNOWN_HEADER_KEYS)

    def test_event_type_value(self):
        self.assertEqual(at.event_type_value("room.joined"), "room.joined")
        for bad in ("room.exploded", "Room.Joined", "", None, 3, ["room.joined"]):
            self.assertIsNone(at.event_type_value(bad), bad)
        self.assertIsNone(at.default_playbook("room.exploded"))

    def test_playbook_value_is_strict(self):
        self.assertEqual(at.playbook_value("room-onboard"), "room-onboard")
        for bad in ("../etc", "room/onboard", "Room-Onboard", "-x", "a.b", "a b", "a" * 65,
                    "room-onboard\n", "", None):
            self.assertIsNone(at.playbook_value(bad), bad)

    def test_trigger_id_value(self):
        tid = "join|@agent:example.org|!room:example.org"
        self.assertEqual(at.trigger_id_value(tid), tid)
        for bad in ("a b", "x\ntask: y", "a" * 257, "", None):
            self.assertIsNone(at.trigger_id_value(bad), bad)


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.skills = base / "skills"
        for name in ("room-onboard", "other-playbook"):
            (self.skills / name).mkdir(parents=True)
            (self.skills / name / "SKILL.md").write_text(f"# {name}\n")
        # A SKILL.md outside skills/ that a traversing id would reach.
        (base / "evil").mkdir()
        (base / "evil" / "SKILL.md").write_text("# evil\n")

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self, event_type, playbook):
        return at.resolve_playbook(event_type, playbook, self.skills)

    def test_task_playbook_wins(self):
        self.assertEqual(self.resolve("room.joined", "other-playbook"),
                         self.skills / "other-playbook" / "SKILL.md")

    def test_unknown_or_invalid_playbook_falls_back_to_default(self):
        want = self.skills / "room-onboard" / "SKILL.md"
        for pb in ("no-such-skill", "../evil", "evil/..", "/etc", "", None):
            self.assertEqual(self.resolve("room.joined", pb), want, pb)

    def test_no_safe_target_means_today_s_handling(self):
        self.assertIsNone(self.resolve("room.mention", None), "reply has no skill: answer as today")
        self.assertIsNone(self.resolve("", None), "no event, no playbook: ordinary message")
        self.assertIsNone(self.resolve("room.exploded", "../evil"))
        self.assertIsNone(self.resolve(None, "no-such-skill"))

    def test_playbook_without_event_type_still_resolves(self):
        self.assertEqual(self.resolve("", "room-onboard"), self.skills / "room-onboard" / "SKILL.md")

    def test_cli_against_the_shipped_skills(self):
        def run(*args):
            return subprocess.run([sys.executable, str(REPO / "src" / "agent_triggers.py"), "resolve", *args],
                                  capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(Path(run("--event-type", "room.joined")),
                         REPO / "skills" / "room-onboard" / "SKILL.md")
        self.assertEqual(run("--event-type", "room.mention"), "none")
        self.assertEqual(run("--event-type", "room.joined", "--playbook", "../../etc"),
                         str(REPO / "skills" / "room-onboard" / "SKILL.md"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
