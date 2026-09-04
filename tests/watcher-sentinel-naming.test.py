#!/usr/bin/env python3
"""The watcher sentinel is per instance, and the shell and Python namers agree.

The defect: every watcher on a host stamped `state/watch-tasks-stream.pid`, so
on a pool host the Nth watcher's write erased the (N-1)th and the four readers
tracked only whichever ran last. Measured on a live pool host: six watcher
processes, one record, four of them invisible to the startup reaper and to
health-check.

Two namers exist because two runtimes read the file, so the test that matters
is that they cannot drift.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import util_paths as up  # noqa: E402

SHELL = ROOT / "src" / "watcher_sentinel.sh"


def _sh(fn: str, *args: str, env: "dict | None" = None) -> str:
    e = {**os.environ, **(env or {})}
    e.pop("SUTANDO_INSTANCE", None) if env is None else None
    out = subprocess.run(
        ["bash", "-c", f'source "{SHELL}"; {fn} ' + " ".join(f'"{a}"' for a in args)],
        capture_output=True, text=True, env=e, check=True)
    return out.stdout


class NamingContract(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        os.environ.pop("SUTANDO_INSTANCE", None)

    def test_no_instance_keeps_the_historic_name(self):
        """A single-instance install must be byte-identical to before, or this
        change is a migration rather than an addition."""
        self.assertEqual(up.watcher_sentinel_path(self.d).name,
                         "watch-tasks-stream.pid")
        self.assertEqual(Path(_sh("sentinel_path_for", str(self.d)).strip()).name,
                         "watch-tasks-stream.pid")

    def test_an_instance_gets_its_own_file(self):
        self.assertEqual(up.watcher_sentinel_path(self.d, "worker-2").name,
                         "watch-tasks-stream-worker-2.pid")
        self.assertEqual(
            Path(_sh("sentinel_path_for", str(self.d), "worker-2").strip()).name,
            "watch-tasks-stream-worker-2.pid")

    def test_two_instances_do_not_collide(self):
        a = up.watcher_sentinel_path(self.d, "worker-1")
        b = up.watcher_sentinel_path(self.d, "worker-2")
        self.assertNotEqual(a, b)

    def test_the_env_default_is_read_by_both(self):
        os.environ["SUTANDO_INSTANCE"] = "core-2"
        try:
            self.assertEqual(up.watcher_sentinel_path(self.d).name,
                             "watch-tasks-stream-core-2.pid")
            self.assertEqual(
                Path(_sh("sentinel_path_for", str(self.d),
                         env={"SUTANDO_INSTANCE": "core-2"}).strip()).name,
                "watch-tasks-stream-core-2.pid")
        finally:
            os.environ.pop("SUTANDO_INSTANCE", None)

    def test_both_namers_agree_across_a_range_of_names(self):
        """The one assertion that survives either implementation being edited."""
        for inst in ["", "core", "worker-2", "a.b_c", "../evil x", "  spaced  ",
                     "UPPER", "-lead-", "...."]:
            with self.subTest(inst=inst):
                self.assertEqual(
                    up.watcher_sentinel_path(self.d, inst).name,
                    Path(_sh("sentinel_path_for", str(self.d), inst).strip()).name)

    def test_a_hostile_instance_name_cannot_escape_the_state_dir(self):
        p = up.watcher_sentinel_path(self.d, "../../etc/passwd")
        self.assertEqual(p.parent, self.d)
        self.assertNotIn("/", p.name)


class Enumeration(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        os.environ.pop("SUTANDO_INSTANCE", None)

    def _names(self):
        return [p.name for p in up.watcher_sentinel_paths(self.d)]

    def _sh_names(self):
        out = _sh("sentinel_paths_in", str(self.d))
        return [Path(x).name for x in out.split() if x]

    def test_empty_state_dir_yields_nothing(self):
        self.assertEqual(self._names(), [])
        self.assertEqual(self._sh_names(), [])

    def test_a_missing_state_dir_is_not_an_error(self):
        """The reaper runs before anything guarantees state/ exists."""
        gone = self.d / "nope"
        self.assertEqual(up.watcher_sentinel_paths(gone), [])

    def test_every_instance_sentinel_is_found(self):
        for n in ["watch-tasks-stream.pid", "watch-tasks-stream-worker-1.pid",
                  "watch-tasks-stream-worker-2.pid"]:
            (self.d / n).write_text("1\n")
        self.assertEqual(len(self._names()), 3)
        self.assertEqual(len(self._sh_names()), 3)
        self.assertEqual(self._names()[0], "watch-tasks-stream.pid")

    def test_the_historic_name_comes_first(self):
        """Readers that report one row keep reporting the same one as before."""
        (self.d / "watch-tasks-stream-aaa.pid").write_text("1\n")
        (self.d / "watch-tasks-stream.pid").write_text("2\n")
        self.assertEqual(self._names()[0], "watch-tasks-stream.pid")

    def test_unrelated_pid_files_are_not_swept_in(self):
        """The glob decides what the reaper may signal, so it must be narrow."""
        for n in ["watch-tasks-stream.pid.bak", "other.pid",
                  "watch-tasks-streamer.pid", "watch-tasks-stream-x.pid.tmp"]:
            (self.d / n).write_text("1\n")
        self.assertEqual(self._names(), [])
        self.assertEqual(self._sh_names(), [])

    def test_a_directory_named_like_a_sentinel_is_skipped(self):
        (self.d / "watch-tasks-stream-dir.pid").mkdir()
        self.assertEqual(self._names(), [])
        self.assertEqual(self._sh_names(), [])

    def test_both_enumerators_agree(self):
        for n in ["watch-tasks-stream.pid", "watch-tasks-stream-w1.pid",
                  "watch-tasks-stream-w2.pid", "unrelated.pid"]:
            (self.d / n).write_text("1\n")
        self.assertEqual(sorted(self._names()), sorted(self._sh_names()))


class ReadersUseTheResolver(unittest.TestCase):
    """The naming rule is worthless if a reader still hardcodes the old path."""

    def test_no_reader_hardcodes_the_sentinel_filename(self):
        offenders = []
        for rel in ["src/health-check.py", "src/services_status.py",
                    "src/startup.sh", "src/watch-tasks-stream.sh"]:
            for i, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
                if "watch-tasks-stream.pid" in line and not line.lstrip().startswith("#"):
                    offenders.append(f"{rel}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], "readers must resolve the sentinel, not name it")


if __name__ == "__main__":
    unittest.main(verbosity=2)
