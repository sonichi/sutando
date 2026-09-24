#!/usr/bin/env python3
"""The native Calendar / Reminders / Contacts scripts need the owner's consent.

Driving those apps raises a macOS Automation permission prompt (user report,
2026-09-24: the desktop app prompted for Calendar without being asked). Contract
for skills/macos-tools/scripts/{calendar-reader,reminders,contacts}.py:

- without `--owner-asked` (or SUTANDO_ALLOW_NATIVE_PIM=1): exit 2, no `open`,
  no osascript, and a stderr message naming the Station-connector order;
- with consent: the flag is stripped and the AppleScript path runs;
- a macOS denial (-1743): exit 3 with a clear message and no second attempt,
  recorded in state/<app>-automation-denied so no later run re-asks;
- the owner's persisted host opt-in (state/native-pim-consent, written by
  `native_pim_consent.py grant`) counts like the env var, and `grant` clears
  stored denials.

All subprocess calls are mocked — no real osascript runs here, and every marker
lands in a per-test temp dir, never the real workspace.
"""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
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
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self._state_patch = patch.object(self.mod.consent, "state_dir", return_value=self.state)
        self._state_patch.start()

    def tearDown(self):
        self._state_patch.stop()
        self._tmp.cleanup()

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
        self.assertTrue((self.state / f"{self.app.lower()}-automation-denied").exists(),
                        "the denial is persisted for every other reader")

    def test_stored_denial_is_final_without_any_subprocess(self):
        (self.state / f"{self.app.lower()}-automation-denied").write_text("earlier")
        with _no_consent_env():
            code = self._run(self.argv_ok, lambda cmd: _done(cmd, stdout=self.ok_stdout))
        self.assertEqual(code, self.mod.consent.EXIT_DENIED)
        self.assertEqual(self.calls, [], "a stored denial must not be re-asked")
        self.assertIn("not asking again", self.err.getvalue())

    def test_persisted_consent_marker_counts_as_consent(self):
        (self.state / "native-pim-consent").write_text("owner")
        with _no_consent_env():
            code = self._run(self.argv_bare, lambda cmd: _done(cmd, stdout=self.ok_stdout))
        self.assertEqual(code, 0)
        self.assertIn("osascript", [c[0] for c in self.calls])


class TestCalendarReader(_Base, unittest.TestCase):
    script = "calendar-reader"
    app = "Calendar"
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
    app = "Reminders"
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
    app = "Contacts"
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
            # Each path is exercised fresh: the first denial persisted, so forget it here.
            (self.state / "contacts-automation-denied").unlink(missing_ok=True)
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
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self._state_patch = patch.object(self.c, "state_dir", return_value=self.state)
        self._state_patch.start()

    def tearDown(self):
        self._state_patch.stop()
        self._tmp.cleanup()

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
        self.assertFalse(self.c.denied_earlier("Calendar"))

    def test_markers_are_per_app_and_named_like_the_briefing_one(self):
        self.assertEqual(self.c.denial_marker("Calendar").name, "calendar-automation-denied")
        self.assertEqual(self.c.denial_marker("Contacts").name, "contacts-automation-denied")
        self.assertEqual(self.c.consent_marker().name, "native-pim-consent")

    def test_record_denial_survives_an_unwritable_state_dir(self):
        with patch.object(self.c, "state_dir", return_value=Path("/nonexistent/ro/state")), \
             patch.object(Path, "mkdir", side_effect=OSError("read-only")):
            marker = self.c.record_denial("Reminders")
        self.assertEqual(marker.name, "reminders-automation-denied")

    def test_grant_writes_consent_and_clears_stored_denials(self):
        self.c.record_denial("Calendar")
        self.c.record_denial("Contacts")
        with _no_consent_env():
            self.assertFalse(self.c.host_opted_in())
            out = self.c.grant()
            self.assertTrue(self.c.host_opted_in())
            self.assertTrue(self.c.owner_asked(["x.py"]))
        self.assertFalse(self.c.denied_earlier("Calendar"))
        self.assertFalse(self.c.denied_earlier("Contacts"))
        self.assertIn("calendar-automation-denied", out)
        self.assertIn("contacts-automation-denied", out)

    def test_revoke_removes_consent_only(self):
        self.c.grant()
        self.c.record_denial("Reminders")
        self.assertIn("removed", self.c.revoke())
        self.assertIn("not set", self.c.revoke())
        with _no_consent_env():
            self.assertFalse(self.c.host_opted_in())
        self.assertTrue(self.c.denied_earlier("Reminders"), "revoke does not forget a macOS denial")

    def test_status_names_marker_env_and_each_app(self):
        self.c.record_denial("Contacts")
        with patch.dict(os.environ, {"SUTANDO_ALLOW_NATIVE_PIM": "1"}):
            text = self.c.status()
        self.assertIn("consent marker: absent", text)
        self.assertIn("SUTANDO_ALLOW_NATIVE_PIM: 1", text)
        self.assertIn("Contacts: DENIED by macOS (stored)", text)
        self.assertIn("Calendar: not denied", text)

    def test_cli_grant_revoke_status_and_usage(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(self.c.main(["grant"]), 0)
            self.assertEqual(self.c.main(["status"]), 0)
            self.assertEqual(self.c.main([]), 0)
            self.assertEqual(self.c.main(["revoke"]), 0)
            self.assertEqual(self.c.main(["bogus"]), 1)
        self.assertIn("native PIM allowed on this host", out.getvalue())
        self.assertIn("consent marker: present", out.getvalue())
        self.assertIn("Usage:", err.getvalue())

    def test_no_consent_message_names_the_grant_command(self):
        msg = self.c.no_consent_message("Contacts")
        self.assertIn("native_pim_consent.py grant", msg)
        self.assertIn("--owner-asked", msg)

    def test_state_dir_falls_back_to_the_config_dir_walk(self):
        self._state_patch.stop()
        try:
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/nonexistent/ws/.claude-sutando"}), \
                 patch.dict(sys.modules, {"workspace_default": None}):
                self.assertEqual(self.c.state_dir(), Path("/nonexistent/ws/state"))
                self.assertEqual(self.c._config_dir_workspace(), Path("/nonexistent/ws"))
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/nonexistent/plain"}):
                self.assertTrue(str(self.c._config_dir_workspace()).endswith("sutando-workspace"))
        finally:
            self._state_patch.start()


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
