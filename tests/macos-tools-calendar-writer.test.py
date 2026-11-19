#!/usr/bin/env python3
"""skills/macos-tools/scripts/calendar-writer.py — the local Calendar event writer.

Contract:
- a write needs `--owner-asked` on the run itself: the host opt-in and the env var
  allow reads only. On a non-owner task, and inside a core session (Claude or
  Codex) with no readable task binding, it exits 2 with no subprocess. A macOS
  denial (-1743) exits 3, once;
- every caller value reaches osascript as argv, never inside the script text;
- the request is echoed before anything is written;
- the event carries an idempotency key, and an event already holding that key is
  reported as existing instead of added again;
- with no `--calendar` and several writable calendars nothing is added;
- success is reported only after the event is read back by uid and its title,
  times and calendar match; any failure at or after creation says the event may
  exist, with the uid when known.

All subprocess calls are mocked — no real osascript runs here, and every marker
lands in a per-test temp dir, never the real workspace.
"""
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "macos-tools" / "scripts"
DENIED = "execution error: Not authorized to send Apple events to Calendar. (-1743)"
START, END = "2026-3-17-50400", "2026-3-17-54000"  # 14:00 and 15:00 as seconds of the day
ADD = ["calendar-writer.py", "add", "Dentist", "2026-03-17 14:00", "2026-03-17 15:00"]
OK_READ = f"{START}|||{END}|||Home|||Dentist\n"
RETRY_HINT = "check Calendar before retrying"

# What each launcher puts in the core session; the Codex one never sets CLAUDECODE.
CORE_ENVS = {
    "codex": {"SUTANDO_CORE_SESSION": "1", "SUTANDO_CORE_RUNTIME": "codex"},
    "claude": {"SUTANDO_CORE_SESSION": "1", "SUTANDO_CORE_RUNTIME": "claude", "CLAUDECODE": "1"},
}


def _load():
    spec = importlib.util.spec_from_file_location("calendar_writer", SCRIPTS / "calendar-writer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _done(cmd, stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout=stdout, stderr=stderr)


def _bare_env():
    drop = ("SUTANDO_ALLOW_NATIVE_PIM", "CLAUDECODE", "SUTANDO_CORE_SESSION", "SUTANDO_CORE_RUNTIME")
    return {k: v for k, v in os.environ.items() if k not in drop}


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
        self.calls, self.err, self.out = [], io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {**_bare_env(), **(env or {})}, clear=True), \
             patch.object(self.mod.subprocess, "run", side_effect=fake_run), \
             patch("time.sleep"), \
             contextlib.redirect_stderr(self.err), contextlib.redirect_stdout(self.out):
            try:
                self.mod.main(list(argv))
            except SystemExit as e:
                return e.code
        return 0

    def _calendar(self, read_back=OK_READ, created="CREATED|||UID-1|||Home\n"):
        """Calendar that answers the add step with ``created`` and the read-back with ``read_back``."""
        def responder(cmd):
            if cmd[0] != "osascript":
                return _done(cmd)
            if cmd[2] == self.mod.ADD_SCRIPT:
                return created(cmd) if callable(created) else _done(cmd, stdout=created)
            return read_back(cmd) if callable(read_back) else _done(cmd, stdout=read_back)
        return responder

    def _osascripts(self):
        return [c for c in self.calls if c[0] == "osascript"]

    def _bind(self, tier):
        ws = self.state.parent
        (ws / "state" / "bindings").mkdir(parents=True, exist_ok=True)
        (ws / "tasks").mkdir(exist_ok=True)
        (ws / "state" / "bindings" / "active-execution.json").write_text(json.dumps({"task_id": "t1"}))
        (ws / "tasks" / "t1.txt").write_text(f"source: discord\naccess_tier: {tier}\ntask: x\n")

    # ── consent: a write needs the flag on the run itself ──
    def test_refuses_without_consent_and_touches_nothing(self):
        code = self._run(ADD, self._calendar())
        self.assertEqual(code, self.mod.consent.EXIT_NO_CONSENT)
        self.assertEqual(self.calls, [], "no `open`/osascript without consent")
        self.assertIn("--owner-asked", self.err.getvalue())
        self.assertIn("composio_find", self.err.getvalue())

    def test_standing_host_opt_in_alone_does_not_allow_a_write(self):
        (self.state / "native-pim-consent").write_text("owner")
        self.assertTrue(self.mod.consent.owner_asked(["x.py"], self.state), "it does allow a read")
        code = self._run(ADD, self._calendar())
        self.assertEqual(code, self.mod.consent.EXIT_NO_CONSENT)
        self.assertEqual(self.calls, [])
        self.assertIn("allow reading, not writing", self.err.getvalue())

    def test_env_var_alone_does_not_allow_a_write(self):
        code = self._run(ADD, self._calendar(), env={"SUTANDO_ALLOW_NATIVE_PIM": "1"})
        self.assertEqual(code, self.mod.consent.EXIT_NO_CONSENT)
        self.assertEqual(self.calls, [])
        self.assertIn("allow reading, not writing", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_flag_with_the_opt_in_and_env_present_still_writes(self):
        (self.state / "native-pim-consent").write_text("owner")
        code = self._run(ADD + ["--owner-asked"], self._calendar(), env={"SUTANDO_ALLOW_NATIVE_PIM": "1"})
        self.assertEqual(code, 0, self.err.getvalue())

    def test_team_task_is_refused_before_any_subprocess_on_both_cores(self):
        """The real launcher env, nothing mocked in the consent module: Codex has no CLAUDECODE."""
        self._bind("team")
        for core, env in CORE_ENVS.items():
            for consent_env in ({}, {"SUTANDO_ALLOW_NATIVE_PIM": "1"}):
                code = self._run(ADD + ["--owner-asked"], self._calendar(), env={**env, **consent_env})
                self.assertEqual(code, self.mod.consent.EXIT_NO_CONSENT, (core, consent_env))
                self.assertEqual(self.calls, [], f"{core}: no open/osascript on a team task")
                self.assertIn("not the owner's", self.err.getvalue())

    def test_unknown_tier_in_a_core_session_is_refused_before_any_subprocess(self):
        ws = self.state.parent
        binding = ws / "state" / "bindings" / "active-execution.json"
        binding.parent.mkdir(parents=True)

        def nothing(): binding.unlink(missing_ok=True)
        def dangling(): binding.write_text(json.dumps({"task_id": "task-gone"}))
        def unreadable(): binding.write_text("{not json")
        for core, env in CORE_ENVS.items():
            for case in (nothing, dangling, unreadable):
                case()
                code = self._run(ADD + ["--owner-asked"], self._calendar(), env=env)
                label = (core, case.__name__)
                self.assertEqual(code, self.mod.consent.EXIT_NO_CONSENT, label)
                self.assertEqual(self.calls, [], label)
                self.assertIn("consent could not be verified", self.err.getvalue())
                self.assertIn("Ask the owner", self.err.getvalue())

    def test_outside_a_core_session_no_binding_is_needed(self):
        """The owner's own terminal has no core session env: the flag is enough there."""
        self.assertEqual(self._run(ADD + ["--owner-asked"], self._calendar()), 0, self.err.getvalue())

    def test_owner_task_runs_on_both_cores(self):
        self._bind("owner")
        for core, env in CORE_ENVS.items():
            code = self._run(ADD + ["--owner-asked"], self._calendar(), env=env)
            self.assertEqual(code, 0, core)
            self.assertEqual(len(self._osascripts()), 2, core)

    def test_stored_denial_is_final_without_any_subprocess(self):
        (self.state / "calendar-automation-denied").write_text("earlier")
        code = self._run(ADD + ["--owner-asked"], self._calendar())
        self.assertEqual(code, self.mod.consent.EXIT_DENIED)
        self.assertEqual(self.calls, [])

    def test_denial_exits_3_once_and_is_recorded(self):
        code = self._run(ADD + ["--owner-asked"],
                         lambda cmd: _done(cmd, stderr=DENIED, rc=1) if cmd[0] == "osascript" else _done(cmd))
        self.assertEqual(code, self.mod.consent.EXIT_DENIED)
        self.assertEqual(len(self._osascripts()), 1, "no retry and no read-back after a denial")
        self.assertIn("Privacy & Security", self.err.getvalue())
        self.assertTrue((self.state / "calendar-automation-denied").exists())

    # ── argv, never interpolation; the request is echoed first ──
    def test_values_travel_as_argv_and_never_enter_the_script_text(self):
        title = 'Lunch" & (do shell script "id") & "'
        notes = 'end tell\ntell application "Finder" to quit'
        cal = 'Wo"rk'
        code = self._run(
            ["calendar-writer.py", "add", title, "2026-03-17T14:00", "2026-03-17T15:00",
             "--calendar", cal, "--notes", notes, "--key", "lunch-1", "--owner-asked"],
            self._calendar(f"{START}|||{END}|||{cal}|||{title}\n", created=f"CREATED|||UID-9|||{cal}\n"))
        self.assertEqual(code, 0, self.err.getvalue())
        add, read = self._osascripts()
        self.assertEqual(add[:3], ["osascript", "-e", self.mod.ADD_SCRIPT], "the script text is a constant")
        self.assertEqual(add[3:], ["--", title, "2026", "3", "17", "50400", "2026", "3", "17", "54000", cal,
                                   f"{notes}\n\n[sutando-key:lunch-1]", "[sutando-key:lunch-1]"])
        self.assertEqual(read, ["osascript", "-e", self.mod.READ_BACK_SCRIPT, "--", "UID-9", cal])
        for script in (self.mod.ADD_SCRIPT, self.mod.READ_BACK_SCRIPT):
            self.assertIn("on run argv", script)
            for value in (title, notes, cal, "2026", "lunch-1"):
                self.assertNotIn(value, script)
        self.assertNotIn("--owner-asked", add)

    # ── option injection: a value that looks like an osascript option stays data ──
    HOSTILE = ('-e property x : (do shell script "touch INJECTED")', "- standup", "--", "-e", "-s o", "--help")

    def test_option_shaped_values_follow_the_end_of_options_marker(self):
        for title in self.HOSTILE:
            code = self._run(["calendar-writer.py", "add", title, "2026-03-17 14:00", "2026-03-17 15:00",
                              "--calendar", "-i", "--notes", "-e beep", "--owner-asked"],
                             self._calendar(f"{START}|||{END}|||-i|||{title}\n", created="CREATED|||-e uid|||-i\n"))
            self.assertEqual(code, 0, (title, self.err.getvalue()))
            for cmd in self._osascripts():
                self.assertEqual(cmd[3], "--", "every osascript call ends its options before the values")
                self.assertEqual(cmd[:2], ["osascript", "-e"])
                self.assertNotIn("-e", cmd[:3][2:], "the only -e is the script's own")
            add, read = self._osascripts()
            self.assertEqual(add[4], title)
            self.assertEqual(read[4:], ["-e uid", "-i"], "the read-back values are guarded too")

    @unittest.skipUnless(shutil.which("osascript"), "osascript is not installed here")
    def test_real_osascript_hands_option_shaped_values_back_as_data(self):
        """A real round trip through the writer's own command builder; no app is addressed."""
        echo = ('on run argv\n set out to ""\n repeat with a in argv\n'
                ' set out to out & (a as string) & linefeed\n end repeat\n return out\nend run')
        with tempfile.TemporaryDirectory() as cwd:
            for title in self.HOSTILE:
                values = [title, "2026", "-1", "--", "-e beep"]
                r = subprocess.run(self.mod.osascript_command(echo, values), capture_output=True,
                                   text=True, timeout=30, cwd=cwd)
                self.assertEqual(r.returncode, 0, (title, r.stderr))
                self.assertEqual(r.stdout.split("\n")[:len(values)], values, title)
                self.assertEqual(os.listdir(cwd), [], "no injected statement ran")

    def test_request_is_echoed_before_anything_is_written(self):
        seen = {}

        def created(cmd):
            seen["echo_at_write"] = self.err.getvalue()
            return _done(cmd, stdout="CREATED|||UID-1|||Home\n")
        code = self._run(ADD + ["--notes", "Room 4", "--owner-asked"], self._calendar(created=created))
        self.assertEqual(code, 0, self.err.getvalue())
        for part in ("Adding to Calendar:", "'Dentist'", "2026-03-17 14:00 to 2026-03-17 15:00",
                     "the only writable one", "'Room 4'", "key "):
            self.assertIn(part, seen["echo_at_write"])

    def test_event_is_made_in_one_step_with_its_notes_and_key(self):
        make = [ln for ln in self.mod.ADD_SCRIPT.splitlines() if "make new event" in ln]
        self.assertEqual(len(make), 1)
        self.assertIn("description:theBody", make[0])
        self.assertNotIn("set description", self.mod.ADD_SCRIPT, "no write after the event exists")

    # ── idempotency ──
    def test_key_is_stable_for_a_retry_and_differs_for_another_event(self):
        k = self.mod.event_key
        when = self.mod.parse_when
        a, b = when("2026-03-17 14:00"), when("2026-03-17 15:00")
        self.assertEqual(k("Dentist", a, b, ""), k(" Dentist ", a, b, ""))
        self.assertEqual(k("Dentist", a, b, "Work"), k("Dentist", a, b, "work"))
        self.assertNotEqual(k("Dentist", a, b, ""), k("Dentist", a, when("2026-03-17 15:30"), ""))
        self.assertNotEqual(k("Dentist", a, b, ""), k("Dentist", a, b, "Work"))
        self.assertEqual(k("Dentist", a, b, "", "mine-1"), "mine-1")
        self._run(ADD + ["--owner-asked"], self._calendar())
        first = self._osascripts()[0][-1]
        self._run(ADD + ["--owner-asked"], self._calendar())
        self.assertEqual(self._osascripts()[0][-1], first, "the retry searches for the same marker")
        self.assertEqual(first, f"[sutando-key:{k('Dentist', a, b, '')}]")

    def test_existing_event_with_the_key_is_reported_not_added_again(self):
        code = self._run(ADD + ["--owner-asked"], self._calendar(created="EXISTS|||UID-7|||Home\n"))
        self.assertEqual(code, 0, self.err.getvalue())
        result = json.loads(self.out.getvalue())
        self.assertEqual((result["created"], result["already_exists"], result["uid"]), (False, True, "UID-7"))
        self.assertEqual(self._osascripts()[1][3:], ["--", "UID-7", "Home"], "the existing event is read back")
        lookup = self.mod.ADD_SCRIPT
        self.assertLess(lookup.index("contains theMarker"), lookup.index("make new event"),
                        "the key is looked up before the create step, in the same run")

    def test_existing_event_that_differs_is_an_error_and_still_no_second_copy(self):
        code = self._run(ADD + ["--key", "k1", "--owner-asked"],
                         self._calendar(f"{START}|||{END}|||Home|||Dentist (moved)\n",
                                        created="EXISTS|||UID-7|||Home\n"))
        self.assertEqual(code, 1)
        self.assertIn("read-back does not match", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_a_second_run_for_the_same_key_is_refused_while_the_first_holds_the_lock(self):
        lock = self.state / "calendar-writer-locks" / (hashlib.sha256(b"k1").hexdigest()[:32] + ".lock")
        lock.parent.mkdir()
        with open(lock, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            code = self._run(ADD + ["--key", "k1", "--owner-asked"], self._calendar())
            self.assertEqual(code, 1)
            self.assertEqual(self._osascripts(), [], "neither the lookup nor the create ran")
            self.assertIn("another run is adding this same event", self.err.getvalue())
            self.assertIn(RETRY_HINT, self.err.getvalue())
            self.assertEqual(self._run(ADD + ["--key", "k2", "--owner-asked"], self._calendar()), 0,
                             "another key is not held up")
        self.assertEqual(self._run(ADD + ["--key", "k1", "--owner-asked"], self._calendar()), 0,
                         "released once the first run ends")

    def test_lock_is_held_across_the_lookup_and_create_and_released_after(self):
        lock = self.state / "calendar-writer-locks" / (hashlib.sha256(b"k1").hexdigest()[:32] + ".lock")
        seen = {}

        def busy(path):
            with open(path, "w") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return True
            return False

        def created(cmd):
            seen["during_add"] = busy(lock)
            return _done(cmd, stdout="CREATED|||UID-1|||Home\n")
        self.assertEqual(self._run(ADD + ["--key", "k1", "--owner-asked"], self._calendar(created=created)), 0)
        self.assertTrue(seen["during_add"])
        self.assertFalse(busy(lock))

    def test_lock_falls_back_to_the_temp_dir_and_an_unusable_one_adds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.mod.tempfile, "gettempdir", return_value=tmp), \
             patch.object(self.mod.consent, "state_dir", side_effect=ImportError("no resolver")):
            with self.mod.key_lock("k1"):
                self.assertEqual(len(os.listdir(tmp)), 1)
        with patch.object(self.mod.tempfile, "gettempdir", return_value="/nonexistent/dir"), \
             patch.object(self.mod.consent, "state_dir", side_effect=ImportError("no resolver")):
            with self.assertRaises(self.mod.WriteError) as ctx:
                with self.mod.key_lock("k1"):
                    pass
        self.assertIn("nothing was added", str(ctx.exception))

    def test_a_lookup_error_is_an_error_not_a_missing_duplicate(self):
        script = self.mod.ADD_SCRIPT
        lookup = script[script.index("whose start date is startDate"):script.index("make new event")]
        self.assertNotIn("try", lookup, "an event that cannot be read stops the run before the create step")
        code = self._run(ADD + ["--owner-asked"], self._calendar(
            created=lambda cmd: _done(cmd, stderr="execution error: Can’t get description of event 2. (-1728)", rc=1)))
        self.assertEqual(code, 1)
        self.assertEqual(len(self._osascripts()), 1, "no read-back: nothing was created")
        self.assertIn("-1728", self.err.getvalue())
        self.assertEqual(self.out.getvalue(), "")

    def test_bad_key_is_refused_before_any_subprocess(self):
        for key in ("has space", "a/b", "x" * 65, "]"):
            self.assertEqual(self._run(ADD + ["--key", key, "--owner-asked"], self._calendar()), 1, key)
            self.assertEqual(self.calls, [], key)

    # ── which calendar ──
    def test_several_writable_calendars_without_a_choice_adds_nothing(self):
        code = self._run(ADD + ["--owner-asked"], self._calendar(created="AMBIGUOUS|||Home\nWork\n"))
        self.assertEqual(code, 1)
        self.assertEqual(len(self._osascripts()), 1)
        msg = self.err.getvalue()
        self.assertIn("'Home', 'Work'", msg)
        self.assertIn("--calendar NAME", msg)
        self.assertIn("nothing was added", msg)
        self.assertNotIn(RETRY_HINT, msg)
        script = self.mod.ADD_SCRIPT
        self.assertLess(script.index('return "AMBIGUOUS|||"'), script.index("make new event"))

    def test_unknown_calendar_adds_nothing(self):
        for argv, expected in ((ADD + ["--calendar", "Nope"], "no calendar named 'Nope'"),
                               (ADD, "no writable calendar")):
            code = self._run(argv + ["--owner-asked"], self._calendar(created="NOCAL|||\n"))
            self.assertEqual(code, 1)
            self.assertIn(expected, self.err.getvalue())
            self.assertNotIn(RETRY_HINT, self.err.getvalue())

    def test_read_back_checks_the_calendar_too(self):
        code = self._run(ADD + ["--calendar", "Work", "--owner-asked"],
                         self._calendar(f"{START}|||{END}|||Personal|||Dentist\n",
                                        created="CREATED|||UID-1|||Personal\n"))
        self.assertEqual(code, 1)
        self.assertIn("in 'Work', Calendar has", self.err.getvalue())
        self.assertIn("in 'Personal'", self.err.getvalue())
        code = self._run(ADD + ["--calendar", "work", "--owner-asked"],
                         self._calendar(f"{START}|||{END}|||Work|||Dentist\n",
                                        created="CREATED|||UID-1|||Work\n"))
        self.assertEqual(code, 0, "calendar names compare without case, as AppleScript does")
        self.assertEqual(json.loads(self.out.getvalue())["calendar"], "Work")

    # ── read-back ──
    def test_success_is_reported_only_after_a_matching_read_back(self):
        code = self._run(ADD + ["--key", "k1", "--owner-asked"], self._calendar())
        self.assertEqual(code, 0, self.err.getvalue())
        self.assertEqual(json.loads(self.out.getvalue()), {
            "created": True, "already_exists": False, "uid": "UID-1", "calendar": "Home",
            "title": "Dentist", "start": "2026-03-17T14:00", "end": "2026-03-17T15:00", "key": "k1"})
        self.assertEqual(self._osascripts()[1][3:], ["--", "UID-1", "Home"], "read back by the uid Calendar returned")

    def test_read_back_mismatch_is_an_error(self):
        for label, read in (("time", "2026-3-17-54000|||2026-3-17-57600|||Home|||Dentist\n"),
                            ("title", f"{START}|||{END}|||Home|||New Event\n")):
            code = self._run(ADD + ["--owner-asked"], self._calendar(read))
            self.assertEqual(code, 1, label)
            self.assertEqual(self.out.getvalue(), "", "nothing on stdout claims success")
            self.assertIn("read-back does not match", self.err.getvalue())
            self.assertIn("The event exists (uid UID-1)", self.err.getvalue())
            self.assertIn(RETRY_HINT, self.err.getvalue())

    # ── retry safety: any failure at or after the create step says the event may exist ──
    def test_failure_after_creation_reports_that_the_event_may_exist(self):
        known = "The event may have been created (uid UID-1, calendar 'Home'): check Calendar before retrying."
        cases = {
            "uid unreadable after create": (self._calendar(created="CREATED||||||Can’t get uid of event.\n"),
                                            "The event may have been created: check Calendar before retrying."),
            "unexpected answer": (self._calendar(created="\n"),
                                  "The event may have been created: check Calendar before retrying."),
            "read-back AppleScript error": (
                self._calendar(lambda cmd: _done(cmd, stderr="execution error: boom (-10000)", rc=1)), known),
            "read-back finds nothing": (self._calendar("\n"), known),
        }
        for label, (responder, expected) in cases.items():
            code = self._run(ADD + ["--notes", "Room 4", "--owner-asked"], responder)
            self.assertEqual(code, 1, label)
            self.assertIn(expected, self.err.getvalue(), label)
            self.assertNotIn("second copy", self.err.getvalue(), "no promise that a retry cannot duplicate")
            self.assertEqual(self.out.getvalue(), "", label)

    def test_denial_on_read_back_still_names_the_created_event(self):
        code = self._run(ADD + ["--owner-asked"],
                         self._calendar(lambda cmd: _done(cmd, stderr=DENIED, rc=1)))
        self.assertEqual(code, self.mod.consent.EXIT_DENIED)
        self.assertIn("may have been created (uid UID-1", self.err.getvalue())

    def test_failure_before_creation_does_not_claim_an_event(self):
        code = self._run(ADD + ["--calendar", "Nope", "--owner-asked"],
                         lambda cmd: _done(cmd, stderr="execution error: boom (-1719)", rc=1)
                         if cmd[0] == "osascript" else _done(cmd))
        self.assertEqual(code, 1)
        self.assertIn("-1719", self.err.getvalue())
        self.assertNotIn("may have been created", self.err.getvalue())

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

    def test_a_hung_open_or_a_missing_osascript_is_reported_not_raised(self):
        def responder(cmd):
            if cmd[0] == "open":
                raise subprocess.TimeoutExpired(cmd, 5)
            raise FileNotFoundError("osascript")
        code = self._run(ADD + ["--owner-asked"], responder)
        self.assertEqual(code, 1)
        self.assertIn("could not run osascript", self.err.getvalue())

    def test_bad_arguments_exit_1_before_any_subprocess(self):
        for argv in (["calendar-writer.py"], ["calendar-writer.py", "add", "Dentist"],
                     ["calendar-writer.py", "delete", "a", "b", "c"],
                     ["calendar-writer.py", "add", "Dentist", "tomorrow 2pm", "2026-03-17 15:00"],
                     ["calendar-writer.py", "add", "Dentist", "2026-03-17 15:00", "2026-03-17 14:00"],
                     ["calendar-writer.py", "add", " ", "2026-03-17 14:00", "2026-03-17 15:00"],
                     ADD + ["--calendar"]):
            self.assertEqual(self._run(argv + ["--owner-asked"], self._calendar()), 1, argv)
            self.assertEqual(self.calls, [], argv)
        self.assertEqual(self._run(["calendar-writer.py", "add", "Dentist", "tomorrow", "x", "--owner-asked"],
                                   self._calendar()), 1)
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
