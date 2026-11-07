#!/usr/bin/env python3
"""The per-host crons.json seed: source precedence, no overwrite, crash-consistent publish."""
import contextlib
import signal
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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

    def test_unmarked_legacy_copy_of_the_current_example_is_copied_whole(self):
        shutil.copy(self.skill / "crons.example.json", self.skill / "crons.json")
        self._seed()
        self.assertEqual(self.target.read_bytes(), (self.skill / "crons.example.json").read_bytes())

    def test_unmarked_interim_copy_of_the_example_is_copied_whole(self):
        interim = self.ws / "crons" / "h1.json"
        interim.parent.mkdir(parents=True)
        shutil.copy(self.skill / "crons.example.json", interim)
        self._seed()
        self.assertEqual(self.target.read_bytes(), interim.read_bytes())

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


class PolarityTests(unittest.TestCase):
    """The whole rule: canonical -> no-op; legacy/interim -> whole; nothing -> main-loop from the example."""

    V01_DEMO = REPO / "tests/fixtures/schedule-crons/crons.example.2b45993b.json"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.skill = root / "skill"
        self.skill.mkdir()
        shutil.copy(REPO / "skills/schedule-crons/crons.example.json", self.skill / "crons.example.json")
        self.legacy = self.skill / "crons.json"
        self.target = self.ws / "hosts" / "h1" / "crons.json"
        self.afternoon = datetime(2026, 10, 6, 13, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, first_install_only=("main-loop",)):
        return seed_crons.seed(self.ws, "h1", self.skill, first_install_only=first_install_only)

    def _names(self, path=None):
        return [e["name"] for e in json.loads((path or self.target).read_text())]

    def test_fresh_install_is_main_loop_only(self):
        status, _, source = self._seed()
        self.assertEqual((status, source), ("seeded", self.skill / "crons.example.json"))
        self.assertEqual(self._names(), ["main-loop"])
        self.assertFalse(morning_catchup_due(json.loads(self.target.read_text()), self.afternoon))

    def test_legacy_copy_of_the_current_example_is_preserved_whole(self):
        # An older init.sh made exactly this copy, and older docs registered every entry from it.
        shutil.copy(self.skill / "crons.example.json", self.legacy)
        status, _, source = self._seed()
        self.assertEqual((status, source), ("seeded", self.legacy))
        self.assertEqual(self.target.read_bytes(), self.legacy.read_bytes())
        self.assertEqual(len(self._names()), len(self._names(self.legacy)))
        self.assertGreater(len(self._names()), 1)

    def test_unmarked_v0_1_demo_legacy_is_preserved_whole(self):
        shutil.copy(self.V01_DEMO, self.legacy)
        status, _, source = self._seed()
        self.assertEqual((status, source), ("seeded", self.legacy))
        self.assertEqual(self._names(), ["main-loop", "morning-briefing", "daily-insight",
                                         "pending-questions", "sync-memory"])
        self.assertEqual(self.target.read_bytes(), self.V01_DEMO.read_bytes())

    def test_unmarked_interim_v0_1_demo_is_preserved_whole(self):
        interim = self.ws / "crons" / "h1.json"
        interim.parent.mkdir(parents=True)
        shutil.copy(self.V01_DEMO, interim)
        self._seed()
        self.assertEqual(self.target.read_bytes(), self.V01_DEMO.read_bytes())

    def test_existing_canonical_is_untouched_on_both_runtimes(self):
        shutil.copy(self.skill / "crons.example.json", self.legacy)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_text('[{"name": "owner-only", "cron": "0 9 * * *"}]')
        os.utime(self.target, (1_000_000, 1_000_000))
        before = self.target.read_bytes()
        self.assertEqual(self._seed(), ("exists", self.target, None))
        self.assertEqual(self._seed(None), ("exists", self.target, None))
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.target.stat().st_mtime, 1_000_000)

    def test_a_reseed_after_deleting_canonical_republishes_the_legacy_file_whole(self):
        shutil.copy(self.skill / "crons.example.json", self.legacy)
        self._seed(None)  # Claude: every entry gets registered
        self.target.unlink()
        self._seed()
        self.assertEqual(self.target.read_bytes(), self.legacy.read_bytes())

    def test_source_is_read_once_and_the_read_bytes_are_published(self):
        shutil.copy(self.skill / "crons.example.json", self.legacy)
        real_read = Path.read_bytes
        reads = []

        def read_bytes(path):
            data = real_read(path)
            if path == self.legacy:
                reads.append(path)
                # A concurrent replacement after the read must not change what is published.
                path.write_text('[{"name": "owner-only"}]')
            return data

        expected = self.legacy.read_bytes()
        with mock.patch.object(Path, "read_bytes", read_bytes):
            self._seed()
        self.assertEqual(reads, [self.legacy])
        self.assertEqual(self.target.read_bytes(), expected)

    def test_no_installer_state_is_consulted_or_written(self):
        self._seed()
        written = sorted(str(p.relative_to(self.ws)) for p in self.ws.rglob("*"))
        self.assertEqual(written, ["hosts", "hosts/h1", "hosts/h1/crons.json"])


# The child pauses at one publish boundary, says so, and waits to be terminated.
_CRASH_CHILD = r"""
import importlib.util, os, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("seed_crons", sys.argv[1])
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
ws, skill, point, ready = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], Path(sys.argv[5])

def pause():
    ready.write_text(point)
    time.sleep(60)

real_link, real_write = mod.os.link, Path.write_bytes
if point == "before-publish":
    real_publish = mod._publish
    def publish(target, content):
        pause(); return real_publish(target, content)
    mod._publish = publish
elif point == "mid-temp-write":
    def write_bytes(path, data):
        with open(path, "wb") as f:
            f.write(data[: len(data) // 2]); f.flush()
        pause()
    Path.write_bytes = write_bytes
elif point == "before-link":
    def link(src, dst):
        pause(); return real_link(src, dst)
    mod.os.link = link
elif point == "after-link":
    def link(src, dst):
        real_link(src, dst); pause()
    mod.os.link = link
mod.seed(ws, "h1", skill, first_install_only=("main-loop",))
"""


class CrashConsistencyTests(unittest.TestCase):
    """SIGTERM at every publish boundary leaves no canonical file or a complete one; a retry completes it."""

    POINTS = ("before-publish", "mid-temp-write", "before-link", "after-link")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.skill = root / "skill"
        self.skill.mkdir()
        shutil.copy(REPO / "skills/schedule-crons/crons.example.json", self.skill / "crons.example.json")
        self.target = self.ws / "hosts" / "h1" / "crons.json"
        self.ready = root / "ready"

    def tearDown(self):
        self.tmp.cleanup()

    def _terminate_at(self, point):
        self.ready.unlink(missing_ok=True)
        child = subprocess.Popen([sys.executable, "-c", _CRASH_CHILD, str(SCRIPT), str(self.ws),
                                  str(self.skill), point, str(self.ready)])
        deadline = time.monotonic() + 30
        while not self.ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.ready.exists(), f"child never reached {point}")
        child.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(30), -signal.SIGTERM)

    def _assert_nothing_or_complete(self, expected, point):
        if self.target.exists():
            self.assertEqual(self.target.read_bytes(), expected, point)
        retry = seed_crons.seed(self.ws, "h1", self.skill, first_install_only=("main-loop",))
        self.assertIn(retry[0], ("seeded", "exists"), point)
        self.assertEqual(self.target.read_bytes(), expected, point)
        return retry[0]

    def _fresh_expected(self):
        entries = json.loads((self.skill / "crons.example.json").read_text())
        return (json.dumps([e for e in entries if e["name"] == "main-loop"], indent=2) + "\n").encode()

    def test_fresh_install_terminated_at_each_boundary(self):
        outcomes = {}
        for point in self.POINTS:
            with self.subTest(point=point):
                shutil.rmtree(self.ws, ignore_errors=True)
                self._terminate_at(point)
                existed = self.target.exists()
                retry = self._assert_nothing_or_complete(self._fresh_expected(), point)
                outcomes[point] = (existed, retry)
                self.assertEqual([e["name"] for e in json.loads(self.target.read_text())], ["main-loop"])
        # The sweep reaches both sides of the one durable step: before it nothing, after it the file.
        self.assertEqual(outcomes, {"before-publish": (False, "seeded"), "mid-temp-write": (False, "seeded"),
                                    "before-link": (False, "seeded"), "after-link": (True, "exists")})

    def test_legacy_source_terminated_at_each_boundary_is_never_filtered(self):
        legacy = self.skill / "crons.json"
        shutil.copy(self.skill / "crons.example.json", legacy)
        for point in self.POINTS:
            with self.subTest(point=point):
                shutil.rmtree(self.ws, ignore_errors=True)
                self._terminate_at(point)
                self._assert_nothing_or_complete(legacy.read_bytes(), point)
                self.assertGreater(len(json.loads(self.target.read_text())), 1)

    def test_a_crash_leftover_temp_is_never_read_as_the_schedule(self):
        self._terminate_at("before-link")
        leftovers = [p.name for p in self.target.parent.iterdir()]
        self.assertEqual(len(leftovers), 1)
        self.assertRegex(leftovers[0], r"^\.crons\.json\.\d+\.tmp$")
        self.assertEqual(seed_crons.seed(self.ws, "h1", self.skill)[0], "seeded")


class ConcurrentPublisherTests(unittest.TestCase):
    """Concurrent seeders never clobber: exactly one publishes, the rest see a valid winner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.skill = root / "skill"
        self.skill.mkdir()
        shutil.copy(REPO / "skills/schedule-crons/crons.example.json", self.skill / "crons.example.json")
        self.target = self.ws / "hosts" / "h1" / "crons.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _race(self, n, *extra):
        argv = [sys.executable, str(SCRIPT), "--skill-dir", str(self.skill), "--workspace", str(self.ws),
                "--host-label", "h1", *extra]
        procs = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(n)]
        results = [(p.wait(60), *p.communicate()) for p in procs]
        for rc, _, err in results:
            self.assertEqual(rc, 0, err)
        return [out.split()[0] for _, out, _ in results]

    def test_concurrent_codex_seeders_publish_one_main_loop_file(self):
        statuses = self._race(8, "--first-install-only", "main-loop")
        self.assertEqual(sorted(statuses), ["exists"] * 7 + ["seeded"])
        self.assertEqual([e["name"] for e in json.loads(self.target.read_text())], ["main-loop"])
        self.assertEqual(sorted(p.name for p in self.target.parent.iterdir()), ["crons.json"])

    def test_mixed_runtimes_racing_leave_one_valid_winner(self):
        argv = [sys.executable, str(SCRIPT), "--skill-dir", str(self.skill), "--workspace", str(self.ws),
                "--host-label", "h1"]
        procs = [subprocess.Popen(argv + (["--first-install-only", "main-loop"] if i % 2 else []),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(8)]
        outs = [p.communicate(timeout=60)[0] for p in procs]
        self.assertEqual(sum(o.startswith("seeded") for o in outs), 1)
        names = [e["name"] for e in json.loads(self.target.read_text())]
        full = [e["name"] for e in json.loads((self.skill / "crons.example.json").read_text())]
        self.assertIn(names, (["main-loop"], full))


def _strict_env(home, **extra):
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
    env.update(extra)
    return env


class FreshInstallEndToEndTests(unittest.TestCase):
    """#5157 at today's code: init.sh writes no legacy copy, so the Codex seed takes main-loop only."""

    def _repo(self, root):
        repo = root / "repo"
        skill = repo / "skills/schedule-crons"
        skill.mkdir(parents=True)
        shutil.copy(REPO / "skills/schedule-crons/crons.example.json", skill / "crons.example.json")
        ws = root / "ws"
        env = _strict_env(root, SUTANDO_REPO=str(repo), SUTANDO_WORKSPACE=str(ws),
                          SUTANDO_TEST_MODE="1", SUTANDO_PY=sys.executable, SUTANDO_HOST_LABEL="h1",
                          CLAUDE_CONFIG_DIR=str(root / ".claude"))
        return skill, ws, env

    def _init_then_codex_seed(self, skill, ws, env):
        init = subprocess.run(["bash", str(REPO / "src/init.sh"), "--auto"],
                              capture_output=True, text=True, env=env)
        self.assertEqual(init.returncode, 0, init.stderr)
        seed = subprocess.run(
            [sys.executable, str(SCRIPT), "--skill-dir", str(skill), "--workspace", str(ws),
             "--host-label", "h1", "--first-install-only", "main-loop"],
            capture_output=True, text=True, env=env)
        self.assertEqual(seed.returncode, 0, seed.stderr)
        return [e["name"] for e in json.loads((ws / "hosts/h1/crons.json").read_text())]

    def test_init_then_codex_seed_is_main_loop_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill, ws, env = self._repo(Path(tmp))
            self.assertEqual(self._init_then_codex_seed(skill, ws, env), ["main-loop"])
            self.assertFalse((skill / "crons.json").exists())

    def test_init_then_codex_seed_keeps_an_upgraded_hosts_legacy_file_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill, ws, env = self._repo(Path(tmp))
            shutil.copy(skill / "crons.example.json", skill / "crons.json")  # an older init.sh's copy
            names = self._init_then_codex_seed(skill, ws, env)
            self.assertEqual(names, [e["name"] for e in json.loads((skill / "crons.json").read_text())])
            self.assertGreater(len(names), 1)


class DocumentedSkillCommandTests(unittest.TestCase):
    """Runs the seed command exactly as SKILL.md documents it, with a developer-tools stub first on PATH."""

    SKILL_MD = REPO / "skills/schedule-crons/SKILL.md"

    @staticmethod
    def documented_seed_command():
        text = DocumentedSkillCommandTests.SKILL_MD.read_text()
        blocks = re.findall(r"```bash\n(.*?)```", text, re.S)
        seeds = [b for b in blocks if "seed_crons.py" in b]
        if len(seeds) != 1:
            raise AssertionError(f"expected one documented seed command, found {len(seeds)}")
        return seeds[0]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ws = root / "ws"
        self.target = self.ws / "hosts/h1/crons.json"
        stubs = root / "stub-bin"
        stubs.mkdir()
        stub = stubs / "python3"
        stub.write_text("#!/bin/sh\necho developer-tools-stub >&2\nexit 71\n")
        stub.chmod(0o755)
        self.env = _strict_env(root, PATH=f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                               SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=str(self.ws),
                               SUTANDO_HOST_LABEL="h1")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, command, **extra):
        return subprocess.run(["bash", "-c", command], cwd=REPO, capture_output=True, text=True,
                              env={**self.env, **extra})

    def test_the_documented_command_is_the_resolver_routed_seeder(self):
        cmd = self.documented_seed_command()
        self.assertIn("scripts/python-binary.sh", cmd)
        self.assertNotRegex(cmd, r"(^|[\s;])python3 skills/")
        self.assertNotIn("cp ", cmd)

    @unittest.skipIf((REPO / "skills/schedule-crons/crons.json").exists(),
                     "a legacy crons.json in this checkout would be the command's source")
    def test_documented_command_beats_a_path_stub_with_a_valid_sutando_py(self):
        result = self._run(self.documented_seed_command(), SUTANDO_PY=sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("developer-tools-stub", result.stderr)
        self.assertTrue(result.stdout.startswith(f"seeded {self.target.resolve()}"), result.stdout)
        self.assertEqual(self.target.read_bytes(),
                         (REPO / "skills/schedule-crons/crons.example.json").read_bytes())

    def test_control_the_bare_form_hits_the_stub(self):
        # Proves the stub is live: the pre-fix bare `python3` invocation fails under the same env.
        result = self._run("python3 skills/schedule-crons/scripts/seed_crons.py", SUTANDO_PY=sys.executable)
        self.assertEqual(result.returncode, 71)
        self.assertIn("developer-tools-stub", result.stderr)
        self.assertFalse(self.target.exists())

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

    def test_unreadable_starter_does_not_affect_a_legacy_file(self):
        # A legacy file is never classified, so the example is not even parsed.
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
        real_config = seed_crons._config

        def fake_config(key):
            self.config_calls.append(key)
            value = self.config[key]
            if isinstance(value, Exception):
                raise value
            return value

        seed_crons._config = fake_config
        self.addCleanup(setattr, seed_crons, "_config", real_config)

    def tearDown(self):
        self.tmp.cleanup()

    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        argv = ("--skill-dir", str(self.skill), *argv)
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
