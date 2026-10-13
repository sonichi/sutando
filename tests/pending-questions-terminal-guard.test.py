#!/usr/bin/env python3
"""Owner questions left only in the core's terminal text are refused once, toward ask-owner."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "skills" / "pending-questions" / "hooks" / "terminal-questions.py"
_spec = importlib.util.spec_from_file_location("terminal_questions", HOOK)
tq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tq)

CORE = {"SUTANDO_CORE_SESSION": "1"}
INCIDENT = """I reviewed the two PRs and posted the results. Nothing was pushed or merged.

Decisions for you:
- Was your "ok" on #1 also the go-ahead to restart the connection for its live test?
- Reply "y" to send the install message now.
- Run the full restart now, or wait for a quiet moment?
- Once #2 merges, OK to restart the connection so card clicks work?
- Should I look into the undelivered replies?
"""
STATUS = "Reviewed #1 and #2; results posted, nothing merged. Two questions filed to your DM."


def prompt(pid, text="go"):
    return {"type": "user", "promptId": pid, "uuid": pid + "-u", "message": {"role": "user", "content": text}}


def assistant_text(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def bash(tid, command, pid, output):
    return [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}]}},
        {"type": "user", "promptId": pid, "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "content": output}]}},
    ]


ASK_OK = "recorded: q-20261012-1 — Restart now?\nsent: queued owner-dm via results/proactive-1.txt (…)"


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ws = Path(self._td.name) / "ws"
        self.transcript = Path(self._td.name) / "t.jsonl"

    def write(self, entries):
        self.transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))

    def payload(self, **kw):
        p = {"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": False,
             "transcript_path": str(self.transcript)}
        p.update(kw)
        return p

    def decide(self, env=CORE, **kw):
        return tq.decide(self.payload(**kw), self.ws, env)


class Decide(Base):
    def test_incident_shape_blocks(self):
        self.write([prompt("p1"), assistant_text(INCIDENT)])
        reason = self.decide()
        self.assertIsNotNone(reason)
        self.assertIn("scripts/ask-owner.py", reason)
        self.assertIn("4 questions", reason)

    def test_last_assistant_message_is_read_first(self):
        self.write([prompt("p1"), assistant_text(STATUS)])
        self.assertIsNotNone(self.decide(last_assistant_message=INCIDENT))

    def test_turn_that_filed_through_ask_owner_passes(self):
        self.write([prompt("p1"),
                    *bash("t1", 'python3 scripts/ask-owner.py "Restart now?"', "p1", ASK_OK),
                    assistant_text(INCIDENT)])
        self.assertIsNone(self.decide())

    def test_pq_ask_with_list_content_result_passes(self):
        out = [{"type": "text", "text": ASK_OK}]
        self.write([prompt("p1"),
                    *bash("t1", 'python3 skills/pending-questions/scripts/pq.py ask "x?"', "p1", out),
                    assistant_text(INCIDENT)])
        self.assertIsNone(self.decide())

    def test_an_ask_in_an_earlier_turn_does_not_count(self):
        self.write([prompt("p0"),
                    *bash("t1", 'python3 scripts/ask-owner.py "Restart now?"', "p0", ASK_OK),
                    assistant_text("filed"), prompt("p1"), assistant_text(INCIDENT)])
        self.assertIsNotNone(self.decide())

    def test_failed_ask_does_not_count(self):
        failed = "recorded: FAILED — no store (NOT recorded anywhere; ask by hand)\nsent: FAILED — x"
        self.write([prompt("p1"),
                    *bash("t1", 'python3 scripts/ask-owner.py "Restart now?"', "p1", failed),
                    assistant_text(INCIDENT)])
        self.assertIsNotNone(self.decide())

    def test_report_line_from_another_command_does_not_count(self):
        self.write([prompt("p1"),
                    *bash("t1", "grep -rn queued src/", "p1", "sent: queued owner-dm"),
                    assistant_text(INCIDENT)])
        self.assertIsNotNone(self.decide())

    def test_plain_status_line_passes(self):
        self.write([prompt("p1"), assistant_text(STATUS)])
        self.assertIsNone(self.decide())

    def test_one_question_or_quoted_or_fenced_questions_pass(self):
        for text in ("Done. Should I also restart?",
                     "Done.\n- Should I also restart?\n- Merged #1.",
                     "Qingyun asked:\n> - Is X right?\n> - Is Y right?",
                     "Repro:\n```\n- a?\n- b?\n```"):
            self.write([prompt("p1"), assistant_text(text)])
            self.assertIsNone(self.decide(), text)

    def test_fires_once_per_turn(self):
        self.write([prompt("p1"), assistant_text(INCIDENT)])
        self.assertIsNotNone(self.decide())
        self.assertIsNone(self.decide(), "second Stop of the same turn")
        self.assertIsNone(self.decide(stop_hook_active=True))
        self.write([prompt("p1"), assistant_text(INCIDENT), prompt("p2"), assistant_text(INCIDENT)])
        self.assertIsNotNone(self.decide(), "a new turn is refused again")

    def test_stop_hook_active_never_blocks(self):
        self.write([prompt("p1"), assistant_text(INCIDENT)])
        self.assertIsNone(self.decide(stop_hook_active=True))

    def test_guests_and_workers_pass(self):
        self.write([prompt("p1"), assistant_text(INCIDENT)])
        for env in ({}, {"SUTANDO_CORE_SESSION": "0"},
                    {"SUTANDO_CORE_SESSION": "1", "SUTANDO_INSTANCE_ID": "w1"}):
            self.assertIsNone(self.decide(env=env), env)

    def test_unwritable_marker_fails_open(self):
        self.write([prompt("p1"), assistant_text(INCIDENT)])
        self.ws.mkdir(parents=True)
        (self.ws / "state").write_text("not a directory")
        self.assertIsNone(self.decide())


class EntryPoint(Base):
    """The real `main()` in a process, as Claude Code runs it, with a stub workspace resolver."""

    def run_hook(self, payload, env_extra):
        repo = Path(self._td.name) / "repo"
        hooks = repo / "skills" / "pending-questions" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        shutil.copy(HOOK, hooks / HOOK.name)
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "workspace_default.py").write_text(
            f"def resolve_workspace(migrate=True):\n    return {str(self.ws)!r}\n")
        env = {k: v for k, v in os.environ.items()
               if k not in ("SUTANDO_CORE_SESSION", "SUTANDO_INSTANCE_ID")}
        env.update(env_extra)
        return subprocess.run([sys.executable, str(hooks / HOOK.name)], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=30)

    def test_core_incident_emits_one_block_decision(self):
        self.write([prompt("p1"), assistant_text(INCIDENT)])
        r = self.run_hook(self.payload(), CORE)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["decision"], "block")
        r = self.run_hook(self.payload(), CORE)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_guest_and_garbage_input_are_silent(self):
        self.write([prompt("p1"), assistant_text(INCIDENT)])
        self.assertEqual(self.run_hook(self.payload(), {}).stdout, "")
        r = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True,
                           text=True, env={**os.environ, **CORE}, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class Declared(unittest.TestCase):
    def test_skill_declares_the_stop_hook(self):
        sys.path.insert(0, str(REPO / "src"))
        from skill_hooks import discover
        self.assertIn(("Stop", HOOK.name), [(e, t) for e, t, _, _ in discover(REPO)])


if __name__ == "__main__":
    unittest.main()
