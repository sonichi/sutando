#!/usr/bin/env python3
"""The per-host crons.json seed: source precedence, no overwrite, atomic publish."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

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



class HistoricalStarterTests(unittest.TestCase):
    """init.sh never refreshes the legacy copy, so an older release's untouched starter
    must still read as a starter; anything an owner changed must read as established."""

    FIXTURES = REPO / "tests/fixtures/schedule-crons"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.skill = root / "skill"
        self.skill.mkdir()
        for name in ("crons.example.json", seed_crons.STARTERS_FILE):
            shutil.copy(REPO / "skills/schedule-crons" / name, self.skill / name)
        self.target = self.ws / "hosts" / "h1" / "crons.json"
        self.afternoon = datetime(2026, 10, 6, 13, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        return seed_crons.seed(self.ws, "h1", self.skill, first_install_only=("main-loop",))

    def _names(self):
        return [e["name"] for e in json.loads(self.target.read_text())]

    def _legacy(self, entries):
        (self.skill / "crons.json").write_text(json.dumps(entries, indent=2))

    def _fixture(self, name):
        return json.loads((self.FIXTURES / name).read_text())

    def test_current_example_is_pinned(self):
        current = json.loads((REPO / "skills/schedule-crons/crons.example.json").read_text())
        self.assertIn(seed_crons.starter_digest(current), seed_crons.shipped_starter_digests(),
                      "crons.example.json changed: append its starter_digest() to "
                      "skills/schedule-crons/shipped-starters.json so older installs stay classified")

    def test_fixture_versions_are_pinned(self):
        pinned = seed_crons.shipped_starter_digests()
        for name in ("crons.example.v0.12.0.json", "crons.example.2b45993b.json"):
            self.assertIn(seed_crons.starter_digest(self._fixture(name)), pinned, name)

    def test_untouched_v0_12_0_legacy_starter_is_a_first_install(self):
        old = self._fixture("crons.example.v0.12.0.json")
        self.assertNotEqual(old, json.loads((self.skill / "crons.example.json").read_text()))
        self.assertIn("morning-briefing", [e["name"] for e in old])
        shutil.copy(self.FIXTURES / "crons.example.v0.12.0.json", self.skill / "crons.json")
        _, _, source = self._seed()
        self.assertEqual(source, self.skill / "crons.json")
        self.assertEqual(self._names(), ["main-loop"])
        self.assertFalse(morning_catchup_due(json.loads(self.target.read_text()), self.afternoon))

    def test_untouched_first_shipped_starter_is_a_first_install(self):
        shutil.copy(self.FIXTURES / "crons.example.2b45993b.json", self.skill / "crons.json")
        self._seed()
        self.assertEqual(self._names(), ["main-loop"])

    def test_interim_copy_of_an_older_starter_is_a_first_install(self):
        interim = self.ws / "crons" / "h1.json"
        interim.parent.mkdir(parents=True)
        shutil.copy(self.FIXTURES / "crons.example.v0.12.0.json", interim)
        self._seed()
        self.assertEqual(self._names(), ["main-loop"])

    def test_older_starter_reformatted_is_still_a_starter(self):
        old = self._fixture("crons.example.v0.12.0.json")
        (self.skill / "crons.json").write_text(json.dumps([dict(reversed(e.items())) for e in old]))
        self._seed()
        self.assertEqual(self._names(), ["main-loop"])

    def test_established_schedule_built_on_the_current_template_is_copied_whole(self):
        entries = json.loads((self.skill / "crons.example.json").read_text())
        for e in entries:
            if e["name"] == "morning-briefing":
                e["cron"] = "30 7 * * *"
        self._legacy(entries)
        before = (self.skill / "crons.json").read_bytes()
        self._seed()
        self.assertEqual(self.target.read_bytes(), before)
        self.assertTrue(morning_catchup_due(json.loads(self.target.read_text()), self.afternoon))

    def test_established_schedule_built_on_an_older_starter_is_copied_whole(self):
        entries = self._fixture("crons.example.v0.12.0.json")
        entries.append({"name": "owner-job", "cron": "0 9 * * *", "prompt": "mine"})
        self._legacy(entries)
        self._seed()
        self.assertIn("owner-job", self._names())
        self.assertIn("morning-briefing", self._names())

    def test_without_the_pin_file_an_older_starter_reads_as_established(self):
        (self.skill / seed_crons.STARTERS_FILE).unlink()
        shutil.copy(self.FIXTURES / "crons.example.v0.12.0.json", self.skill / "crons.json")
        self._seed()
        self.assertIn("morning-briefing", self._names())

    def test_malformed_pin_file_is_refused_under_the_filter(self):
        shutil.copy(self.FIXTURES / "crons.example.v0.12.0.json", self.skill / "crons.json")
        for bad in ("{not json", '{"sha256": "x"}', '[{"digest": "x"}]', '["x"]'):
            (self.skill / seed_crons.STARTERS_FILE).write_text(bad)
            with self.assertRaises(ValueError, msg=bad):
                self._seed()
            self.assertFalse(self.target.exists())

    def test_pin_file_is_not_read_without_the_filter(self):
        (self.skill / seed_crons.STARTERS_FILE).write_text("{not json")
        shutil.copy(self.FIXTURES / "crons.example.v0.12.0.json", self.skill / "crons.json")
        self.assertEqual(seed_crons.seed(self.ws, "h1", self.skill)[0], "seeded")
        self.assertEqual(self.target.read_bytes(),
                         (self.FIXTURES / "crons.example.v0.12.0.json").read_bytes())

    def test_source_is_read_once_and_classified_on_the_published_bytes(self):
        legacy = self.skill / "crons.json"
        shutil.copy(self.FIXTURES / "crons.example.v0.12.0.json", legacy)
        real_read_bytes, real_read_text = Path.read_bytes, Path.read_text
        reads = []

        def read_bytes(path):
            data = real_read_bytes(path)
            if path == legacy:
                reads.append("bytes")
                # A concurrent replacement after the read must not change the outcome.
                legacy.write_text('[{"name": "owner-only"}]')
            return data

        def read_text(path, *a, **kw):
            if path == legacy:
                reads.append("text")
            return real_read_text(path, *a, **kw)

        with mock.patch.object(Path, "read_bytes", read_bytes), \
                mock.patch.object(Path, "read_text", read_text):
            self._seed()
        self.assertEqual(reads, ["bytes"])
        self.assertEqual(self._names(), ["main-loop"])


class SeedErrorPathTests(unittest.TestCase):
    """The refusals: no source, a starter that is not JSON, a starter that is not a list."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.skill = root / "skill"
        self.skill.mkdir()
        self.target = self.ws / "hosts" / "h1" / "crons.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_seed_source_raises_and_writes_nothing(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            seed_crons.seed(self.ws, "h1", self.skill)
        self.assertIn(str(self.skill), str(ctx.exception))
        self.assertFalse((self.ws / "hosts").exists())

    def test_invalid_json_example_is_refused_under_the_filter(self):
        (self.skill / "crons.example.json").write_text("{not json")
        with self.assertRaises(ValueError):
            seed_crons.seed(self.ws, "h1", self.skill, first_install_only=("main-loop",))
        self.assertFalse(self.target.exists())

    def test_invalid_json_example_is_copied_verbatim_without_the_filter(self):
        (self.skill / "crons.example.json").write_text("{not json")
        self.assertEqual(seed_crons.seed(self.ws, "h1", self.skill)[0], "seeded")
        self.assertEqual(self.target.read_text(), "{not json")

    def test_non_list_starter_is_refused_under_the_filter(self):
        (self.skill / "crons.example.json").write_text('{"name": "main-loop"}')
        with self.assertRaisesRegex(ValueError, "not a JSON list"):
            seed_crons.seed(self.ws, "h1", self.skill, first_install_only=("main-loop",))
        self.assertFalse(self.target.exists())

    def test_unreadable_starter_makes_a_legacy_file_established(self):
        # An example that does not parse cannot match, so the legacy file is copied whole.
        (self.skill / "crons.example.json").write_text("{not json")
        (self.skill / "crons.json").write_text('[{"name": "a"}, {"name": "main-loop"}]')
        seed_crons.seed(self.ws, "h1", self.skill, first_install_only=("main-loop",))
        self.assertEqual([e["name"] for e in json.loads(self.target.read_text())], ["a", "main-loop"])

    def test_invalid_json_legacy_is_copied_whole_under_the_filter(self):
        (self.skill / "crons.example.json").write_text('[{"name": "main-loop"}]')
        (self.skill / "crons.json").write_text("{broken")
        _, _, source = seed_crons.seed(self.ws, "h1", self.skill, first_install_only=("main-loop",))
        self.assertEqual(source, self.skill / "crons.json")
        self.assertEqual(self.target.read_text(), "{broken")

    def test_non_dict_entries_are_dropped_by_the_filter(self):
        (self.skill / "crons.example.json").write_text('["main-loop", {"name": "main-loop"}, {"name": "x"}]')
        seed_crons.seed(self.ws, "h1", self.skill, first_install_only=("main-loop",))
        self.assertEqual(json.loads(self.target.read_text()), [{"name": "main-loop"}])

    def test_seed_sources_order_is_interim_then_legacy_then_example(self):
        self.assertEqual(seed_crons.seed_sources(self.ws, "h1", self.skill), [
            self.ws / "crons" / "h1.json",
            self.skill / "crons.json",
            self.skill / "crons.example.json",
        ])


class MainInProcessTests(unittest.TestCase):
    """main() called in-process, with the seed pinned to a temp skill dir and config stubbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.skill = root / "skill"
        self.skill.mkdir()
        (self.skill / "crons.example.json").write_text(
            '[{"name": "main-loop"}, {"name": "morning-briefing"}, {"name": "other"}]')
        self.config = {"workspace": str(self.ws), "host-label": "h1"}
        self.config_calls = []
        real_seed = seed_crons.seed
        real_config = seed_crons._config

        def fake_config(key):
            self.config_calls.append(key)
            value = self.config[key]
            if isinstance(value, Exception):
                raise value
            return value

        seed_crons.seed = lambda *a, **kw: real_seed(*a, skill_dir=self.skill, **kw)
        seed_crons._config = fake_config
        self.addCleanup(setattr, seed_crons, "seed", real_seed)
        self.addCleanup(setattr, seed_crons, "_config", real_config)

    def tearDown(self):
        self.tmp.cleanup()

    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["seed_crons.py", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = seed_crons.main()
        return rc, out.getvalue(), err.getvalue()

    def _target(self):
        return self.ws.resolve() / "hosts" / "h1" / "crons.json"

    def _names(self):
        return [e["name"] for e in json.loads(self._target().read_text())]

    def test_explicit_flags_seed_without_consulting_config(self):
        rc, out, err = self._main("--workspace", str(self.ws), "--host-label", "h1")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, f"seeded {self._target()} from {self.skill / 'crons.example.json'}\n")
        self.assertEqual(self._names(), ["main-loop", "morning-briefing", "other"])
        self.assertEqual(self.config_calls, [])

    def test_second_run_reports_exists_without_a_source(self):
        self._main("--workspace", str(self.ws), "--host-label", "h1")
        rc, out, _ = self._main("--workspace", str(self.ws), "--host-label", "h1")
        self.assertEqual((rc, out), (0, f"exists {self._target()}\n"))

    def test_missing_flags_fall_back_to_config(self):
        rc, out, _ = self._main()
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith(f"seeded {self._target()}"), out)
        self.assertEqual(self.config_calls, ["workspace", "host-label"])

    def test_blank_flags_fall_back_to_config(self):
        rc, _, _ = self._main("--workspace", "  ", "--host-label", " ")
        self.assertEqual(rc, 0)
        self.assertEqual(self.config_calls, ["workspace", "host-label"])
        self.assertTrue(self._target().exists())

    def test_first_install_only_is_repeatable(self):
        rc, _, _ = self._main("--workspace", str(self.ws), "--host-label", "h1",
                              "--first-install-only", "main-loop",
                              "--first-install-only", "other")
        self.assertEqual(rc, 0)
        self.assertEqual(self._names(), ["main-loop", "other"])

    def test_unresolved_workspace_fails_with_a_message(self):
        self.config["workspace"] = ""
        rc, out, err = self._main("--host-label", "h1")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, "seed-crons: workspace did not resolve\n")
        self.assertFalse(self.ws.exists())

    def test_unresolved_host_label_fails_with_a_message(self):
        self.config["host-label"] = ""
        rc, _, err = self._main("--workspace", str(self.ws))
        self.assertEqual(rc, 1)
        self.assertEqual(err, "seed-crons: host label did not resolve\n")
        self.assertFalse((self.ws / "hosts").exists())

    def test_config_helper_failure_fails_with_a_message(self):
        self.config["workspace"] = subprocess.CalledProcessError(2, ["bash", "sutando-config.sh"])
        rc, _, err = self._main()
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("seed-crons: Command"), err)

    def test_missing_seed_source_fails_with_a_message(self):
        (self.skill / "crons.example.json").unlink()
        rc, out, err = self._main("--workspace", str(self.ws), "--host-label", "h1")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("no crons seed source", err)

    def test_non_list_starter_under_the_filter_fails_with_a_message(self):
        (self.skill / "crons.example.json").write_text('{"name": "main-loop"}')
        rc, _, err = self._main("--workspace", str(self.ws), "--host-label", "h1",
                                "--first-install-only", "main-loop")
        self.assertEqual(rc, 1)
        self.assertIn("not a JSON list", err)


class ConfigHelperTests(unittest.TestCase):
    def test_config_runs_the_repo_helper_and_strips_output(self):
        done = subprocess.CompletedProcess([], 0, stdout="/some/ws\n", stderr="")
        with mock.patch.object(seed_crons.subprocess, "run", return_value=done) as run:
            self.assertEqual(seed_crons._config("workspace"), "/some/ws")
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["bash", str(seed_crons.REPO / "scripts" / "sutando-config.sh"), "workspace"])
        self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
