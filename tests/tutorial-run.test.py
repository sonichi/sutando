#!/usr/bin/env python3
"""skills/tutorial/scripts/tutorial_run.py — the tutorial playbook's publish step and its outcome record.

A task tagged `playbook: tutorial` always runs the Getting started page publish with the task's own
room and owner and publishes a generic page (no roles, prompts or owner name, whatever the setup
inputs or the message say); every run, ok or failed, is recorded for health-check, and a failed run
hands the agent one owner line in place of silence. The verdict is read from what publish reports
(action, page, card), and a reply handed to the runner reaches the room before the page's card
however late the result file is delivered.

Run: python3 tests/tutorial-run.test.py
"""
import importlib.util
import json
import re
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

import tutorial_run as tr  # noqa: E402

ROOM = "!dm:example.org"
OWNER = "@o:example.org"
# What getting_started.py publish prints for a page it made and summoned the owner to.
CREATED = {"ok": True, "action": "created", "kind": "html-1", "summoned": True}

SETUP_BODY = ("Hi, I am Dana Reyes. I am all set up and saying hello. I want to use s.space for "
              "Founder, Product Manager. Give me a quick tour. /tutorial")


def _health_check():
    spec = importlib.util.spec_from_file_location("hc_for_tutorial_run", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HC = _health_check()


def tutorial_row(ws):
    """The `skill:tutorial` row health-check makes from the manifest's declared `last_run`."""
    rows = [r for r in HC.check_skill_last_runs(ws, roots=[REPO / "skills"]) if r["name"] == "skill:tutorial"]
    return rows[0] if rows else None


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

class Publish(_Run):
    def test_always_publishes_with_the_tasks_room_and_owner(self):
        script = self.fake_publish(0, CREATED)
        shown = tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=script, now=0)
        argv = self.argv()
        self.assertEqual(argv[:1], ["publish"])
        self.assertEqual(argv[argv.index("--room") + 1], ROOM)
        self.assertEqual(argv[argv.index("--owner") + 1], OWNER)
        self.assertEqual(argv[argv.index("--name") + 1], "auto", "the page carries the agent's name, never the owner's")
        self.assertTrue(shown["ok"])
        self.assertNotIn("owner_line", shown)

    def test_no_inputs_file_is_no_gate(self):
        script = self.fake_publish(0, CREATED)
        self.assertFalse((self.ws / "state" / "onboarding" / "tutorial-inputs.json").exists())
        self.assertTrue(tr.run(ROOM, OWNER, "/tutorial", self.ws, script=script)["ok"])
        self.assertTrue(self.argv_file.exists(), "publish ran without the setup inputs file")


class Generic(_Run):
    """A room's Commons page cannot yet be limited to its owner: the automatic run never puts what the
    owner said about themselves on it."""

    def test_neither_the_setup_inputs_nor_the_message_put_roles_on_the_page(self):
        inputs = self.ws / "state" / "onboarding" / "tutorial-inputs.json"
        inputs.parent.mkdir(parents=True, exist_ok=True)
        inputs.write_text(json.dumps({"room": ROOM, "owner": OWNER, "roles": ["Designer", "Researcher"]}))
        for body in (SETUP_BODY, "Hi.\nI picked: Founder, Designer.\n/tutorial", "/tutorial"):
            tr.run(ROOM, OWNER, body, self.ws, script=self.fake_publish(0, {"action": "created"}))
            argv = self.argv()
            for flag in ("--roles", "--prompts"):
                self.assertNotIn(flag, argv, f"{flag} for {body!r}")
            self.assertNotIn("Founder", json.dumps(argv), body)
            self.assertNotIn("Designer", json.dumps(argv), body)
            self.assertNotIn("roles_from", self.record(), body)


class Summon(_Run):
    def test_tutorial_sent_alone_summons_on_a_refresh(self):
        tr.run(ROOM, OWNER, " /tutorial ", self.ws, script=self.fake_publish(0, {"action": "updated"}))
        self.assertIn("--summon", self.argv())

    def test_the_setup_message_does_not_force_a_summon(self):
        tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, {"action": "created"}))
        self.assertNotIn("--summon", self.argv())


class Outcome(_Run):
    def test_success_is_recorded(self):
        tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, {**CREATED, "kind": "html-9"}),
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

    def test_a_non_json_last_line_is_skipped_for_the_one_before_it(self):
        self.assertEqual(tr._last_json('{"action": "created"}\nnot json\n[1]'), {"action": "created"})
        self.assertEqual(tr._last_json(""), {})

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
        shown = tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, CREATED))
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
            for path in (body, self.base / "absent.txt"):
                with contextlib.redirect_stdout(io.StringIO()):
                    tr.main(["--room", ROOM, "--owner", OWNER, "--body-file", str(path),
                             "--publish-script", str(self.fake_publish(0, CREATED))])
                self.assertEqual(self.record()["outcome"], "ok", path)
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
        row = tutorial_row(self.ws)
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
                              str(self.fake_publish(0, {**CREATED, "summoned": False, "summon_error": "x"}))])
        finally:
            tr._workspace = real
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["owner_line"], tr.CARD_LINE)


class ReportedOutcome(_Run):
    """Exit 0 is not a page: the verdict comes from the action, the page and the card publish reports."""

    def _run(self, out, body=SETUP_BODY):
        return tr.run(ROOM, OWNER, body, self.ws, script=self.fake_publish(0, out))

    def test_the_actions_are_the_ones_publish_can_report(self):
        src = (SCRIPTS / "getting_started.py").read_text()
        reported = set(re.findall(r'"action": "([a-z]+)"', src)) | set(re.findall(r'action = "([a-z]+)"', src))
        reported -= {"create", "update", "resume", "reconcile", "refreshed"}  # plan's and --all-rooms' own
        self.assertEqual(reported, set(tr.PAGE_ACTIONS) | {tr.SKIPPED_ACTION})

    def test_a_skipped_run_with_no_page_is_not_published(self):
        shown = self._run({"ok": True, "action": "skipped", "kind": None, "summoned": False,
                           "reason": "legacy page missing, edited or ambiguous"})
        self.assertFalse(shown["ok"])
        self.assertEqual(shown["owner_line"], tr.SKIPPED_LINE)
        rec = self.record()
        self.assertEqual((rec["outcome"], rec["exit"], rec["action"]), ("page-left-as-is", 0, "skipped"))
        self.assertEqual(rec["reason"], "legacy page missing, edited or ambiguous")
        row = tutorial_row(self.ws)
        self.assertEqual(row["status"], "warn")
        self.assertIn("made or changed nothing", row["detail"])
        self.assertNotIn("published", row["detail"])

    def test_a_skipped_run_that_names_a_page_is_still_not_published(self):
        shown = self._run({"ok": True, "action": "skipped", "kind": "html-4", "summoned": False,
                           "reason": "owner edit detected during migration"}, body="/tutorial")
        self.assertEqual((shown["outcome"], shown["owner_line"]), ("page-left-as-is", tr.SKIPPED_LINE))

    def test_a_requested_card_that_was_not_sent_is_told_without_a_summon_error(self):
        for action in ("updated", "unchanged", "kept", "resumed"):
            shown = self._run({"ok": True, "action": action, "kind": "html-4", "summoned": False}, body="/tutorial")
            self.assertIn("--summon", self.argv(), action)
            self.assertTrue(shown["ok"], f"{action}: the page is there")
            self.assertEqual(shown["owner_line"], tr.CARD_LINE, action)
            self.assertEqual(self.record()["outcome"], "page-made-card-not-sent", action)
            self.assertEqual(tutorial_row(self.ws)["status"], "warn", action)

    def test_a_requested_card_that_was_sent_is_ok(self):
        for action in ("created", "updated", "unchanged", "kept"):
            shown = self._run({"ok": True, "action": action, "kind": "html-4", "summoned": True}, body="/tutorial")
            self.assertNotIn("owner_line", shown, action)
            rec = self.record()
            self.assertEqual((rec["outcome"], rec["summoned"]), ("ok", True), action)
            self.assertIn(f"page {action}, its card was sent", tutorial_row(self.ws)["detail"], action)

    def test_a_kept_or_unchanged_page_with_no_card_asked_is_quiet_and_says_so(self):
        for action in ("kept", "unchanged", "updated", "resumed"):
            shown = self._run({"ok": True, "action": action, "kind": "html-4", "summoned": False,
                               **({"edited": True} if action == "kept" else {})})
            self.assertTrue(shown["ok"], action)
            self.assertNotIn("owner_line", shown, action)
            row = tutorial_row(self.ws)
            self.assertEqual(row["status"], "ok", action)
            self.assertIn(f"page {action}, no card was owed", row["detail"], action)

    def test_a_created_page_owes_a_card_even_when_none_was_asked(self):
        shown = self._run({"ok": True, "action": "created", "kind": "html-4", "summoned": False})
        self.assertEqual(shown["owner_line"], tr.CARD_LINE)

    def test_an_unknown_or_missing_action_is_not_success(self):
        for out in ({}, {"ok": True}, {"ok": True, "action": "refreshed", "kind": "html-4"},
                    {"ok": True, "action": ["created"], "kind": "html-4", "summoned": True}):
            shown = self._run(out)
            self.assertFalse(shown["ok"], out)
            self.assertEqual(shown["owner_line"], tr.OWNER_LINE, out)
            self.assertEqual(self.record()["outcome"], "publish-outcome-unknown", out)
            self.assertEqual(tutorial_row(self.ws)["status"], "warn", out)

    def test_a_page_action_that_names_no_page_is_not_success(self):
        for kind in (None, "", "doc-1", 7):
            shown = self._run({"ok": True, "action": "created", "kind": kind, "summoned": True})
            self.assertFalse(shown["ok"], kind)
            self.assertEqual(shown["owner_line"], tr.OWNER_LINE, kind)
            self.assertIn("named no page", self.record()["reason"], kind)

    def test_the_cli_exits_nonzero_when_no_page_was_reported(self):
        import contextlib
        import io
        real = tr._workspace
        tr._workspace = lambda: self.ws
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = tr.main(["--room", ROOM, "--owner", OWNER, "--body", "/tutorial", "--publish-script",
                              str(self.fake_publish(0, {"ok": True, "action": "skipped", "kind": None,
                                                        "summoned": False, "reason": "audience unknown"}))])
        finally:
            tr._workspace = real
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue())["owner_line"], tr.SKIPPED_LINE)


class _Room(_Run):
    """One room's timeline as a file of events. The room-ops and publish stand-ins post to it at once,
    as the real ones do; a result file reaches it only when the test's bridge drains, which may be late."""

    def setUp(self):
        super().setUp()
        self.room = self.base / "room.jsonl"
        self.result = self.base / "result.txt"
        self.post("owner", SETUP_BODY)
        # Three reads, no pauses between them.
        waits = getattr(tr, "READ_BACK_WAITS", None)
        tr.READ_BACK_WAITS = (0.0, -1.0, -1.0)
        self.addCleanup(setattr, tr, "READ_BACK_WAITS", waits)

    def post(self, kind: str, body: str) -> None:
        events = self.events()
        with self.room.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event_id": f"$e{len(events)}", "kind": kind, "body": body}) + "\n")

    def events(self) -> list:
        if not self.room.exists():
            return []
        return [json.loads(line) for line in self.room.read_text(encoding="utf-8").splitlines()]

    def timeline(self) -> list:
        """What the agent put in the room, in order."""
        return [f"{e['kind']}:{e['body']}" if e["body"] else e["kind"] for e in self.events()
                if e["kind"] != "owner"]

    def room_ops(self, say: str = "confirmed", lands: bool = True, read: str = "ok") -> Path:
        """room_ops.py's `read` and `say` over the room file. `say`: confirmed (an event id), no_id
        (200, no event id), refused (ok false), network (a network error), silent (no answer at all).
        `lands`: whether the say reaches the room. `read`: ok, down, or down_after_say."""
        script = self.base / "fake_room_ops.py"
        script.write_text(textwrap.dedent(f"""\
            import json, os, sys
            room, said_flag = {str(self.room)!r}, {str(self.base / "said.flag")!r}
            events = [json.loads(l) for l in open(room)] if os.path.exists(room) else []
            if sys.argv[1] == "read":
                assert sys.argv[3] == "--limit", sys.argv
                if {read!r} == "down" or ({read!r} == "down_after_say" and os.path.exists(said_flag)):
                    print(json.dumps({{"ok": False, "reason": "network error: down", "messages": []}}, indent=2))
                else:
                    shown = [{{"event_id": e["event_id"], "body": e["body"], "sender": e["kind"]}}
                             for e in reversed(events)][:int(sys.argv[4])]
                    print(json.dumps({{"ok": True, "messages": shown, "complete": False}}, indent=2))
                sys.exit(0)
            assert sys.argv[1] == "say" and sys.argv[3] == "--body-file", sys.argv
            open(said_flag, "w").close()
            eid = None
            if {lands!r}:
                eid = "$e%d" % len(events)
                with open(room, "a") as fh:
                    fh.write(json.dumps({{"event_id": eid, "kind": "say", "body": open(sys.argv[4]).read()}}) + "\\n")
            answer = {{"confirmed": {{"ok": True, "event_id": eid, "reason": None, "state": "confirmed"}},
                      "no_id": {{"ok": True, "event_id": None, "state": "unconfirmed",
                                "reason": "gateway returned 200 with no event_id — delivery not confirmed"}},
                      "refused": {{"ok": False, "event_id": None, "reason": "HTTP 502", "state": "failed"}},
                      "network": {{"ok": False, "event_id": None, "reason": "network error: timed out",
                                  "state": "failed"}}}}.get({say!r})
            if answer is not None:
                print(json.dumps(answer, indent=2))
            """), encoding="utf-8")
        return script

    def card_publish(self) -> Path:
        script = self.fake_publish(0, CREATED)
        card = json.dumps({"event_id": "$card", "kind": "card", "body": ""})
        script.write_text(f'open({str(self.room)!r}, "a").write({card!r} + "\\n")\n'
                          + script.read_text(encoding="utf-8"), encoding="utf-8")
        return script

    def greet(self, **room_ops) -> dict:
        return tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.card_publish(), greeting="Hello Dana",
                      say_script=self.room_ops(**room_ops))

    def close_task(self, shown: dict, greeting: str = "Hello Dana") -> None:
        """The playbook's last step, then the bridge's late drain of the result file."""
        state = shown.get("greeting")
        if state == "sent":
            body = "[REPLIED]"
        elif state == "unknown":
            body = shown["owner_line"]
        else:
            body = "\n\n".join(x for x in (greeting, shown.get("owner_line")) if x)
        self.result.write_text(body, encoding="utf-8")
        if not body.startswith("[REPLIED]"):
            self.post("result", body.replace("\n", " "))


class GreetingOrder(_Room):
    def test_the_reply_is_in_the_room_before_the_card_when_result_delivery_is_late(self):
        shown = self.greet()
        self.assertEqual(self.timeline(), ["say:Hello Dana", "card"], "before any result file is delivered")
        self.close_task(shown)
        self.assertEqual(self.timeline(), ["say:Hello Dana", "card"], "the late result adds no second reply")
        self.assertEqual((shown["greeting"], self.record()["greeting"]), ("sent", "sent"))
        self.assertNotIn("owner_line", shown)

    def test_a_reply_left_to_the_result_file_lands_after_the_card(self):
        """What the runner's own send exists to prevent: the same run with the reply sent the old way."""
        shown = tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.card_publish())
        self.close_task(shown)
        self.assertEqual(self.timeline(), ["card", "result:Hello Dana"])
        self.assertNotIn("greeting", shown)

    def test_a_refused_reply_publishes_nothing_and_goes_out_as_one_result(self):
        shown = self.greet(say="refused", lands=False)
        self.assertFalse(self.argv_file.exists(), "no card may precede a reply that has not been sent")
        self.assertEqual(self.timeline(), [])
        self.assertEqual((shown["ok"], shown["greeting"], shown["owner_line"]), (False, "not_sent", tr.OWNER_LINE))
        rec = self.record()
        self.assertEqual((rec["outcome"], rec["exit"], rec["reason"]), ("greeting-not-sent", None, "HTTP 502"))
        row = tutorial_row(self.ws)
        self.assertEqual(row["status"], "warn")
        self.assertIn("page was not run", row["detail"])
        self.close_task(shown)
        self.assertEqual(self.timeline(), [f"result:Hello Dana  {tr.OWNER_LINE}"])

    def test_a_reply_file_that_is_empty_or_a_missing_script_sends_nothing(self):
        for text, script in (("  ", self.room_ops()), ("Hello Dana", self.base / "no_room_ops.py")):
            shown = tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.card_publish(), greeting=text,
                           say_script=script)
            self.assertEqual((shown["greeting"], shown["outcome"]), ("not_sent", "greeting-not-sent"), text)
        self.assertEqual(self.timeline(), [])

    def test_the_cli_takes_the_reply_from_a_file_and_an_unreadable_one_is_not_sent(self):
        import contextlib
        import io
        greeting = self.base / "greeting.txt"
        greeting.write_text("Hello Dana", encoding="utf-8")
        real = tr._workspace
        tr._workspace = lambda: self.ws
        try:
            for path, state, rc_want in ((greeting, "sent", 0), (self.base / "absent.txt", "not_sent", 1)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = tr.main(["--room", ROOM, "--owner", OWNER, "--body", SETUP_BODY,
                                  "--greeting-file", str(path), "--publish-script", str(self.card_publish()),
                                  "--say-script", str(self.room_ops())])
                self.assertEqual((rc, json.loads(out.getvalue())["greeting"]), (rc_want, state), path)
        finally:
            tr._workspace = real
        self.assertEqual(self.timeline(), ["say:Hello Dana", "card"])

    def test_the_script_is_room_ops_and_takes_the_arguments_sent(self):
        self.assertEqual(tr.SAY_SCRIPT, REPO / "skills" / "agent-room-ops" / "room_ops.py")
        src = tr.SAY_SCRIPT.read_text(encoding="utf-8")
        for parser, flag in (("say", '"--body-file"'), ("read", '"--limit"')):
            self.assertIn(f'sub.add_parser("{parser}"', src)
            self.assertIn(flag, src)

    def test_the_playbook_steps_send_the_reply_through_the_runner(self):
        steps = (REPO / "skills" / "tutorial" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("--greeting-file", steps)
        self.assertNotIn("to the task's result file, so it is posted before the page", steps)
        for state in ("`sent`", "`not_sent`", "`unknown`"):
            self.assertIn(state, steps)
        self.assertNotIn("`unconfirmed`", steps)


class AmbiguousSend(_Room):
    """An ok with no event id, or a send with no answer, proves nothing: the room is read back, and the
    card goes out only behind a reply that is there."""

    def test_an_ok_with_no_event_id_whose_reply_never_landed_shows_no_card_alone(self):
        shown = self.greet(say="no_id", lands=False)
        self.assertFalse(self.argv_file.exists(), "the card would be the only message in the room")
        self.assertEqual((shown["greeting"], shown["outcome"], shown["owner_line"]),
                         ("not_sent", "greeting-not-sent", tr.OWNER_LINE))
        self.close_task(shown)
        self.assertEqual(self.timeline(), [f"result:Hello Dana  {tr.OWNER_LINE}"], "the reply, once, and no card")

    def test_an_ok_with_no_event_id_whose_reply_landed_is_proven_by_the_read_back(self):
        shown = self.greet(say="no_id")
        self.close_task(shown)
        self.assertEqual(self.timeline(), ["say:Hello Dana", "card"])
        self.assertEqual(shown["greeting"], "sent")

    def test_a_send_that_landed_but_lost_its_answer_is_not_sent_twice(self):
        for say in ("network", "silent"):
            self.room.unlink()
            self.post("owner", SETUP_BODY)
            shown = self.greet(say=say)
            self.close_task(shown)
            self.assertEqual(self.timeline(), ["say:Hello Dana", "card"], f"{say}: one reply, then the card")
            self.assertEqual((shown["greeting"], shown["outcome"]), ("sent", "ok"), say)

    def test_a_say_that_times_out_after_landing_is_found_by_the_read_back(self):
        real = tr.subprocess.run

        def slow_say(cmd, **kw):
            done = real(cmd, **kw)
            if "say" in cmd:
                raise tr.subprocess.TimeoutExpired(cmd="say", timeout=120)
            return done

        tr.subprocess.run = slow_say
        try:
            shown = self.greet(say="confirmed")
        finally:
            tr.subprocess.run = real
        self.close_task(shown)
        self.assertEqual(self.timeline(), ["say:Hello Dana", "card"])
        self.assertEqual(shown["greeting"], "sent")

    def test_a_send_with_no_answer_and_no_reply_in_the_room_is_unknown_and_holds_everything(self):
        for say in ("network", "silent"):
            shown = self.greet(say=say, lands=False)
            self.assertFalse(self.argv_file.exists(), f"{say}: no card behind a reply nobody can vouch for")
            self.assertEqual((shown["ok"], shown["greeting"], shown["outcome"], shown["owner_line"]),
                             (False, "unknown", "greeting-unconfirmed", tr.UNKNOWN_LINE), say)
            self.assertEqual(self.timeline(), [], say)
            row = tutorial_row(self.ws)
            self.assertEqual(row["status"], "warn", say)
            self.assertIn("could not be confirmed either way", row["detail"], say)
        self.close_task(shown)
        self.assertEqual(self.timeline(), [f"result:{tr.UNKNOWN_LINE}"], "never the reply a second time")
        self.assertNotIn("Hello Dana", tr.UNKNOWN_LINE)

    def test_an_unknown_send_that_lands_late_is_still_one_reply(self):
        shown = self.greet(say="network", lands=False)
        self.post("say", "Hello Dana")
        self.close_task(shown)
        self.assertEqual(self.timeline(), ["say:Hello Dana", f"result:{tr.UNKNOWN_LINE}"])

    def test_a_room_that_cannot_be_read_back_leaves_an_unproven_send_unknown(self):
        for say, lands in (("no_id", True), ("no_id", False), ("refused", False)):
            self.room.unlink()
            self.post("owner", SETUP_BODY)
            (self.base / "said.flag").unlink(missing_ok=True)
            shown = self.greet(say=say, lands=lands, read="down_after_say")
            self.assertEqual(shown["greeting"], "unknown", (say, lands))
            self.assertFalse(self.argv_file.exists(), (say, lands))

    def test_a_room_that_cannot_be_read_first_is_never_sent_to(self):
        shown = self.greet(read="down")
        self.assertEqual((shown["greeting"], shown["outcome"]), ("not_sent", "greeting-not-sent"))
        self.assertFalse((self.base / "said.flag").exists(), "say was never called")
        self.close_task(shown)
        self.assertEqual(self.timeline(), [f"result:Hello Dana  {tr.OWNER_LINE}"])

    def test_an_older_message_with_the_same_words_is_not_taken_for_this_reply(self):
        self.post("say", "Hello Dana")
        shown = self.greet(say="no_id", lands=False)
        self.assertEqual(shown["greeting"], "not_sent")
        self.assertFalse(self.argv_file.exists())

    def test_absence_is_proven_only_when_the_read_reaches_what_was_there_before(self):
        before = [{"event_id": "$old", "body": "x"}]
        tr.READ_BACK_WAITS = (0.0, 1.0, 2.0)
        real = tr.read_room
        waits = []
        try:
            tr.read_room = lambda *_a: [{"event_id": "$new", "body": "y"}]
            self.assertIsNone(tr.read_back(ROOM, "Hello", before, Path("x"), waits.append))
            tr.read_room = lambda *_a: [{"event_id": "$new", "body": "y"}, {"event_id": "$old", "body": "x"}]
            self.assertIs(tr.read_back(ROOM, "Hello", before, Path("x"), waits.append), False)
            self.assertIsNone(tr.read_back(ROOM, "Hello", [], Path("x"), waits.append), "an empty room before")
            tr.read_room = lambda *_a: [{"event_id": "$new", "body": " Hello\n"}]
            self.assertIs(tr.read_back(ROOM, "Hello", before, Path("x"), waits.append), True)
        finally:
            tr.read_room = real
        self.assertEqual(waits[:2], [1.0, 2.0], "an absent reply is looked for more than once")
        self.assertEqual(len(tr.READ_BACK_WAITS), 3)

    def test_a_read_answer_that_is_not_a_message_list_is_no_read(self):
        script = self.base / "odd_read.py"
        script.write_text('print(\'{"ok": true, "messages": "none"}\')\n', encoding="utf-8")
        self.assertIsNone(tr.read_room(ROOM, script))


class HealthRow(_Run):
    def test_no_run_yet_is_no_row(self):
        self.assertIsNone(tutorial_row(self.ws))

    def test_a_published_page_is_an_ok_row(self):
        tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, CREATED))
        row = tutorial_row(self.ws)
        self.assertEqual((row["name"], row["status"]), ("skill:tutorial", "ok"))

    def test_a_failed_run_is_a_warn_naming_the_step(self):
        tr.run(ROOM, OWNER, SETUP_BODY, self.ws,
               script=self.fake_publish(3, {"ok": False, "reason": "room-commons not installed"}))
        row = tutorial_row(self.ws)
        self.assertEqual(row["status"], "warn")
        self.assertIn("room-commons-not-installed", row["detail"])

    def test_a_record_that_is_not_an_object_is_no_row(self):
        path = tr.record_path(self.ws)
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2]")
        self.assertIsNone(tutorial_row(self.ws))

    def test_the_row_is_wired_into_the_report(self):
        self.assertIn("checks.extend(check_skill_last_runs())", (REPO / "src" / "health-check.py").read_text())

    def test_the_manifest_declares_the_record_this_script_writes(self):
        manifest = json.loads((REPO / "skills" / "tutorial" / "manifest.json").read_text())
        self.assertEqual(self.ws / "state" / manifest["last_run"], tr.record_path(self.ws))

    def test_without_the_declaration_there_is_no_row(self):
        tr.run(ROOM, OWNER, SETUP_BODY, self.ws, script=self.fake_publish(0, {"action": "created"}))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bare = Path(tmp.name) / "skills"
        (bare / "tutorial").mkdir(parents=True)
        (bare / "tutorial" / "manifest.json").write_text('{"name": "tutorial", "playbook": true}')
        self.assertEqual(HC.check_skill_last_runs(self.ws, roots=[bare]), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
