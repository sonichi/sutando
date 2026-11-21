#!/usr/bin/env python3
"""skills/tutorial/scripts/tutorial_run.py — the tutorial playbook's publish step and its outcome record.

A task tagged `playbook: tutorial` always runs the Getting started page publish with the task's own
room and owner; the setup inputs file only supplies roles, and only for its own room; the roles
sentence is read in either form; every run, ok or failed, is recorded for health-check, and a failed
run hands the agent one owner line in place of silence.

Run: python3 tests/tutorial-run.test.py
"""
import importlib.util
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "tutorial" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import tour_page  # noqa: E402
import tutorial_run as tr  # noqa: E402

ROOM = "!dm:example.org"
OTHER_ROOM = "!other:example.org"
OWNER = "@o:example.org"
SETUP_BODY = ("Hi, I am Dana Reyes. I am all set up and saying hello. I want to use s.space for "
              "Founder, Product Manager. Give me a quick tour. /tutorial")


def _health_check():
    spec = importlib.util.spec_from_file_location("hc_for_tutorial_run", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HC = _health_check()


class _Run(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.ws = self.base / "ws"
        self.ws.mkdir()
        self.argv_file = self.base / "argv.json"

    def tearDown(self):
        self._tmp.cleanup()

    def fake_publish(self, code: int, out: dict, err: str = "") -> Path:
        """A stand-in for getting_started.py: records its argv, prints `out`, exits `code`."""
        script = self.base / "fake_publish.py"
        script.write_text(textwrap.dedent(f"""\
            import json, sys
            open({str(self.argv_file)!r}, "w").write(json.dumps(sys.argv[1:]))
            print(json.dumps({out!r}))
            sys.stderr.write({err!r})
            sys.exit({code})
            """), encoding="utf-8")
        return script

    def argv(self) -> list:
        return json.loads(self.argv_file.read_text())

    def record(self) -> dict:
        return json.loads(tr.record_path(self.ws).read_text())

    def save_inputs(self, room: str, roles) -> None:
        path = tr.inputs_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"room": room, "owner": OWNER, "roles": roles}))


class Publish(_Run):
    def test_always_publishes_with_the_tasks_room_and_owner(self):
        script = self.fake_publish(0, {"action": "created", "kind": "html-1"})
        shown = tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=script, now=0)
        argv = self.argv()
        self.assertEqual(argv[:1], ["publish"])
        self.assertEqual(argv[argv.index("--room") + 1], ROOM)
        self.assertEqual(argv[argv.index("--owner") + 1], OWNER)
        self.assertEqual(argv[argv.index("--name") + 1], "auto", "the page carries the agent's name, never the owner's")
        self.assertTrue(shown["ok"])
        self.assertNotIn("owner_line", shown)

    def test_no_inputs_file_is_no_gate(self):
        script = self.fake_publish(0, {"action": "created", "kind": "html-1"})
        self.assertFalse(tr.inputs_path(self.ws).exists())
        self.assertTrue(tr.run(ROOM, OWNER, "/tutorial", self.ws, script=script)["ok"])
        self.assertTrue(self.argv_file.exists(), "publish ran without the setup inputs file")


class Roles(_Run):
    def test_the_inputs_file_wins_for_its_own_room(self):
        self.save_inputs(ROOM, ["Designer", "Researcher"])
        tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, {"action": "created"}))
        argv = self.argv()
        self.assertEqual(argv[argv.index("--roles") + 1], "Designer, Researcher")
        self.assertEqual(self.record()["roles_from"], "inputs")

    def test_another_rooms_inputs_are_ignored_and_the_message_is_read(self):
        self.save_inputs(OTHER_ROOM, ["Designer"])
        tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, {"action": "created"}))
        argv = self.argv()
        self.assertEqual(argv[argv.index("--roles") + 1], "I want to use s.space for Founder, Product Manager.")
        self.assertEqual(self.record()["roles_from"], "message")

    def test_both_sentence_forms_are_read(self):
        for body, want in (
                (SETUP_BODY, "I want to use s.space for Founder, Product Manager."),
                ("Hi. I picked: Founder, Designer. /tutorial", "I picked: Founder, Designer."),
                ("Hi.\nI picked: Founder, Designer.\n/tutorial", "I picked: Founder, Designer."),
                ("I want to use s.space for founder", "I want to use s.space for founder")):
            self.assertEqual(tr.roles_from_body(body), want, body)
            self.assertEqual(tour_page.role_labels(want)[0], "Founder", want)

    def test_ordinary_words_are_not_roles(self):
        for body in ("I want to use it for nothing here, honestly", "I want to use Slack for chat. /tutorial",
                     "Hi, I am all set up and saying hello. Give me a quick tour. /tutorial"):
            self.assertEqual(tr.roles_from_body(body), "", body)

    def test_role_labels_reads_both_forms_the_same(self):
        self.assertEqual(tour_page.role_labels("I want to use s.space for Founder, Product Manager."),
                         ["Founder", "Product Manager"])
        self.assertEqual(tour_page.role_labels("I picked: Founder, Product Manager."),
                         ["Founder", "Product Manager"])

    def test_no_roles_means_no_roles_flag(self):
        tr.run(ROOM, OWNER, "Hi, I am all set up and saying hello. Give me a quick tour. /tutorial",
               self.ws, script=self.fake_publish(0, {"action": "created"}))
        self.assertNotIn("--roles", self.argv())
        self.assertEqual(self.record()["roles_from"], "none")


class Summon(_Run):
    def test_tutorial_sent_alone_summons_on_a_refresh(self):
        tr.run(ROOM, OWNER, " /tutorial ", self.ws, script=self.fake_publish(0, {"action": "updated"}))
        self.assertIn("--summon", self.argv())

    def test_the_setup_message_does_not_force_a_summon(self):
        tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, {"action": "created"}))
        self.assertNotIn("--summon", self.argv())


class Outcome(_Run):
    def test_success_is_recorded(self):
        tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, {"action": "created", "kind": "html-9"}),
               now=1795000000)
        rec = self.record()
        self.assertEqual((rec["outcome"], rec["exit"], rec["action"], rec["kind"]), ("ok", 0, "created", "html-9"))
        self.assertEqual((rec["room"], rec["owner"]), (ROOM, OWNER))
        self.assertEqual(rec["at"], "2026-11-18T11:06:40Z")

    def test_each_failure_exit_is_recorded_and_hands_the_owner_line(self):
        for code, out, err, outcome, reason in (
                (2, {}, "getting_started: no --room\n", "no-room-or-bad-input", "getting_started: no --room"),
                (3, {"ok": False, "reason": "room-commons not installed"}, "", "room-commons-not-installed",
                 "room-commons not installed"),
                (4, {"ok": False, "reason": "no agent identity"}, "", "commons-call-failed", "no agent identity"),
                (1, {}, "Traceback: boom\n", "publish-did-not-finish", "Traceback: boom")):
            shown = tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(code, out, err))
            rec = self.record()
            self.assertEqual((rec["outcome"], rec["exit"], rec["reason"]), (outcome, code, reason), code)
            self.assertFalse(shown["ok"], code)
            self.assertEqual(shown["owner_line"], tr.OWNER_LINE, code)
            self.assertIn("/tutorial", shown["owner_line"])

    def test_a_publish_that_cannot_start_is_recorded_too(self):
        shown = tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.base / "missing.py")
        self.assertFalse(shown["ok"])
        self.assertEqual(self.record()["outcome"], "publish-did-not-finish")

    def test_the_cli_exits_by_the_outcome(self):
        import contextlib
        import io
        script = self.fake_publish(3, {"ok": False, "reason": "room-commons not installed"})
        out = io.StringIO()
        # The CLI resolves the real workspace; point it at this test's own instead.
        real = tr._workspace
        tr._workspace = lambda: self.ws
        try:
            with contextlib.redirect_stdout(out):
                rc = tr.main(["--room", ROOM, "--owner", OWNER, "--body", SETUP_BODY,
                              "--publish-script", str(script)])
        finally:
            tr._workspace = real
        self.assertEqual(rc, 1)
        self.assertEqual(self.record()["outcome"], "room-commons-not-installed")
        self.assertEqual(json.loads(out.getvalue())["outcome"], "room-commons-not-installed")


class EdgePaths(_Run):
    def test_the_last_json_line_is_read_past_other_output(self):
        script = self.base / "noisy.py"
        script.write_text('print("warming up")\nprint("{not json")\nprint(\'{"action": "updated"}\')\n',
                          encoding="utf-8")
        self.assertEqual(tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=script)["action"], "updated")

    def test_a_publish_that_raises_is_recorded(self):
        real = tr.subprocess.run

        def boom(*_a, **_k):
            raise tr.subprocess.TimeoutExpired(cmd="publish", timeout=600)

        tr.subprocess.run = boom
        try:
            shown = tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, {}))
        finally:
            tr.subprocess.run = real
        self.assertEqual(shown["outcome"], "publish-did-not-finish")
        self.assertIn("TimeoutExpired", shown["reason"])

    def _refusing_record_writes(self):
        real = tr.os.replace

        def refuse(*_a, **_k):
            raise OSError("read-only")

        tr.os.replace = refuse
        self.addCleanup(setattr, tr.os, "replace", real)

    def test_a_record_that_cannot_be_written_leaves_no_temp_file_and_no_false_failure(self):
        self._refusing_record_writes()
        shown = tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, {"action": "created"}))
        self.assertTrue(shown["ok"], "a published page is not reported failed because recording failed")
        self.assertNotIn("owner_line", shown)
        self.assertIn("read-only", shown["record_error"])
        self.assertEqual(list(tr.record_path(self.ws).parent.glob(".last-run.*")), [])

    def test_a_failed_publish_still_hands_the_retry_notice_when_recording_fails(self):
        import contextlib
        import io
        self._refusing_record_writes()
        real = tr._workspace
        tr._workspace = lambda: self.ws
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = tr.main(["--room", ROOM, "--owner", OWNER, "--body", SETUP_BODY, "--publish-script",
                              str(self.fake_publish(3, {"ok": False, "reason": "room-commons not installed"}))])
        finally:
            tr._workspace = real
        shown = json.loads(out.getvalue())
        self.assertEqual(rc, 1)
        self.assertEqual(shown["owner_line"], tr.OWNER_LINE)
        self.assertEqual(shown["outcome"], "room-commons-not-installed")
        self.assertIn("record_error", shown)
        self.assertEqual(list(tr.record_path(self.ws).parent.glob(".last-run.*")), [])

    def test_the_cli_reads_the_body_file_and_survives_a_missing_one(self):
        import contextlib
        import io
        body = self.base / "body.txt"
        body.write_text(SETUP_BODY, encoding="utf-8")
        real = tr._workspace
        tr._workspace = lambda: self.ws
        try:
            for path, want in ((body, "message"), (self.base / "absent.txt", "none")):
                with contextlib.redirect_stdout(io.StringIO()):
                    tr.main(["--room", ROOM, "--owner", OWNER, "--body-file", str(path),
                             "--publish-script", str(self.fake_publish(0, {"action": "created"}))])
                self.assertEqual(self.record()["roles_from"], want, path)
        finally:
            tr._workspace = real

    def test_the_workspace_comes_from_the_shared_resolver(self):
        import workspace_default
        self.assertEqual(tr._workspace(), workspace_default.resolve_workspace())


class CardNotSent(_Run):
    def test_a_created_page_whose_card_failed_is_not_a_silent_success(self):
        out = {"ok": True, "action": "created", "kind": "html-3", "summoned": False,
               "summon_error": "summon refused: 502"}
        shown = tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, out))
        self.assertTrue(shown["ok"], "the page exists")
        self.assertEqual(shown["owner_line"], tr.CARD_LINE)
        self.assertIn("/tutorial", shown["owner_line"])
        rec = self.record()
        self.assertEqual((rec["outcome"], rec["reason"], rec["kind"]),
                         ("page-made-card-not-sent", "summon refused: 502", "html-3"))
        row = HC.check_tutorial_last_run(self.ws)
        self.assertEqual(row["status"], "warn")
        self.assertIn("card did not reach the owner", row["detail"])

    def test_an_ordinary_refresh_that_does_not_summon_says_nothing(self):
        out = {"ok": True, "action": "updated", "kind": "html-3", "summoned": False}
        shown = tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, out))
        self.assertTrue(shown["ok"])
        self.assertNotIn("owner_line", shown)
        self.assertEqual(self.record()["outcome"], "ok")

    def test_the_cli_exits_zero_when_the_page_exists_without_its_card(self):
        import contextlib
        import io
        real = tr._workspace
        tr._workspace = lambda: self.ws
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = tr.main(["--room", ROOM, "--owner", OWNER, "--body", SETUP_BODY, "--publish-script",
                              str(self.fake_publish(0, {"action": "created", "summon_error": "x"}))])
        finally:
            tr._workspace = real
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["owner_line"], tr.CARD_LINE)


class HealthRow(_Run):
    def test_no_run_yet_is_no_row(self):
        self.assertIsNone(HC.check_tutorial_last_run(self.ws))

    def test_a_published_page_is_an_ok_row(self):
        tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, {"action": "created"}))
        row = HC.check_tutorial_last_run(self.ws)
        self.assertEqual((row["name"], row["status"]), ("tutorial-page", "ok"))

    def test_a_failed_run_is_a_warn_naming_the_step(self):
        tr.run(ROOM, OWNER, SETUP_BODY, self.ws,
               script=self.fake_publish(3, {"ok": False, "reason": "room-commons not installed"}))
        row = HC.check_tutorial_last_run(self.ws)
        self.assertEqual(row["status"], "warn")
        self.assertIn("room-commons-not-installed", row["detail"])

    def test_a_record_that_is_not_an_object_is_no_row(self):
        path = tr.record_path(self.ws)
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2]")
        self.assertIsNone(HC.check_tutorial_last_run(self.ws))

    def test_the_row_is_wired_into_the_report(self):
        self.assertIn("check_tutorial_last_run()", (REPO / "src" / "health-check.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=1)
