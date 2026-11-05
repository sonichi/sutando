#!/usr/bin/env python3
"""src/agent_triggers.py — the AgentTrigger registry and the playbook dispatch rule.

The registry maps each event type to its default playbook; resolve picks the
task's own playbook when it names a real skill, else the event's default, else
none (today's message handling). Only registry playbooks and skills whose
manifest sets `"playbook": true` may run; a playbook id never resolves outside skills/.

Run: python3 tests/agent-triggers.test.py
"""
import contextlib
import importlib.util
import io
import json
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

SRC_COPY = at


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The ag2-sparrow copy is a verbatim vendor of src/; the same suite runs against both.
VENDORED_COPY = _load("sparrow_agent_triggers",
                      REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "agent_triggers.py")
lint = _load("lint_skill_triggers", REPO / "scripts" / "lint-skill.py")


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
        manifests = {"room-onboard": None, "other-playbook": '{"name": "other-playbook", "playbook": true}',
                     "plain-skill": '{"name": "plain-skill"}',
                     "truthy-skill": '{"name": "truthy-skill", "playbook": "yes"}',
                     "broken-skill": '{"playbook": true'}
        for name, manifest in manifests.items():
            (self.skills / name).mkdir(parents=True)
            (self.skills / name / "SKILL.md").write_text(f"# {name}\n")
            if manifest is not None:
                (self.skills / name / "manifest.json").write_text(manifest)
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

    def test_only_allowed_playbooks_run(self):
        self.assertTrue(at.is_playbook(self.skills, "room-onboard"), "registry playbook")
        self.assertTrue(at.is_playbook(self.skills, "other-playbook"), "manifest opt-in")
        for name in ("plain-skill", "truthy-skill", "broken-skill", "no-such-skill", "../evil"):
            self.assertFalse(at.is_playbook(self.skills, name), name)

    def test_non_playbook_skill_is_refused_and_falls_back(self):
        want = self.skills / "room-onboard" / "SKILL.md"
        for name in ("plain-skill", "truthy-skill", "broken-skill"):
            self.assertEqual(self.resolve("room.joined", name), want, name)
            self.assertIsNone(self.resolve("", name), name)
            self.assertIsNone(self.resolve("room.mention", name), name)

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
        # An installed skill that is not a playbook cannot be steered into by the header.
        self.assertTrue((REPO / "skills" / "connect-apps" / "SKILL.md").is_file())
        self.assertEqual(run("--event-type", "room.joined", "--playbook", "connect-apps"),
                         str(REPO / "skills" / "room-onboard" / "SKILL.md"))
        self.assertEqual(run("--playbook", "connect-apps"), "none")
        self.assertEqual(Path(run("--playbook", "room-onboard")),
                         REPO / "skills" / "room-onboard" / "SKILL.md")

    def test_cli_in_process(self):
        def run(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(at._main(["resolve", "--skills-root", str(self.skills), *args]), 0)
            return out.getvalue().strip()
        self.assertEqual(run("--event-type", "room.joined"), str(self.skills / "room-onboard" / "SKILL.md"))
        self.assertEqual(run("--playbook", "plain-skill"), "none")

    def test_lint_refuses_a_non_boolean_playbook_flag(self):
        for value, ok in ((True, True), (False, True), ("yes", False), (1, False)):
            d = self.skills / f"lint-{len(str(value))}-{ok}"
            d.mkdir()
            (d / "manifest.json").write_text(json.dumps(
                {"name": d.name, "version": "0.1.0", "owner": "github:x/y", "stability": "experimental",
                 "playbook": value}))
            errors, _warnings = lint._lint_manifest(d)
            self.assertEqual(not any("playbook must be a boolean" in e for e in errors), ok, (value, errors))

    def test_shipped_room_onboard_opts_in(self):
        manifest = json.loads((REPO / "skills" / "room-onboard" / "manifest.json").read_text())
        self.assertIs(manifest.get("playbook"), True)


if __name__ == "__main__":
    ok = True
    for copy in (SRC_COPY, VENDORED_COPY):
        at = copy
        print(f"== {copy.__file__}")
        ok &= unittest.main(verbosity=1, exit=False).result.wasSuccessful()
    sys.exit(0 if ok else 1)
