#!/usr/bin/env python3
"""The per-host crons.json seed: source precedence, no overwrite, atomic publish."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
