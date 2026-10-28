#!/usr/bin/env python3
"""Contacts and Reminders values reach osascript as run-handler arguments.

Each script runs a fixed AppleScript (``on run argv``) and passes the owner's
query, names, phone, email, due date and list name after ``--``. A value that
contains AppleScript quoting stays data: it never appears in the ``-e`` source.
All subprocess calls are mocked; no real osascript runs here.
"""
import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "macos-tools" / "scripts"
QUOTED = 'x") & (do shell script "touch /tmp/sutando-probe") & ("'


def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Harness(unittest.TestCase):
    script = ""

    def setUp(self):
        self.mod = _load(self.script)
        self._tmp = tempfile.TemporaryDirectory()
        state = Path(self._tmp.name) / "state"
        state.mkdir()
        self._patch = patch.object(self.mod.consent, "state_dir", return_value=state)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def osascript_calls(self, argv, stdout="OK"):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        env = {**os.environ, "SUTANDO_ALLOW_NATIVE_PIM": "1"}
        with patch.object(self.mod.subprocess, "run", side_effect=fake_run), patch("time.sleep"), \
             patch.dict(os.environ, env, clear=True), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                self.mod.main(argv)
            except SystemExit:
                pass
        return [c for c in calls if c[0] == "osascript"]

    def assert_values_are_arguments(self, call, script, values):
        self.assertEqual(call[:2], ["osascript", "-e"])
        self.assertIs(call[2], script, "the executed source must be the fixed script")
        self.assertTrue(script.lstrip().startswith("on run argv"))
        self.assertEqual(call[3], "--")
        self.assertEqual(call[4:], values)
        for v in values:
            if v:
                self.assertNotIn(v, call[2])


class ContactsArguments(_Harness):
    script = "contacts"

    def test_search_query_is_an_argument(self):
        [call] = self.osascript_calls(["contacts.py", "search", QUOTED])
        self.assert_values_are_arguments(call, self.mod.SEARCH_SCRIPT, [QUOTED])

    def test_add_fields_are_arguments(self):
        [call] = self.osascript_calls(
            ["contacts.py", "add", f"Bob {QUOTED}", "--phone", QUOTED + "1", "--email", QUOTED + "2"])
        self.assert_values_are_arguments(
            call, self.mod.ADD_SCRIPT, ["Bob", QUOTED, QUOTED + "1", QUOTED + "2"])

    def test_add_without_optional_fields_passes_empty_strings(self):
        [call] = self.osascript_calls(["contacts.py", "add", "Bob"])
        self.assert_values_are_arguments(call, self.mod.ADD_SCRIPT, ["Bob", "", "", ""])

    def test_update_fields_are_arguments(self):
        [call] = self.osascript_calls(["contacts.py", "update", QUOTED, "--email", QUOTED + "2"])
        self.assert_values_are_arguments(call, self.mod.UPDATE_SCRIPT, [QUOTED, "", QUOTED + "2"])

    def test_a_dash_leading_value_stays_after_the_option_terminator(self):
        [call] = self.osascript_calls(["contacts.py", "search", "-e"])
        self.assertEqual(call[3:], ["--", "-e"])


class RemindersArguments(_Harness):
    script = "reminders"

    def test_add_fields_are_arguments(self):
        [call] = self.osascript_calls(["reminders.py", "add", QUOTED, QUOTED + "d", QUOTED + "l"])
        self.assert_values_are_arguments(
            call, self.mod.ADD_SCRIPT, [QUOTED, QUOTED + "d", QUOTED + "l"])

    def test_add_defaults_keep_the_default_list_and_no_due_date(self):
        [call] = self.osascript_calls(["reminders.py", "add", "Buy groceries"])
        self.assert_values_are_arguments(call, self.mod.ADD_SCRIPT, ["Buy groceries", "", ""])
        self.assertIn("default list", self.mod.ADD_SCRIPT)

    def test_complete_name_is_an_argument(self):
        [call] = self.osascript_calls(["reminders.py", "complete", QUOTED])
        self.assert_values_are_arguments(call, self.mod.COMPLETE_SCRIPT, [QUOTED])


if __name__ == "__main__":
    unittest.main()
