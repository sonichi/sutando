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

    def test_the_row_is_wired_into_the_report(self):
        self.assertIn("check_tutorial_last_run()", (REPO / "src" / "health-check.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=1)
