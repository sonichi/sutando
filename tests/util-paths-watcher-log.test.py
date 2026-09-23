#!/usr/bin/env python3
"""`util_paths.watcher_log_path` / `watcher_log_cursor_path`: one log per inbox,
and the cursor that names its reader.

These two paths are the only agreement between four programs that never call
each other -- the detacher, the watcher, the log reader and the freshness
predicate. A shell caller reaches them through the CLI rather than rebuilding
the encoding, so the CLI is pinned here beside the functions.

Run: python3 tests/util-paths-watcher-log.test.py
"""
from __future__ import annotations

import contextlib
import io
import runpy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import util_paths as up  # noqa: E402


class _Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class TestWatcherLogPath(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name) / "ws"

    def test_the_log_lives_under_the_workspace_logs_dir(self):
        p = up.watcher_log_path(self.ws, self.ws / "tasks")
        self.assertEqual(p.parent, self.ws / "logs")
        self.assertTrue(p.name.endswith(".events.log"), p.name)

    def test_two_inboxes_never_share_a_log(self):
        # A pool host runs many inboxes under one workspace, and a session
        # tailing its own must not see another's deliveries.
        a = up.watcher_log_path(self.ws, self.ws / "tasks")
        b = up.watcher_log_path(self.ws, self.ws / "deliveries" / "w1")
        self.assertNotEqual(a, b)

    def test_the_name_comes_from_the_inbox_basename(self):
        self.assertIn("w1", up.watcher_log_path(self.ws, self.ws / "deliveries" / "w1").name)

    def test_a_trailing_slash_does_not_split_one_inbox_into_two(self):
        bare = up.watcher_log_path(self.ws, str(self.ws / "tasks"))
        slashed = up.watcher_log_path(self.ws, str(self.ws / "tasks") + "/")
        self.assertEqual(bare, slashed)

    def test_an_inbox_with_no_basename_falls_back_rather_than_naming_the_dir(self):
        # "/" has no name; a bare "logs/watcher-.events.log" would collide with
        # every other nameless inbox on the host.
        self.assertEqual(up.watcher_log_path(self.ws, "/").name,
                         up.watcher_log_path(self.ws, "/tasks").name)

    def test_characters_a_filename_cannot_hold_are_replaced(self):
        name = up.watcher_log_path(self.ws, "/x/we ird:name").name
        self.assertNotIn(" ", name)
        self.assertNotIn(":", name)
        self.assertIn("we_ird_name", name)

    def test_two_inboxes_differing_only_in_a_replaced_character_still_collide(self):
        # Sanitisation is lossy: "a:b" and "a b" map to one log. Pinned so a
        # change to the rule is deliberate rather than discovered.
        self.assertEqual(up.watcher_log_path(self.ws, "/x/a:b"),
                         up.watcher_log_path(self.ws, "/x/a b"))


class TestWatcherLogCursorPath(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name) / "ws"

    def test_the_cursor_lives_in_state_not_beside_the_log(self):
        # state/ is where every other liveness record lives, and what the Stop
        # hook and the supervisor are already pointed at.
        c = up.watcher_log_cursor_path(self.ws, self.ws / "tasks")
        self.assertEqual(c.parent, self.ws / "state")

    def test_the_cursor_is_named_after_its_own_log(self):
        log = up.watcher_log_path(self.ws, self.ws / "tasks")
        cur = up.watcher_log_cursor_path(self.ws, self.ws / "tasks")
        self.assertEqual(cur.name, log.name + ".cursor")

    def test_two_inboxes_never_share_a_cursor(self):
        a = up.watcher_log_cursor_path(self.ws, self.ws / "tasks")
        b = up.watcher_log_cursor_path(self.ws, self.ws / "deliveries" / "w1")
        self.assertNotEqual(a, b)


class TestCli(unittest.TestCase):
    """The shell callers ask for these paths rather than rebuilding them; a
    second encoding is the failure this CLI exists to prevent."""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name) / "ws"

    MODULE = ROOT / "src" / "util_paths.py"

    def _run(self, *args):
        """In-process, via runpy: one interpreter, no PATH assumption, and the
        same argv the shell callers pass."""
        argv = sys.argv
        out, err = io.StringIO(), io.StringIO()
        code = 0
        sys.argv = ["util_paths.py", *args]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    runpy.run_path(str(self.MODULE), run_name="__main__")
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 1
        finally:
            sys.argv = argv
        return _Result(code, out.getvalue(), err.getvalue())

    def test_watcher_log_prints_what_the_function_returns(self):
        r = self._run("watcher-log", str(self.ws), str(self.ws / "tasks"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(up.watcher_log_path(self.ws, self.ws / "tasks")))

    def test_watcher_log_cursor_prints_what_the_function_returns(self):
        r = self._run("watcher-log-cursor", str(self.ws), str(self.ws / "tasks"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(),
                         str(up.watcher_log_cursor_path(self.ws, self.ws / "tasks")))

    def test_a_missing_inbox_argument_is_a_usage_error_not_a_guess(self):
        # Both take TWO operands; a one-operand fallthrough would print a path
        # built from a workspace read as an inbox.
        for sub in ("watcher-log", "watcher-log-cursor"):
            r = self._run(sub, str(self.ws))
            self.assertEqual(r.returncode, 2, sub)
            self.assertIn("usage:", r.stderr)

    def test_the_usage_line_names_both_subcommands(self):
        # A usage line that omits a subcommand sends its caller looking for a
        # path helper that is already there.
        err = self._run("nonsense").stderr
        self.assertIn("watcher-log", err)
        self.assertIn("watcher-log-cursor", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
