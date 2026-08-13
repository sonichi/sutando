#!/usr/bin/env python3
"""A skill declares its own hook; core discovers, never names one."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from skill_hooks import discover


class SkillHookDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.repo = Path(self._td.name)

    def _skill(self, name, manifest, hook_body="#!/usr/bin/env python3\n"):
        d = self.repo / "skills" / name
        (d / "hooks").mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps(manifest))
        if hook_body is not None:
            (d / "hooks" / "g.py").write_text(hook_body)
        return d

    def test_a_declared_present_hook_is_discovered(self):
        self._skill("demo", {"name": "demo", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]})
        rows = discover(self.repo)
        self.assertEqual(len(rows), 1)
        event, token, cmd = rows[0]
        self.assertEqual(event, "PreToolUse")
        self.assertEqual(token, "g.py")
        self.assertTrue(cmd.startswith("python3 "), cmd)
        self.assertIn("skills/demo/hooks/g.py", cmd)

    def test_the_command_is_absolute_so_it_is_portable(self):
        """The whole point: no host-specific path is written by hand."""
        self._skill("demo", {"name": "demo", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]})
        cmd = discover(self.repo)[0][2]
        self.assertIn(str(self.repo), cmd)

    def test_a_skill_with_no_hooks_key_contributes_nothing(self):
        self._skill("demo", {"name": "demo", "tools": "./tools.ts"})
        self.assertEqual(discover(self.repo), [])

    def test_a_disabled_skill_is_skipped(self):
        self._skill("demo", {"name": "demo", "enabled": False, "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]})
        self.assertEqual(discover(self.repo), [])

    def test_a_declared_but_ABSENT_hook_is_not_registered(self):
        """Registering a path that does not exist arms nothing and reads as armed."""
        self._skill("demo", {"name": "demo", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/missing.py"}]})
        self.assertEqual(discover(self.repo), [])

    def test_a_broken_manifest_does_not_abort_discovery_of_the_others(self):
        self._skill("broken", {"name": "broken"})
        (self.repo / "skills" / "broken" / "manifest.json").write_text("{not json")
        self._skill("good", {"name": "good", "hooks": [
            {"event": "PreToolUse", "command": "./hooks/g.py"}]})
        self.assertEqual([r[1] for r in discover(self.repo)], ["g.py"])

    def test_malformed_hook_entries_are_skipped_not_raised(self):
        self._skill("demo", {"name": "demo", "hooks": [
            "a string", {"event": "PreToolUse"}, {"command": "./hooks/g.py"},
            {"event": "PreToolUse", "command": "./hooks/g.py"}]})
        self.assertEqual(len(discover(self.repo)), 1)

    def test_hooks_as_a_non_list_is_skipped(self):
        self._skill("demo", {"name": "demo", "hooks": {"event": "PreToolUse"}})
        self.assertEqual(discover(self.repo), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
