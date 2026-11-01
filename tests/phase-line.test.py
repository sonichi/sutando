#!/usr/bin/env python3
"""src/phase_line.py: the phase word and status line sidecar for a task's live status.

The rules phase from the tool alone, the sidecar's latest-step rule, the switch and debounce, the
redaction and leak gate, the model line's validation, and the hook writing a record for every bound
tool call. The model is a fake; nothing here reaches the network. In process, so coverage sees it.

Run: python3 tests/phase-line.test.py   (stdlib only)
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import phase_line as pl  # noqa: E402

ON = {"PHASE_MODEL": "vertex", "PHASE_MODEL_PROJECT": "proj-x"}
OFF = {"PHASE_MODEL": "off", "PHASE_MODEL_PROJECT": "proj-x"}


def _task(ws, tid, goal="Rebase https://github.com/o/r/pull/3331 onto main for Alice", members="@alice:s.space"):
    d = Path(ws) / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.txt").write_text(f"id: {tid}\nsender_name: Alice\nroom_members: {members}\ntask: {goal}\n")


def _rows(ws, tid, lines, audience="owner"):
    p = Path(ws) / "state" / "agent-activity.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        for ln in lines:
            f.write(json.dumps({"ts": time.time(), "kind": "working", "line": ln, "audience": audience,
                                "task": {"id": tid}}) + "\n")


class Rules(unittest.TestCase):
    def test_each_tool_maps_to_its_phase(self):
        cases = [("Read", {}, "Reading files"), ("Grep", {}, "Searching code"), ("Glob", {}, "Searching code"),
                 ("WebSearch", {}, "Searching the web"), ("Edit", {}, "Editing code"), ("Write", {}, "Editing code"),
                 ("SendMessage", {}, "Asking someone"), ("Agent", {}, "Asking someone"), ("Monitor", {}, "Waiting"),
                 ("mcp__s-space__room_message_send", {}, "Writing a reply"),
                 ("Bash", {"command": "python3 tests/x.test.py"}, "Running tests"),
                 ("Bash", {"command": "gh pr diff 12"}, "Reviewing changes"),
                 ("Bash", {"command": "python3 room_ops.py say R hi"}, "Writing a reply"),
                 ("Bash", {"command": "sleep 30"}, "Waiting"),
                 ("Bash", {"command": "grep -n foo src"}, "Searching code"),
                 ("Bash", {"command": "sed -n 1,9p a"}, "Reading files"),
                 ("Bash", {"command": "git fetch origin"}, "Running a command"),
                 ("Bash", {"command": "until x; do sleep 1; done", "run_in_background": True}, "Waiting"),
                 ("Bash", {"command": "npm run build", "run_in_background": True}, "Running a command"),
                 ("SomethingNew", {}, "Running a command"), ("", {}, "Thinking")]
        for tool, inp, want in cases:
            self.assertEqual(pl.rules_phase(tool, inp), want, (tool, inp))
            self.assertIn(want, pl.PHASES)


class Sidecar(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def test_the_model_answer_wins_only_for_the_newest_step(self):
        pl.write(self.ws, {"task_id": "task-a", "step": "s1", "phase": "Running a command", "phase_source": "rules"})
        pl.write(self.ws, {"task_id": "task-a", "step": "s1", "phase": "Reviewing changes", "phase_source": "model",
                           "line": "Reviewing PR #12", "line_source": "model"})
        self.assertEqual(pl.latest(self.ws, "task-a"), {"phase": "Reviewing changes", "phase_source": "model",
                                                        "step": "s1", "line": "Reviewing PR #12", "line_source": "model"})
        pl.write(self.ws, {"task_id": "task-a", "step": "s2", "phase": "Editing code", "phase_source": "rules"})
        self.assertEqual(pl.latest(self.ws, "task-a"), {"phase": "Editing code", "phase_source": "rules", "step": "s2"})
        self.assertIsNone(pl.latest(self.ws, "task-other"))

    def test_a_write_that_cannot_happen_returns_false_and_rotation_keeps_one_old_file(self):
        Path(self.ws, "state").mkdir()
        pl.sidecar_path(self.ws).mkdir()
        self.assertFalse(pl.write(self.ws, {"task_id": "t"}))
        ws2 = tempfile.mkdtemp()
        with patch.object(pl, "SIDECAR_MAX_BYTES", 200):
            for i in range(5):
                self.assertTrue(pl.write(ws2, {"task_id": "task-a", "step": str(i), "phase": "Thinking",
                                               "phase_source": "rules"}))
        self.assertTrue(pl.sidecar_path(ws2).with_name(pl.SIDECAR + ".1").exists())

    def test_unreadable_lines_are_skipped(self):
        p = pl.sidecar_path(self.ws)
        p.parent.mkdir(parents=True)
        p.write_text('not json\n{"task_id":"task-a","step":"s","phase":"Waiting","phase_source":"rules","at":1}\n')
        self.assertEqual(pl.latest(self.ws, "task-a")["phase"], "Waiting")


class Switch(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.spawned = []

    def test_off_writes_rules_and_starts_nothing(self):
        with patch.dict(os.environ, OFF):
            self.assertEqual(pl.on_step(self.ws, "task-a", "Read", {}, None, spawn=lambda *a: self.spawned.append(a)),
                             "Reading files")
        self.assertEqual(self.spawned, [])
        self.assertEqual(pl.latest(self.ws, "task-a")["phase"], "Reading files")

    def test_on_without_a_project_is_off(self):
        with patch.dict(os.environ, {"PHASE_MODEL": "vertex", "PHASE_MODEL_PROJECT": ""}):
            self.assertFalse(pl.model_on())

    def test_on_starts_the_model_at_most_once_per_debounce_window(self):
        with patch.dict(os.environ, ON):
            for _ in range(3):
                pl.on_step(self.ws, "task-a", "Bash", {"command": "git fetch"}, "Fetching", spawn=lambda *a: self.spawned.append(a))
            pl.on_step(self.ws, "task-b", "Bash", {"command": "git fetch"}, "Fetching", spawn=lambda *a: self.spawned.append(a))
        self.assertEqual([a[1] for a in self.spawned], ["task-a", "task-b"])

    def test_spawn_is_detached(self):
        with patch.object(pl.subprocess, "Popen") as po:
            pl._spawn(self.ws, "task-a")
        kw = po.call_args.kwargs
        self.assertTrue(kw["start_new_session"])
        self.assertIn("enrich", po.call_args.args[0])


class Redaction(unittest.TestCase):
    def test_ids_links_paths_and_people_become_placeholders_and_prs_come_back(self):
        r = pl.Redactor(["Alice"])
        out = r("Alice asked @alice:s.space in !abc:s.space about https://github.com/o/r/pull/3331 and #12, "
                "see /Users/a/x.py, mail a@b.co, key ghp_abcdefghijklmnop, call +1 415 555 0100")
        for gone in ("Alice", "@alice", "!abc", "github.com", "3331", "/Users", "a@b.co", "ghp_", "415"):
            self.assertNotIn(gone, out)
        self.assertIn("PR1", out)
        self.assertIn("PR2", out)
        self.assertFalse(pl.leaks(out))
        self.assertEqual(r.restore("Rebasing PR1 after PR2"), "Rebasing PR #3331 after PR #12")
        self.assertEqual(r.restore("Rebasing PR9"), "Rebasing PR9")
        self.assertEqual(r("PR #3331 again, PR 12, and PR1"), "PR1 again, PR2, and PR1",
                         "the same number keeps its placeholder; a bare PR1 is left alone")

    def test_the_leak_scan_catches_what_redaction_must_remove(self):
        for raw in ("@x:s.space", "!r:s.space", "https://x.y", "/Users/q", "a@b.co", "sk-abcdefghijk"):
            self.assertTrue(pl.leaks(raw), raw)


class Line(unittest.TestCase):
    def test_validation(self):
        prompt = "Task goal: Rebase PR1\nRecent steps:\n[working] Fetch"
        self.assertEqual(pl.validate_line("Rebasing PR1 onto main.", prompt), "Rebasing PR1 onto main")
        self.assertEqual(pl.validate_line("Asking Person2 to review", prompt), "Asking a teammate to review")
        for bad in ("Rebasing PR7", "x" * 61, "Editing src/app.py", "See https://x", "Working on the request", "", None,
                    "Fixing <path>", "Commit 0123456789abcdef"):
            self.assertIsNone(pl.validate_line(bad, prompt), bad)


class Enrich(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        _task(self.ws, "task-a")
        _rows(self.ws, "task-a", ["Fetch origin", "Rebase onto main for #3331"])
        _rows(self.ws, "task-a", ["Room-only line Bob"], audience="room")
        pl.write(self.ws, {"task_id": "task-a", "step": "Rebase onto main for #3331", "phase": "Running a command",
                           "phase_source": "rules"})
        self.prompts = []

    def _call(self, answer):
        def call(project, model, system, user):
            self.prompts.append((project, model, user))
            return answer
        return call

    def test_a_good_answer_is_written_with_the_pr_number_put_back(self):
        with patch.dict(os.environ, ON):
            rec = pl.enrich(self.ws, "task-a", call=self._call({"phase": "Running a command", "line": "Rebasing PR1 onto main"}))
        self.assertEqual(rec["line"], "Rebasing PR #3331 onto main")
        self.assertEqual(pl.latest(self.ws, "task-a")["line"], "Rebasing PR #3331 onto main")
        project, model, user = self.prompts[0]
        self.assertEqual((project, model), ("proj-x", "gemini-3.5-flash-lite"))
        self.assertNotIn("Alice", user)
        self.assertNotIn("3331", user)
        self.assertNotIn("Room-only", user, "only owner-audience steps are sent")

    def test_a_task_mid_file_sends_only_its_task_line_and_steps_once(self):
        ws = tempfile.mkdtemp()
        d = Path(ws) / "tasks"
        d.mkdir(parents=True)
        (d / "task-m.txt").write_text("id: task-m\ntask: Merge #5 with evidence.\nroom_name: Dev-Alice\n"
                                      "room_members: @alice:s.space\nuser_id: @alice:s.space\n")
        _rows(ws, "task-m", ["Check CI", "Check CI", "Merge #5"])
        pl.write(ws, {"task_id": "task-m", "step": "Merge #5", "phase": "Running a command", "phase_source": "rules"})
        with patch.dict(os.environ, ON):
            pl.enrich(ws, "task-m", call=self._call({}))
        user = self.prompts[0][2]
        self.assertIn("Task goal: Merge PR1 with evidence.", user)
        self.assertNotIn("room_name", user)
        self.assertEqual(user.count("[working] Check CI"), 1)

    def test_a_bad_phase_or_no_answer_leaves_the_rules_record(self):
        with patch.dict(os.environ, ON):
            self.assertIsNone(pl.enrich(self.ws, "task-a", call=self._call({"phase": "Dancing", "line": "x"})))
            self.assertIsNone(pl.enrich(self.ws, "task-a", call=self._call({})))
        self.assertEqual(pl.latest(self.ws, "task-a")["phase_source"], "rules")

    def test_a_bad_line_keeps_the_model_phase_but_no_line(self):
        with patch.dict(os.environ, ON):
            rec = pl.enrich(self.ws, "task-a", call=self._call({"phase": "Running a command", "line": "Editing /etc/x"}))
        self.assertNotIn("line", rec)
        self.assertNotIn("line", pl.latest(self.ws, "task-a"))

    def test_a_leak_after_redaction_skips_the_call(self):
        with patch.dict(os.environ, ON), patch.object(pl, "leaks", return_value=True):
            self.assertIsNone(pl.enrich(self.ws, "task-a", call=self._call({"phase": "Waiting", "line": "x"})))
        self.assertEqual(self.prompts, [])

    def test_off_or_no_record_does_nothing(self):
        with patch.dict(os.environ, OFF):
            self.assertIsNone(pl.enrich(self.ws, "task-a", call=self._call({"phase": "Waiting", "line": "x"})))
        with patch.dict(os.environ, ON):
            self.assertIsNone(pl.enrich(self.ws, "task-none", call=self._call({"phase": "Waiting", "line": "x"})))
        self.assertEqual(self.prompts, [])

    def test_gemini_failures_answer_empty(self):
        with patch.object(pl.subprocess, "run", side_effect=OSError("no gcloud")):
            self.assertEqual(pl.gemini("p", "m", "s", "u"), {})
        fake = type("R", (), {"stdout": "tok"})()
        with patch.object(pl.subprocess, "run", return_value=fake), \
             patch.object(pl.urllib.request, "urlopen", side_effect=pl.urllib.error.URLError("down")):
            self.assertEqual(pl.gemini("p", "m", "s", "u"), {})


class Hook(unittest.TestCase):
    def test_a_bound_read_gets_a_rules_record_without_a_row(self):
        spec = importlib.util.spec_from_file_location("activity_hook", REPO / "skills" / "agent-activity" / "hooks" / "activity-hook.py")
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        ws = Path(tempfile.mkdtemp())
        p = {"ws": ws}
        with patch.object(hook, "bound_task", return_value=({"id": "task-a"}, "!r:x")), \
             patch.dict(os.environ, OFF):
            out = hook.handle({"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Read",
                               "tool_input": {"file_path": "/x"}}, dict(p, bind=ws / "bind.json"), run=lambda *a, **k: None)
        self.assertEqual(out, [])
        self.assertEqual(pl.latest(ws, "task-a")["phase"], "Reading files")

    def test_the_hook_survives_a_broken_phase_module(self):
        spec = importlib.util.spec_from_file_location("activity_hook2", REPO / "skills" / "agent-activity" / "hooks" / "activity-hook.py")
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        with patch.object(pl, "on_step", side_effect=RuntimeError("boom")):
            hook.step_phase(Path(tempfile.mkdtemp()), "task-a", "Read", {}, None)


if __name__ == "__main__":
    unittest.main()
