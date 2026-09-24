#!/usr/bin/env python3
"""The native Calendar / Reminders / Contacts scripts need the owner's consent.

Driving those apps raises a macOS Automation permission prompt (user report,
2026-09-24: the desktop app prompted for Calendar without being asked). Contract
for skills/macos-tools/scripts/{calendar-reader,reminders,contacts}.py:

- without `--owner-asked` (or SUTANDO_ALLOW_NATIVE_PIM=1): exit 2, no `open`,
  no osascript, and a stderr message naming the Station-connector order;
- with consent: the flag is stripped and the AppleScript path runs;
- a macOS denial (-1743): exit 3 with a clear message and no second attempt.

All subprocess calls are mocked — no real osascript runs here.
"""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "macos-tools" / "scripts"
DENIED = "execution error: Not authorized to send Apple events to Calendar. (-1743)"


def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _done(cmd, stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout=stdout, stderr=stderr)


def _no_consent_env():
    env = {k: v for k, v in os.environ.items() if k != "SUTANDO_ALLOW_NATIVE_PIM"}
    return patch.dict(os.environ, env, clear=True)


class _Base:  # mixin: the per-script classes below are the TestCases
    script = ""
    app = ""
    argv_ok = []      # a consented invocation (flag included)
    argv_bare = []    # the same without the flag
    ok_stdout = ""

    def setUp(self):
        self.mod = _load(self.script)
        self.calls = []
        self.err = io.StringIO()
        self.out = io.StringIO()

    def _run(self, argv, responder):
        def fake_run(cmd, **kwargs):
            self.calls.append(cmd)
            return responder(cmd)
        with patch.object(self.mod.subprocess, "run", side_effect=fake_run), \
             patch("time.sleep"), \
             contextlib.redirect_stderr(self.err), contextlib.redirect_stdout(self.out):
            try:
                self.mod.main(argv)
            except SystemExit as e:
                return e.code
        return 0

    def test_refuses_without_consent_and_touches_nothing(self):
        with _no_consent_env():
            code = self._run(self.argv_bare, lambda cmd: _done(cmd))
        self.assertEqual(code, self.mod.consent.EXIT_NO_CONSENT)
        self.assertEqual(self.calls, [], "no `open`/osascript without consent")
        msg = self.err.getvalue()
        self.assertIn("--owner-asked", msg)
        self.assertIn("composio_find", msg)
        self.assertIn("mcp__claude_ai_Google_Calendar__", msg)
        self.assertIn("ask the owner", msg)
        self.assertIn("never re-prompt", msg)

    def test_flag_runs_the_applescript_path(self):
        with _no_consent_env():
            code = self._run(self.argv_ok, lambda cmd: _done(cmd, stdout=self.ok_stdout))
        self.assertEqual(code, 0)
        self.assertIn("osascript", [c[0] for c in self.calls])
        self.assertNotIn("--owner-asked", self.out.getvalue())

    def test_env_escape_hatch_counts_as_consent(self):
        with patch.dict(os.environ, {"SUTANDO_ALLOW_NATIVE_PIM": "1"}):
            code = self._run(self.argv_bare, lambda cmd: _done(cmd, stdout=self.ok_stdout))
        self.assertEqual(code, 0)
        self.assertIn("osascript", [c[0] for c in self.calls])

    def test_denial_exits_3_once_with_a_clear_message(self):
        def responder(cmd):
            if cmd[0] == "osascript":
                return _done(cmd, stderr=DENIED, rc=1)
            return _done(cmd)
        with _no_consent_env():
            code = self._run(self.argv_ok, responder)
        self.assertEqual(code, self.mod.consent.EXIT_DENIED)
        self.assertEqual(len([c for c in self.calls if c[0] == "osascript"]), 1, "no retry after a denial")
        msg = self.err.getvalue()
        self.assertIn("Privacy & Security", msg)
        self.assertIn("not asking again", msg)


class TestCalendarReader(_Base, unittest.TestCase):
    script = "calendar-reader"
    argv_ok = ["calendar-reader.py", "1", "text", "--owner-asked"]
    argv_bare = ["calendar-reader.py", "1", "text"]
    ok_stdout = "Work|||Standup|||Monday, March 16, 2026 at 9:00:00 AM|||Monday, March 16, 2026 at 9:30:00 AM|||Room|||notes|||false\n"

    def test_flag_is_stripped_before_the_days_argument(self):
        seen = {}

        def responder(cmd):
            if cmd[0] == "osascript":
                seen["script"] = cmd[2]
            return _done(cmd, stdout=self.ok_stdout)
        with _no_consent_env():
            self._run(["calendar-reader.py", "--owner-asked", "3"], responder)
        self.assertIn("(3 * days)", seen["script"])

    def test_other_errors_keep_the_json_error_shape(self):
        with _no_consent_env():
            code = self._run(["calendar-reader.py", "--owner-asked"],
                             lambda cmd: _done(cmd, stderr="execution error: boom (-600)", rc=1)
                             if cmd[0] == "osascript" else _done(cmd))
        self.assertEqual(code, 0)
        self.assertIn('"error": "execution error: boom (-600)"', self.out.getvalue())


class TestReminders(_Base, unittest.TestCase):
    script = "reminders"
    argv_ok = ["reminders.py", "list", "--due-today", "--owner-asked"]
    argv_bare = ["reminders.py", "list", "--due-today"]
    ok_stdout = "Work|||Call Bob|||missing value|||false|||\n"

    def test_usage_paths_still_answer_with_consent(self):
        """Argument handling is unchanged once the flag is stripped: add, complete, usage."""
        with _no_consent_env():
            code = self._run(["reminders.py", "add", "Fix bug", "2026-03-17", "Work", "--owner-asked"],
                             lambda cmd: _done(cmd))
        self.assertEqual(code, 0)
        self.assertIn("Added: Fix bug (due 2026-03-17)", self.out.getvalue())
        with _no_consent_env():
            code = self._run(["reminders.py", "complete", "Fix bug", "--owner-asked"],
                             lambda cmd: _done(cmd, stdout="Done: Fix bug"))
        self.assertEqual(code, 0)
        self.assertIn("Done: Fix bug", self.out.getvalue())
        for argv in (["reminders.py", "--owner-asked"], ["reminders.py", "add", "--owner-asked"],
                     ["reminders.py", "complete", "--owner-asked"], ["reminders.py", "bogus", "--owner-asked"]):
            with _no_consent_env():
                self.assertEqual(self._run(argv, lambda cmd: _done(cmd)), 1, argv)
        self.assertIn("--owner-asked", self.out.getvalue())

    def test_add_and_lists_share_the_gate(self):
        with _no_consent_env():
            code = self._run(["reminders.py", "add", "Call Bob"], lambda cmd: _done(cmd))
        self.assertEqual(code, self.mod.consent.EXIT_NO_CONSENT)
        self.assertEqual(self.calls, [])
        with _no_consent_env():
            code = self._run(["reminders.py", "lists", "--owner-asked"], lambda cmd: _done(cmd, stdout="Work, Home"))
        self.assertEqual(code, 0)
        self.assertIn("- Work", self.out.getvalue())


class TestContacts(_Base, unittest.TestCase):
    script = "contacts"
    argv_ok = ["contacts.py", "search", "Bob", "--owner-asked"]
    argv_bare = ["contacts.py", "search", "Bob"]
    ok_stdout = "Bob Smith|||bob@x.com,|||123,\n"

    def test_add_and_update_go_through_the_denial_check(self):
        def responder(cmd):
            if cmd[0] == "osascript":
                return _done(cmd, stderr=DENIED, rc=1)
            return _done(cmd)
        for argv in (["contacts.py", "add", "Bob Smith", "--phone", "1", "--owner-asked"],
                     ["contacts.py", "update", "Bob", "--email", "b@x.com", "--owner-asked"]):
            self.calls = []
            with _no_consent_env():
                code = self._run(argv, responder)
            self.assertEqual(code, self.mod.consent.EXIT_DENIED, argv)
            self.assertEqual(len([c for c in self.calls if c[0] == "osascript"]), 1)

    def test_usage_paths_still_answer_with_consent(self):
        with _no_consent_env():
            code = self._run(["contacts.py", "add", "Bob Smith", "--phone", "1", "--email", "b@x.com", "--owner-asked"],
                             lambda cmd: _done(cmd))
        self.assertEqual(code, 0)
        self.assertIn("Added Bob Smith phone:1 email:b@x.com", self.out.getvalue())
        for argv in (["contacts.py", "--owner-asked"], ["contacts.py", "bogus", "--owner-asked"]):
            with _no_consent_env():
                self.assertEqual(self._run(argv, lambda cmd: _done(cmd)), 1, argv)
        self.assertIn("--owner-asked", self.out.getvalue())

    def test_update_succeeds_with_consent(self):
        with _no_consent_env():
            code = self._run(["contacts.py", "update", "Bob", "--phone", "456", "--owner-asked"],
                             lambda cmd: _done(cmd, stdout="OK: updated Bob Smith"))
        self.assertEqual(code, 0)
        self.assertIn("OK: updated Bob Smith", self.out.getvalue())


class TestConsentHelper(unittest.TestCase):
    def setUp(self):
        self.c = _load("native_pim_consent")

    def test_owner_asked_reads_flag_or_env(self):
        with _no_consent_env():
            self.assertFalse(self.c.owner_asked(["x.py", "list"]))
            self.assertTrue(self.c.owner_asked(["x.py", "list", "--owner-asked"]))
        with patch.dict(os.environ, {"SUTANDO_ALLOW_NATIVE_PIM": "1"}):
            self.assertTrue(self.c.owner_asked(["x.py"]))
        with patch.dict(os.environ, {"SUTANDO_ALLOW_NATIVE_PIM": "0"}):
            self.assertFalse(self.c.owner_asked(["x.py"]))

    def test_owner_asked_defaults_to_sys_argv(self):
        with _no_consent_env(), patch.object(sys, "argv", ["x.py", "--owner-asked"]):
            self.assertTrue(self.c.owner_asked())
            self.assertEqual(self.c.require_consent("Calendar"), ["x.py"])

    def test_is_denied_matches_both_spellings(self):
        self.assertTrue(self.c.is_denied(DENIED))
        self.assertTrue(self.c.is_denied("Not authorized to send Apple events to Reminders."))
        self.assertFalse(self.c.is_denied("Application isn't running. (-600)"))
        self.assertFalse(self.c.is_denied(""))

    def test_exit_if_denied_is_a_no_op_otherwise(self):
        self.c.exit_if_denied("Calendar", "some other error")


if __name__ == "__main__":
    _r = unittest.main(exit=False)
    # os._exit skips the atexit handler coverage.py uses to write its fragment.
    try:
        import coverage
        _cov = coverage.Coverage.current()
        if _cov is not None:
            _cov.save()
    except Exception:
        pass
    os._exit(0 if _r.result.wasSuccessful() else 1)
