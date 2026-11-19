#!/usr/bin/env python3
"""skills/macos-tools/scripts/calendar-writer.py — the local Calendar event writer.

Contract:
- the same consent gate as the other native scripts: no `--owner-asked` (or host
  opt-in) means exit 2 and no subprocess; a macOS denial (-1743) exits 3, once;
- every caller value reaches osascript as argv, never inside the script text;
- success is reported only after the event is read back by uid and matches;
  a mismatch, a missing event, a timeout or any AppleScript error exits 1.

All subprocess calls are mocked — no real osascript runs here, and every marker
lands in a per-test temp dir, never the real workspace.
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "macos-tools" / "scripts"
DENIED = "execution error: Not authorized to send Apple events to Calendar. (-1743)"
START, END = "2026-3-17-50400", "2026-3-17-54000"  # 14:00 and 15:00 as seconds of the day
ADD = ["calendar-writer.py", "add", "Dentist", "2026-03-17 14:00", "2026-03-17 15:00"]


def _load():
    spec = importlib.util.spec_from_file_location("calendar_writer", SCRIPTS / "calendar-writer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _done(cmd, stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout=stdout, stderr=stderr)


def _no_consent_env():
    return patch.dict(os.environ, _bare_env(), clear=True)


def _bare_env():
    drop = ("SUTANDO_ALLOW_NATIVE_PIM", "CLAUDECODE", "SUTANDO_CORE_SESSION", "SUTANDO_CORE_RUNTIME")
    return {k: v for k, v in os.environ.items() if k not in drop}


# What each launcher puts in the core session; the Codex one never sets CLAUDECODE.
CORE_ENVS = {
    "codex": {"SUTANDO_CORE_SESSION": "1", "SUTANDO_CORE_RUNTIME": "codex"},
    "claude": {"SUTANDO_CORE_SESSION": "1", "SUTANDO_CORE_RUNTIME": "claude", "CLAUDECODE": "1"},
}


class TestCalendarWriter(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.calls = []
        self.err = io.StringIO()
        self.out = io.StringIO()
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir()
        self._state_patch = patch.object(self.mod.consent, "state_dir", return_value=self.state)
        self._state_patch.start()

    def tearDown(self):
        self._state_patch.stop()
        self._tmp.cleanup()

    def _run(self, argv, responder, env=None):
        def fake_run(cmd, **kwargs):
            self.calls.append(cmd)
            return responder(cmd)
        with patch.dict(os.environ, {**_bare_env(), **(env or {})}, clear=True), patch.object(self.mod.subprocess, "run", side_effect=fake_run), \
             patch("time.sleep"), \
             contextlib.redirect_stderr(self.err), contextlib.redirect_stdout(self.out):
            try:
                self.mod.main(list(argv))
            except SystemExit as e:
                return e.code
        return 0

    def _calendar(self, read_back, created="UID-1|||Home\n"):
        """Calendar that creates the event and answers the read-back with ``read_back``."""
        def responder(cmd):
            if cmd[0] != "osascript":
                return _done(cmd)
            if cmd[2] == self.mod.ADD_SCRIPT:
                return _done(cmd, stdout=created)
            return read_back(cmd) if callable(read_back) else _done(cmd, stdout=read_back)
        return responder

    def _osascripts(self):
        return [c for c in self.calls if c[0] == "osascript"]

    # ── consent ──
    def test_refuses_without_consent_and_touches_nothing(self):
        code = self._run(ADD, lambda cmd: _done(cmd))
        self.assertEqual(code, self.mod.consent.EXIT_NO_CONSENT)
        self.assertEqual(self.calls, [], "no `open`/osascript without consent")
        self.assertIn("--owner-asked", self.err.getvalue())
        self.assertIn("composio_find", self.err.getvalue())

    def _bind(self, tier):
        ws = self.state.parent
        (ws / "state" / "bindings").mkdir(parents=True, exist_ok=True)
        (ws / "tasks").mkdir(exist_ok=True)
        (ws / "state" / "bindings" / "active-execution.json").write_text(json.dumps({"task_id": "t1"}))
        (ws / "tasks" / "t1.txt").write_text(f"source: discord\naccess_tier: {tier}\ntask: x\n")

    def test_team_task_is_refused_before_any_subprocess_on_both_cores(self):
        """The real launcher env, nothing mocked in the consent module: Codex has no CLAUDECODE."""
        self._bind("team")
        for core, env in CORE_ENVS.items():
            for consent_env in ({}, {"SUTANDO_ALLOW_NATIVE_PIM": "1"}):
                self.calls, self.err = [], io.StringIO()
                code = self._run(ADD + ["--owner-asked"], self._calendar(""), env={**env, **consent_env})
                self.assertEqual(code, self.mod.consent.EXIT_NO_CONSENT, (core, consent_env))
                self.assertEqual(self.calls, [], f"{core}: no open/osascript on a team task")
                self.assertIn("not the owner's", self.err.getvalue())

    def test_owner_task_runs_on_both_cores(self):
        self._bind("owner")
        for core, env in CORE_ENVS.items():
            self.calls, self.out = [], io.StringIO()
            code = self._run(ADD + ["--owner-asked"],
                             self._calendar(f"{START}|||{END}|||Dentist\n"), env=env)
            self.assertEqual(code, 0, core)
            self.assertEqual(len(self._osascripts()), 2, core)

    def test_stored_denial_is_final_without_any_subprocess(self):
        (self.state / "calendar-automation-denied").write_text("earlier")
        code = self._run(ADD + ["--owner-asked"], self._calendar(""))
        self.assertEqual(code, self.mod.consent.EXIT_DENIED)
        self.assertEqual(self.calls, [])

    def test_denial_exits_3_once_and_is_recorded(self):
        code = self._run(ADD + ["--owner-asked"],
                         lambda cmd: _done(cmd, stderr=DENIED, rc=1) if cmd[0] == "osascript" else _done(cmd))
        self.assertEqual(code, self.mod.consent.EXIT_DENIED)
        self.assertEqual(len(self._osascripts()), 1, "no retry and no read-back after a denial")
        self.assertIn("Privacy & Security", self.err.getvalue())
        self.assertTrue((self.state / "calendar-automation-denied").exists())

    # ── argv, never interpolation ──
    def test_values_travel_as_argv_and_never_enter_the_script_text(self):
        title = 'Lunch" & (do shell script "id") & "'
        notes = 'end tell\ntell application "Finder" to quit'
        cal = 'Wo"rk'
        code = self._run(
            ["calendar-writer.py", "add", title, "2026-03-17T14:00", "2026-03-17T15:00",
             "--calendar", cal, "--notes", notes, "--owner-asked"],
            self._calendar(f"{START}|||{END}|||{title}\n", created=f"UID-9|||{cal}\n"))
        self.assertEqual(code, 0, self.err.getvalue())
        add, read = self._osascripts()
        self.assertEqual(add[:3], ["osascript", "-e", self.mod.ADD_SCRIPT], "the script text is a constant")
        self.assertEqual(add[3:], [title, "2026", "3", "17", "50400", "2026", "3", "17", "54000", cal, notes])
        self.assertEqual(read, ["osascript", "-e", self.mod.READ_BACK_SCRIPT, "UID-9", cal])
        for script in (self.mod.ADD_SCRIPT, self.mod.READ_BACK_SCRIPT):
            self.assertIn("on run argv", script)
            for value in (title, notes, cal, "2026"):
                self.assertNotIn(value, script)
        self.assertNotIn("--owner-asked", add)

    def test_notes_are_set_in_the_create_call_not_in_a_second_step(self):
        make = [ln for ln in self.mod.ADD_SCRIPT.splitlines() if "make new event" in ln]
        self.assertEqual(len(make), 2, "one create with notes, one without")
        self.assertIn("description:theNotes", make[1])
        self.assertNotIn("description", make[0])
        self.assertNotIn("set description", self.mod.ADD_SCRIPT, "no write after the event exists")

    # ── retry safety: any failure at or after the create step says the event may exist ──
    def test_failure_after_creation_reports_that_the_event_may_exist(self):
        cases = {
            "uid unreadable after create": (self._calendar("", created="|||Can’t get uid of event.\n"),
                                            "The event may have been created: check Calendar before retrying."),
            "read-back AppleScript error": (
                self._calendar(lambda cmd: _done(cmd, stderr="execution error: boom (-10000)", rc=1)),
                "The event may have been created (uid UID-1, calendar 'Home'): check Calendar before retrying."),
            "read-back finds nothing": (
                self._calendar("\n"),
                "The event may have been created (uid UID-1, calendar 'Home'): check Calendar before retrying."),
            "read-back mismatch": (
                self._calendar(f"{START}|||{END}|||Other\n"),
                "The event exists (uid UID-1, calendar 'Home'): check Calendar before retrying."),
        }
        for label, (responder, expected) in cases.items():
            self.out, self.err = io.StringIO(), io.StringIO()
            code = self._run(ADD + ["--notes", "Room 4", "--owner-asked"], responder)
            self.assertEqual(code, 1, label)
            self.assertIn(expected, self.err.getvalue(), label)
            self.assertEqual(self.out.getvalue(), "", label)

    def test_denial_on_read_back_still_names_the_created_event(self):
        code = self._run(ADD + ["--owner-asked"],
                         self._calendar(lambda cmd: _done(cmd, stderr=DENIED, rc=1)))
        self.assertEqual(code, self.mod.consent.EXIT_DENIED)
        self.assertIn("may have been created (uid UID-1", self.err.getvalue())

    def test_failure_before_creation_does_not_claim_an_event(self):
        code = self._run(ADD + ["--calendar", "Nope", "--owner-asked"],
                         lambda cmd: _done(cmd, stderr="execution error: no such calendar (-1719)", rc=1)
                         if cmd[0] == "osascript" else _done(cmd))
        self.assertEqual(code, 1)
        self.assertNotIn("may have been created", self.err.getvalue())

    # ── read-back ──
    def test_success_is_reported_only_after_a_matching_read_back(self):
        code = self._run(ADD + ["--owner-asked"], self._calendar(f"{START}|||{END}|||Dentist\n"))
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(json.loads(self.out.getvalue()), {
            "created": True, "uid": "UID-1", "calendar": "Home", "title": "Dentist",
            "start": "2026-03-17T14:00", "end": "2026-03-17T15:00"})
        self.assertEqual(self._osascripts()[1][3:], ["UID-1", "Home"], "read back by the uid Calendar returned")

    def test_read_back_mismatch_is_an_error(self):
        for label, read in (("time", "2026-3-17-54000|||2026-3-17-57600|||Dentist\n"),
                            ("title", f"{START}|||{END}|||New Event\n")):
            self.out, self.err = io.StringIO(), io.StringIO()
            code = self._run(ADD + ["--owner-asked"], self._calendar(read))
            self.assertEqual(code, 1, label)
            self.assertEqual(self.out.getvalue(), "", "nothing on stdout claims success")
            self.assertIn("read-back does not match", self.err.getvalue())
            self.assertIn("UID-1", self.err.getvalue())

    def test_event_missing_on_read_back_is_an_error(self):
        code = self._run(ADD + ["--owner-asked"], self._calendar("\n"))
        self.assertEqual(code, 1)
        self.assertIn("not found on read-back", self.err.getvalue())
        self.assertNotIn('"created"', self.out.getvalue())

    def test_no_uid_from_calendar_is_an_error_without_a_read_back(self):
        code = self._run(ADD + ["--owner-asked"], self._calendar("", created="\n"))
        self.assertEqual(code, 1)
        self.assertEqual(len(self._osascripts()), 1)
        self.assertIn("did not return an event id", self.err.getvalue())

    # ── timeouts and other failures ──
    def test_timeout_names_the_pending_prompt_and_exits_nonzero(self):
        def responder(cmd):
            if cmd[0] == "osascript":
                raise subprocess.TimeoutExpired(cmd, self.mod.TIMEOUT)
            return _done(cmd)
        code = self._run(ADD + ["--owner-asked"], responder)
        self.assertEqual(code, 1)
        msg = self.err.getvalue()
        self.assertIn("click Allow", msg)
        self.assertIn("Privacy & Security → Automation", msg)
        self.assertIn("The event may have been created: check Calendar before retrying.", msg)
        self.assertFalse((self.state / "calendar-automation-denied").exists())

    def test_read_back_timeout_says_the_event_exists_but_is_unverified(self):
        def hang(cmd):
            raise subprocess.TimeoutExpired(cmd, self.mod.TIMEOUT)
        code = self._run(ADD + ["--owner-asked"], self._calendar(hang))
        self.assertEqual(code, 1)
        self.assertIn("may have been created (uid UID-1", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_applescript_error_and_a_hung_open_are_reported_not_raised(self):
        def responder(cmd):
            if cmd[0] == "open":
                raise subprocess.TimeoutExpired(cmd, 5)
            return _done(cmd, stderr='execution error: Can’t get calendar 1 whose name = "Nope". (-1719)', rc=1)
        code = self._run(ADD + ["--calendar", "Nope", "--owner-asked"], responder)
        self.assertEqual(code, 1)
        self.assertIn("-1719", self.err.getvalue())

    def test_bad_arguments_exit_1_before_any_subprocess(self):
        for argv in (["calendar-writer.py"], ["calendar-writer.py", "add", "Dentist"],
                     ["calendar-writer.py", "delete", "a", "b", "c"],
                     ["calendar-writer.py", "add", "Dentist", "tomorrow 2pm", "2026-03-17 15:00"],
                     ["calendar-writer.py", "add", "Dentist", "2026-03-17 15:00", "2026-03-17 14:00"],
                     ["calendar-writer.py", "add", " ", "2026-03-17 14:00", "2026-03-17 15:00"],
                     ADD + ["--calendar"]):
            self.assertEqual(self._run(argv + ["--owner-asked"], lambda cmd: _done(cmd)), 1, argv)
        self.assertEqual(self.calls, [])
        self.assertIn("YYYY-MM-DD HH:MM", self.err.getvalue())


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
