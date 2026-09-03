#!/usr/bin/env python3
"""src/pool_names.py — the pool's one naming owner.

Pins: canonical `worker-N`, legacy `core-N` accepted on the read side only,
seat/name resolution, every suffix/label builder, env precedence
(WORKER_ID > WORKER_SEAT > CORE_ID), and the CLI shell callers use.

Run: python3 tests/pool-names.test.py
"""
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import pool_names as pn  # noqa: E402

CLI = REPO / "src" / "pool_names.py"


def cli(*args, env=None):
    return subprocess.run([sys.executable, str(CLI), *args], env=env,
                          capture_output=True, text=True, timeout=20)


class NamesTest(unittest.TestCase):
    def test_worker_name_and_seat(self):
        self.assertEqual(pn.worker_name(3), "worker-3")
        self.assertEqual(pn.worker_name("12"), "worker-12")
        for bad in (0, -1, "x", "", None, "1.5"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                pn.worker_name(bad)
        self.assertEqual(pn.seat_of("worker-4"), 4)
        self.assertEqual(pn.seat_of("core-4"), 4)
        for other in ("pool-lead", "worker-", "worker-0", "worker-x",
                      "sutando-core", "core-agent", "coreworker-1"):
            self.assertIsNone(pn.seat_of(other), other)

    def test_canonical_maps_legacy_only(self):
        self.assertEqual(pn.canonical("core-2"), "worker-2")
        self.assertEqual(pn.canonical("worker-2"), "worker-2")
        self.assertEqual(pn.canonical("pool-lead"), "pool-lead")
        self.assertEqual(pn.canonical("core-agent"), "core-agent")
        self.assertTrue(pn.is_worker_name("worker-1"))
        self.assertFalse(pn.is_worker_name("core-1"))
        self.assertTrue(pn.is_legacy_name("core-1"))

    def test_resolve_accepts_seat_or_either_spelling(self):
        for v in ("2", 2, "worker-2", "core-2", " core-2 "):
            self.assertEqual(pn.resolve(v), "worker-2", repr(v))
        self.assertEqual(pn.resolve("pool-lead"), "pool-lead")

    def test_aliases_canonical_first_legacy_second(self):
        self.assertEqual(pn.aliases("worker-5"), ("worker-5", "core-5"))
        self.assertEqual(pn.aliases("core-5"), ("worker-5", "core-5"))
        self.assertEqual(pn.aliases("pool-lead"), ("pool-lead",))
        self.assertEqual(pn.legacy_name("worker-5"), "core-5")
        self.assertIsNone(pn.legacy_name("pool-lead"))

    def test_suffix_builders_write_canonical_read_both(self):
        self.assertEqual(pn.assigned_suffix("core-1"), ".assigned-worker-1.txt")
        self.assertEqual(pn.claimed_suffix("worker-1"), ".claimed-worker-1.txt")
        self.assertEqual(pn.assigned_suffixes("worker-1"),
                         (".assigned-worker-1.txt", ".assigned-core-1.txt"))
        self.assertEqual(pn.claimed_suffixes("core-1"),
                         (".claimed-worker-1.txt", ".claimed-core-1.txt"))
        self.assertEqual(pn.claimed_suffixes("other"), (".claimed-other.txt",))

    def test_host_surface_builders(self):
        self.assertEqual(pn.launchd_label(2), "com.sutando.worker-2")
        self.assertEqual(pn.launchd_label("core-2"), "com.sutando.worker-2")
        self.assertEqual(pn.tmux_session("3"), "worker-3")
        self.assertEqual(pn.log_stem("core-3"), "worker-3")
        self.assertEqual(pn.alive_filename("core-3"), "worker-3.alive")
        self.assertEqual(pn.alive_filenames("worker-3"),
                         ("worker-3.alive", "core-3.alive"))
        self.assertEqual(pn.done_dir_names("worker-3"), ("worker-3", "core-3"))

    def test_env_precedence(self):
        self.assertEqual(pn.from_env({"SUTANDO_WORKER_ID": "worker-7"}), "worker-7")
        self.assertEqual(pn.from_env({"SUTANDO_WORKER_ID": "core-7"}), "worker-7")
        self.assertEqual(pn.from_env({"SUTANDO_WORKER_SEAT": "2",
                                      "SUTANDO_CORE_ID": "9"}), "worker-2")
        self.assertEqual(pn.from_env({"SUTANDO_CORE_ID": "9"}), "worker-9")
        self.assertIsNone(pn.from_env({}))
        self.assertIsNone(pn.from_env({"SUTANDO_CORE_ID": "legacy"}))
        self.assertEqual(pn.seat_from_env({"SUTANDO_WORKER_ID": "worker-4"}), 4)
        self.assertEqual(pn.pool_size_from_env({"SUTANDO_CORE_POOL_SIZE": "3"}), 3)
        self.assertEqual(pn.pool_size_from_env(
            {"SUTANDO_WORKER_POOL_SIZE": "4", "SUTANDO_CORE_POOL_SIZE": "3"}), 4)
        self.assertIsNone(pn.pool_size_from_env({}))


class MainTest(unittest.TestCase):
    """_main in-process: every exit code the shell callers branch on."""

    def run_main(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=env is not None):
            with redirect_stdout(out), redirect_stderr(err):
                rc = pn._main(list(argv))
        return rc, out.getvalue().strip(), err.getvalue()

    def test_builder_prints_and_exits_0(self):
        self.assertEqual(self.run_main("launchd_label", "core-2"),
                         (0, "com.sutando.worker-2", ""))

    def test_none_result_exits_1_silently(self):
        self.assertEqual(self.run_main("seat_of", "pool-lead"), (1, "", ""))

    def test_bad_seat_exits_2_with_the_reason(self):
        rc, out, err = self.run_main("worker_name", "0")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("seat must be >= 1", err)

    def test_unknown_verb_and_arity_print_usage(self):
        for argv in (("bogus", "1"), ("worker_name",), ()):
            rc, out, err = self.run_main(*argv)
            self.assertEqual((rc, out), (2, ""), argv)
            self.assertIn("usage: pool_names.py", err)

    def test_from_env_prints_the_canonical_name(self):
        self.assertEqual(self.run_main("from_env", env={"SUTANDO_CORE_ID": "3"}),
                         (0, "worker-3", ""))

    def test_from_env_outside_a_pool_exits_2(self):
        self.assertEqual(self.run_main("from_env", env={}), (2, "", ""))


class CliTest(unittest.TestCase):
    def test_builders(self):
        self.assertEqual(cli("worker_name", "2").stdout.strip(), "worker-2")
        self.assertEqual(cli("canonical", "core-2").stdout.strip(), "worker-2")
        self.assertEqual(cli("seat_of", "core-2").stdout.strip(), "2")
        self.assertEqual(cli("launchd_label", "2").stdout.strip(),
                         "com.sutando.worker-2")
        self.assertEqual(cli("legacy_name", "worker-2").stdout.strip(), "core-2")
        r = cli("legacy_name", "pool-lead")
        self.assertEqual((r.returncode, r.stdout), (1, ""))

    def test_usage_and_bad_seat_exit_2(self):
        self.assertEqual(cli().returncode, 2)
        self.assertEqual(cli("nope", "1").returncode, 2)
        r = cli("worker_name", "0")
        self.assertEqual(r.returncode, 2)
        self.assertIn("seat", r.stderr)

    def test_from_env(self):
        base = {"PATH": "/usr/bin:/bin"}
        r = cli("from_env", env={**base, "SUTANDO_CORE_ID": "3"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "worker-3"))
        r = cli("from_env", env={**base, "SUTANDO_WORKER_ID": "worker-5",
                                 "SUTANDO_CORE_ID": "3"})
        self.assertEqual(r.stdout.strip(), "worker-5")
        self.assertEqual(cli("from_env", env=base).returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
