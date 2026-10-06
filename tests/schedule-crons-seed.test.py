#!/usr/bin/env python3
"""The per-host crons.json seed: source precedence, no overwrite, atomic publish."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/schedule-crons/scripts/seed_crons.py"
spec = importlib.util.spec_from_file_location("seed_crons", SCRIPT)
seed_crons = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_crons)


class SeedCronsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.skill = root / "skill"
        self.skill.mkdir()
        (self.skill / "crons.example.json").write_text('[{"name": "from-example"}]')
        self.target = self.ws / "hosts" / "h1" / "crons.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        return seed_crons.seed(self.ws, "h1", self.skill)

    def _names(self):
        return [e["name"] for e in json.loads(self.target.read_text())]

    def test_example_is_the_last_resort(self):
        status, target, source = self._seed()
        self.assertEqual((status, target, source),
                         ("seeded", self.target, self.skill / "crons.example.json"))
        self.assertEqual(self._names(), ["from-example"])

    def test_legacy_skill_file_beats_example(self):
        (self.skill / "crons.json").write_text('[{"name": "from-legacy"}]')
        self._seed()
        self.assertEqual(self._names(), ["from-legacy"])

    def test_interim_workspace_file_beats_legacy_and_example(self):
        (self.skill / "crons.json").write_text('[{"name": "from-legacy"}]')
        interim = self.ws / "crons" / "h1.json"
        interim.parent.mkdir(parents=True)
        interim.write_text('[{"name": "from-interim"}]')
        self._seed()
        self.assertEqual(self._names(), ["from-interim"])

    def test_interim_file_of_another_host_is_ignored(self):
        other = self.ws / "crons" / "h2.json"
        other.parent.mkdir(parents=True)
        other.write_text('[{"name": "other-host"}]')
        self._seed()
        self.assertEqual(self._names(), ["from-example"])

    def test_existing_file_is_untouched(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('[{"name": "owner"}]')
        os.utime(self.target, (1_000_000, 1_000_000))
        status, _, source = self._seed()
        self.assertEqual((status, source), ("exists", None))
        self.assertEqual(self._names(), ["owner"])
        self.assertEqual(self.target.stat().st_mtime, 1_000_000)

    def test_a_file_that_appears_mid_seed_is_not_clobbered(self):
        real_link = os.link

        def racing_link(src, dst):
            Path(dst).write_text('[{"name": "racer"}]')
            return real_link(src, dst)

        seed_crons.os.link = racing_link
        try:
            status, _, _ = self._seed()
        finally:
            seed_crons.os.link = real_link
        self.assertEqual(status, "exists")
        self.assertEqual(self._names(), ["racer"])

    def test_no_temp_file_is_left_behind(self):
        self._seed()
        self.assertEqual(sorted(p.name for p in self.target.parent.iterdir()), ["crons.json"])

    def test_shipped_example_carries_the_main_loop(self):
        status, _, _ = seed_crons.seed(self.ws, "h1")
        self.assertEqual(status, "seeded")
        main = [e for e in json.loads(self.target.read_text()) if e.get("name") == "main-loop"]
        self.assertEqual(main[0]["prompt_skill"], "proactive-loop")

    def test_cli_seeds_and_then_reports_exists(self):
        argv = [sys.executable, str(SCRIPT), "--workspace", str(self.ws), "--host-label", "h1"]
        first = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(first.stdout.startswith("seeded "), first.stdout)
        before = self.target.read_bytes()
        second = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(second.stdout.startswith("exists "), second.stdout)
        self.assertEqual(self.target.read_bytes(), before)

    def test_cli_refuses_a_blank_host_label_it_cannot_resolve(self):
        with self.assertRaises(ValueError):
            seed_crons.seed(self.ws, "  ", self.skill)
        self.assertFalse((self.ws / "hosts").exists())


def morning_catchup_due(entries, now):
    """The rule of ag2-space/ag2space-cinny-desktop#741's morningCatchupDue, restated
    in Python: due only if a morning-briefing entry exists and its slot has passed
    today. A fresh install that is due re-opens #5157 (a wake task ahead of the
    owner's first message)."""
    for e in entries:
        if e.get("name") != "morning-briefing":
            continue
        minute, hour = (int(f) for f in e["cron"].split()[:2])
        if (now.hour, now.minute) >= (hour, minute):
            return True
    return False


class FirstInstallOnlyTests(unittest.TestCase):
    """Codex seeds only the main loop on a first install; established schedules stay whole."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.skill = root / "skill"
        self.skill.mkdir()
        shutil.copy(REPO / "skills/schedule-crons/crons.example.json", self.skill / "crons.example.json")
        self.target = self.ws / "hosts" / "h1" / "crons.json"
        self.afternoon = datetime(2026, 10, 6, 13, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        return seed_crons.seed(self.ws, "h1", self.skill, first_install_only=("main-loop",))

    def _entries(self):
        return json.loads(self.target.read_text())

    def test_first_install_from_the_example_seeds_main_loop_only(self):
        status, _, source = self._seed()
        self.assertEqual((status, source), ("seeded", self.skill / "crons.example.json"))
        names = [e["name"] for e in self._entries()]
        self.assertEqual(names, ["main-loop"])
        self.assertNotIn("morning-briefing", names)
        self.assertNotIn("example-digest", names)
        self.assertEqual(self._entries()[0]["prompt_skill"], "proactive-loop")

    def test_init_generated_legacy_copy_of_the_example_is_a_first_install(self):
        shutil.copy(self.skill / "crons.example.json", self.skill / "crons.json")
        _, _, source = self._seed()
        self.assertEqual(source, self.skill / "crons.json")
        self.assertEqual([e["name"] for e in self._entries()], ["main-loop"])

    def test_legacy_equal_to_the_example_as_json_but_not_bytes_is_a_first_install(self):
        example = json.loads((self.skill / "crons.example.json").read_text())
        (self.skill / "crons.json").write_text(json.dumps(example))
        self._seed()
        self.assertEqual([e["name"] for e in self._entries()], ["main-loop"])

    def test_interim_copy_of_the_example_is_a_first_install(self):
        interim = self.ws / "crons" / "h1.json"
        interim.parent.mkdir(parents=True)
        shutil.copy(self.skill / "crons.example.json", interim)
        self._seed()
        self.assertEqual([e["name"] for e in self._entries()], ["main-loop"])

    def test_customized_legacy_is_copied_whole_with_the_filter(self):
        legacy = [{"name": "main-loop", "cron": "*/15 * * * *", "prompt_skill": "proactive-loop"},
                  {"name": "morning-briefing", "cron": "57 6 * * *", "prompt": "brief"},
                  {"name": "owner-job", "cron": "0 9 * * *", "prompt": "mine"}]
        (self.skill / "crons.json").write_text(json.dumps(legacy))
        before = (self.skill / "crons.json").read_bytes()
        self._seed()
        self.assertEqual(self.target.read_bytes(), before)
        self.assertTrue(morning_catchup_due(self._entries(), self.afternoon))

    def test_customized_interim_is_copied_whole_with_the_filter(self):
        interim = self.ws / "crons" / "h1.json"
        interim.parent.mkdir(parents=True)
        interim.write_text('[{"name": "owner-only", "cron": "0 9 * * *", "prompt": "keep"}]')
        self._seed()
        self.assertEqual([e["name"] for e in self._entries()], ["owner-only"])

    def test_existing_file_is_untouched_with_the_filter(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text('[{"name": "morning-briefing", "cron": "57 6 * * *"}]')
        before = self.target.read_bytes()
        self.assertEqual(self._seed()[0], "exists")
        self.assertEqual(self._seed()[0], "exists")
        self.assertEqual(self.target.read_bytes(), before)

    def test_fresh_seed_does_not_make_the_desktop_morning_catchup_due(self):
        # ag2-space/ag2space-cinny-desktop#741 + #5157: a fresh Codex install at 13:00
        # must not queue task-wake-briefing ahead of the owner's first message.
        self._seed()
        self.assertFalse(morning_catchup_due(self._entries(), self.afternoon))
        shutil.copy(self.skill / "crons.example.json", self.skill / "crons.json")
        self.target.unlink()
        self._seed()
        self.assertFalse(morning_catchup_due(self._entries(), self.afternoon))

    def test_unfiltered_seed_is_unchanged_for_the_claude_path(self):
        seed_crons.seed(self.ws, "h1", self.skill)
        self.assertEqual(self.target.read_bytes(), (self.skill / "crons.example.json").read_bytes())

    @unittest.skipIf((REPO / "skills/schedule-crons/crons.json").exists(),
                     "a legacy crons.json in this checkout would be the CLI's source")
    def test_cli_flag_filters_a_first_install(self):
        argv = [sys.executable, str(SCRIPT), "--workspace", str(self.ws), "--host-label", "h1",
                "--first-install-only", "main-loop"]
        result = subprocess.run(argv, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        # The CLI uses the shipped skill dir, whose example carries the full starter set.
        self.assertEqual([e["name"] for e in self._entries()], ["main-loop"])


if __name__ == "__main__":
    unittest.main()
