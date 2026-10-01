#!/usr/bin/env python3
"""ask-owner sends a pending question where the owner reads and keeps the file as
the ledger: entry parsed by the notifier, proactive file routed per bridge, a failed
send still leaves the entry and exits 0, a refused osascript names the fix."""
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "scripts" / "ask-owner.py"
sys.path.insert(0, str(REPO / "src"))
import pending_questions_ask as pqa  # noqa: E402

from proactive_routing import proactive_destination  # noqa: E402

from result_markers import parse_markers  # noqa: E402

HOST = "test-host"


def _cpq(pq_file):
    spec = importlib.util.spec_from_file_location("cpq", REPO / "src" / "check-pending-questions.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.PQ_FILE = Path(pq_file)
    return m


class _Workspace(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="pq-send-"))
        (self.ws / "hosts" / HOST).mkdir(parents=True)
        (self.ws / "results").mkdir()
        self.pq = self.ws / "hosts" / HOST / "pending-questions.md"
        self.bin = self.ws / "bin"
        self.bin.mkdir()
        self.calls = self.ws / "osascript-calls"
        self._osascript(0)
        os.environ["SUTANDO_HOST_LABEL"] = HOST
        self.addCleanup(os.environ.pop, "SUTANDO_HOST_LABEL", None)

    def _osascript(self, rc):
        p = self.bin / "osascript"
        p.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{self.calls}'\n"
                     f"[ {rc} -ne 0 ] && echo 'execution error: Not authorized (-1743)' >&2\n"
                     f"exit {rc}\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    def _run(self, *args, path=None):
        env = {**os.environ, "SUTANDO_HOST_LABEL": HOST,
               "PATH": f"{self.bin}:/usr/bin:/bin" if path is None else str(path)}
        return subprocess.run([sys.executable, str(CLI), *args, "--workspace", str(self.ws)],
                              capture_output=True, text=True, env=env)

    def _proactive(self):
        return sorted(p for p in (self.ws / "results").iterdir() if p.name.startswith("proactive-"))

    def _task(self, body):
        t = self.ws / "tasks"
        t.mkdir(exist_ok=True)
        f = t / "task-1.txt"
        f.write_text(body)
        return str(f)


class TestLedger(_Workspace):
    def test_entry_is_parsed_by_the_notifier_and_records_the_send(self):
        r = self._run("Merge #4806 despite the absent CLA check?", "--context", "3 reopen cycles")
        self.assertEqual(r.returncode, 0, r.stderr)
        qs = _cpq(self.pq).get_waiting_questions()
        self.assertEqual(len(qs), 1, self.pq.read_text())
        self.assertIn("Merge #4806 despite the absent CLA check?", qs[0]["title"])
        self.assertEqual(qs[0]["snippet"], "Merge #4806 despite the absent CLA check?")
        self.assertIn("Context: 3 reopen cycles", qs[0]["body"])
        self.assertIn("**Status:** open", qs[0]["body"])
        self.assertRegex(qs[0]["body"], r"\*\*Sent:\*\* owner-dm \(last-active bridge\) via proactive-ask-\S+\.txt at \d{4}-")
        self.assertNotIn("(sending)", qs[0]["body"])
        self.assertTrue(pqa.recently_sent(qs[0]["body"]), "the send stamp must read back")

    def test_entry_goes_above_the_divider_and_below_a_title_line(self):
        self.pq.write_text("# Open\n\n## old — earlier\n\nstill waiting\n\n# Resolved\n\n## [RESOLVED] x\n")
        self._run("new one?")
        text = self.pq.read_text()
        self.assertTrue(text.startswith("# Open\n\n## "), text[:60])
        self.assertLess(text.index("new one?"), text.index("## old"))
        titles = [q["title"] for q in _cpq(self.pq).get_waiting_questions()]
        self.assertEqual(len(titles), 2, titles)

    def test_lock_from_another_writer_is_respected_not_deleted(self):
        lock = Path(str(self.pq) + ".lock")
        lock.mkdir()
        pqa.LOCK_WAIT_SEC = 0.3
        try:
            out = pqa.ask_owner("q?", urgency="durable", workspace=self.ws, host=HOST)
        finally:
            pqa.LOCK_WAIT_SEC = 10
        self.assertIn("could not acquire", out["ledger_error"])
        self.assertTrue(lock.is_dir(), "a foreign lock is never removed")
        self.assertIsNotNone(out["proactive_file"], "the send still happens")


class TestRouting(_Workspace):
    def test_no_task_file_goes_to_the_owner_dm_on_the_last_active_bridge(self):
        self._run("which draft?")
        [f] = self._proactive()
        self.assertIsNone(proactive_destination(f.name), f.name)
        parsed = parse_markers(f.read_text())
        self.assertEqual([a.kind for a in parsed.actions], ["dm-only"])
        self.assertIn("which draft?", parsed.body)
        self.assertIn(f"Question for you from the {HOST} core", parsed.body)
        self.assertNotIn("proactive-pending-q-", f.name, "that prefix is the reminder's own, and it unlinks siblings")

    def test_ag2space_task_routes_to_its_room(self):
        task = self._task("id: task-1\nsource: ag2space\nchannel_id: !abc:ag2.space\n"
                          "task: do the thing\nreply_to_event: $ev\n")
        r = self._run("ship it?", "--task-file", task)
        [f] = self._proactive()
        self.assertEqual(proactive_destination(f.name), "ag2space")
        self.assertEqual(f.read_text().splitlines()[0], "[channel: !abc:ag2.space]")
        redirect = [a for a in parse_markers(f.read_text()).actions if a.kind == "redirect"]
        self.assertEqual(redirect[0].value, "!abc:ag2.space")
        self.assertIn(f"sent: ag2space !abc:ag2.space via results/{f.name}", r.stdout)
        self.assertIn(f"**Sent:** ag2space !abc:ag2.space via {f.name} at ", self.pq.read_text())

    def test_discord_task_routes_to_its_channel(self):
        task = self._task("id: task-1\nsource: discord\nchannel_id: 123456789012345678\ntask: x\n")
        self._run("ship it?", "--task-file", task)
        [f] = self._proactive()
        self.assertEqual(proactive_destination(f.name), "discord")
        self.assertEqual(f.read_text().splitlines()[0], "[channel: 123456789012345678]")

    def test_telegram_task_is_tagged_but_carries_no_room_marker(self):
        task = self._task("id: task-1\nsource: telegram\nchat_id: 42\ntask: x\n")
        self._run("ship it?", "--task-file", task)
        [f] = self._proactive()
        self.assertEqual(proactive_destination(f.name), "telegram")
        self.assertNotIn("[channel:", f.read_text(), "telegram drops the marker; its DM is the chat")
        self.assertIn("**Sent:** telegram 42 via ", self.pq.read_text())

    def test_non_bridge_source_falls_back_to_the_owner_dm(self):
        task = self._task("id: task-1\nsource: chat\nchannel_id: local-chat\ntask: x\n")
        self._run("ship it?", "--task-file", task)
        [f] = self._proactive()
        self.assertIsNone(proactive_destination(f.name))
        self.assertEqual(f.read_text().splitlines()[0], "[dm-only]")

    def test_unreadable_task_file_still_sends_to_the_dm_and_says_so(self):
        r = self._run("ship it?", "--task-file", str(self.ws / "missing.txt"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(self._proactive()), 1)
        self.assertIn("task file unreadable", r.stdout)


class TestFailOpen(_Workspace):
    def test_failed_send_keeps_the_ledger_entry_and_exits_0(self):
        (self.ws / "results").rmdir()
        (self.ws / "results").write_text("not a directory")
        r = self._run("still asked?")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sent: FAILED", r.stdout)
        qs = _cpq(self.pq).get_waiting_questions()
        self.assertEqual(len(qs), 1)
        self.assertIn("**Sent:** FAILED", qs[0]["body"])
        self.assertFalse(pqa.recently_sent(qs[0]["body"]), "a failed send is not a send")

    def test_refused_osascript_prints_the_fix(self):
        self._osascript(1)
        r = self._run("q?")
        self.assertEqual(r.returncode, 0)
        self.assertIn("macos: FAILED", r.stdout)
        self.assertIn("System Settings > Notifications", r.stdout)
        self.assertIn("-1743", r.stdout)
        self.assertNotIn("notification sent", r.stdout)

    def test_missing_osascript_prints_the_fix(self):
        empty = self.ws / "empty-bin"
        empty.mkdir()
        r = self._run("q?", path=empty)
        self.assertIn("osascript not found", r.stdout)
        self.assertIn("System Settings > Notifications", r.stdout)

    def test_live_notifies_and_durable_does_not(self):
        r = self._run("q?")
        self.assertIn("macos: notification sent", r.stdout)
        self.assertIn("Question: q?", self.calls.read_text())
        self.calls.unlink()
        r = self._run("q?", "--urgency", "durable")
        self.assertNotIn("macos:", r.stdout)
        self.assertFalse(self.calls.exists())


class TestReminderQuiet(_Workspace):
    def test_reminder_skips_a_question_sent_within_the_hour(self):
        now = 1_800_000_000.0
        pqa.ask_owner("fresh?", urgency="durable", workspace=self.ws, host=HOST, now=now)
        cpq = _cpq(self.pq)
        qs = cpq.get_waiting_questions()
        self.assertEqual(len(qs), 1)
        self.assertEqual(cpq.due_for_reminder(qs, now=pqa.sent_at(qs[0]["body"]) + 600), [])
        self.assertEqual(len(cpq.due_for_reminder(qs, now=pqa.sent_at(qs[0]["body"]) + 3601)), 1)

    def test_unsent_questions_stay_due(self):
        self.pq.write_text("## old — never sent\n\nplain entry\n")
        cpq = _cpq(self.pq)
        self.assertEqual(len(cpq.due_for_reminder(cpq.get_waiting_questions())), 1)

    def test_main_skips_when_everything_was_just_sent(self):
        pqa.ask_owner("fresh?", urgency="durable", workspace=self.ws, host=HOST)
        [sent_file] = self._proactive()
        cpq = _cpq(self.pq)
        cpq.WORKSPACE = self.ws
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cpq.main()
        self.assertIn("(sent) 1 pending questions", buf.getvalue())
        self.assertEqual(self._proactive(), [sent_file], "no second copy to the owner")


if __name__ == "__main__":
    unittest.main()
