#!/usr/bin/env python3
"""The watcher sentinel is per instance, keyed on the runtime's own identity.

The defect: every watcher on a host stamped `state/watch-tasks-stream.pid`, so
on a pool host the Nth watcher's write erased the (N-1)th and the four readers
tracked only whichever ran last. Measured on a live pool host: six watcher
processes, one record, four of them invisible to the startup reaper and to
health-check.

There is ONE namer. The shell calls `util_paths.py`, which delegates to
`src/runtime-api/instance_key.py` — the owner the run dir and the durable
registry already share. An earlier revision of this file keyed on an invented
`SUTANDO_INSTANCE` and mirrored a sanitizer in shell; both namers then agreed
with each other and disagreed with production, which is why "the two agree" is
no longer an assertion here.
"""
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
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
        for k in ("SUTANDO_INSTANCE_ID", "SUTANDO_AGENT_ID", "AGENT_MXID",
                  "AGENT_ID", "SUTANDO_INSTANCE"):
            os.environ.pop(k, None)

    def _name(self, **env):
        for k in ("SUTANDO_INSTANCE_ID", "SUTANDO_AGENT_ID", "AGENT_MXID",
                  "AGENT_ID", "SUTANDO_INSTANCE"):
            os.environ.pop(k, None)
        os.environ.update(env)
        try:
            return up.watcher_sentinel_path(self.d).name
        finally:
            for k in env:
                os.environ.pop(k, None)

    def test_the_canonical_default_keeps_the_historic_name(self):
        """A single-instance install must be byte-identical to before."""
        self.assertEqual(self._name(), "watch-tasks-stream.pid")
        self.assertEqual(Path(_sh("sentinel_path_for", str(self.d)).strip()).name,
                         "watch-tasks-stream.pid")

    def test_the_canonical_env_separates_two_watchers(self):
        """SUTANDO_INSTANCE_ID is what the launcher exports. Keying on anything
        else leaves every production watcher on the historic name."""
        a = self._name(SUTANDO_INSTANCE_ID="worker-1")
        b = self._name(SUTANDO_INSTANCE_ID="worker-2")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, "watch-tasks-stream.pid")

    def test_the_invented_variable_is_not_read(self):
        """Guards the exact defect: a name nothing in the repo produces."""
        self.assertEqual(self._name(SUTANDO_INSTANCE="worker-1"),
                         "watch-tasks-stream.pid")

    def test_the_actor_half_participates(self):
        self.assertNotEqual(self._name(SUTANDO_AGENT_ID="A"),
                            self._name(SUTANDO_AGENT_ID="B"))

    def test_case_differing_instances_do_not_share_a_file(self):
        """macOS and Windows default to case-insensitive filesystems, where two
        names that differ only in case are ONE file and the second write wins."""
        self.assertNotEqual(self._name(SUTANDO_INSTANCE_ID="worker").lower(),
                            self._name(SUTANDO_INSTANCE_ID="Worker").lower())

    def test_punctuation_does_not_alias(self):
        for a, b in (("lead", "-lead-"), ("a b", "a?b"), ("a.b", "a-b")):
            with self.subTest(pair=(a, b)):
                self.assertNotEqual(self._name(SUTANDO_INSTANCE_ID=a),
                                    self._name(SUTANDO_INSTANCE_ID=b))

    def test_a_punctuation_only_instance_does_not_become_the_default(self):
        self.assertNotEqual(self._name(SUTANDO_INSTANCE_ID="...."),
                            "watch-tasks-stream.pid")

    def test_the_shell_asks_the_python_owner(self):
        """Not "the two agree" — the shell has no namer of its own to disagree
        with. Any instance the shell resolves must be the Python answer."""
        self.assertNotIn("tr -c", SHELL.read_text())
        for inst in ("worker-1", "Worker", "a?b", "-lead-", "...."):
            with self.subTest(inst=inst):
                sh = Path(_sh("sentinel_path_for", str(self.d),
                              env={"SUTANDO_INSTANCE_ID": inst}).strip()).name
                self.assertEqual(sh, self._name(SUTANDO_INSTANCE_ID=inst))

    def test_a_hostile_instance_name_cannot_escape_the_state_dir(self):
        p = up.watcher_sentinel_path(self.d, "../../etc/passwd")
        self.assertEqual(p.parent, self.d)
        self.assertNotIn("/", p.name)

    def test_a_non_default_instance_refuses_rather_than_aliasing(self):
        """With the encoder unavailable, the historic name would silently put
        two instances on one file. Refuse instead."""
        with unittest.mock.patch.object(up, "_runtime_identity",
                                        return_value=None):
            os.environ["SUTANDO_INSTANCE_ID"] = "worker-1"
            try:
                with self.assertRaises(RuntimeError):
                    up.watcher_sentinel_path(self.d)
                os.environ["SUTANDO_INSTANCE_ID"] = "default"
                self.assertEqual(up.watcher_sentinel_path(self.d).name,
                                 "watch-tasks-stream.pid")
            finally:
                os.environ.pop("SUTANDO_INSTANCE_ID", None)


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

    def test_an_unreadable_state_dir_degrades_instead_of_raising(self):
        """The reaper calls this at startup before anything else, so a raising
        enumerator aborts the boot rather than skipping one sentinel.

        `Path.glob` swallows a missing, unreadable or non-directory path on
        CPython, so injection is the only way to reach the guard -- and the only
        way to show it is not decoration.
        """
        (self.d / "watch-tasks-stream.pid").write_text("1\n")

        def boom(self, pattern):
            raise OSError(13, "Permission denied")

        # The historic name comes from exists(); only the per-instance sweep
        # raises, so a partial answer still beats an exception.
        with unittest.mock.patch.object(Path, "glob", boom):
            names = [p.name for p in up.watcher_sentinel_paths(self.d)]
        self.assertEqual(names, ["watch-tasks-stream.pid"])

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
