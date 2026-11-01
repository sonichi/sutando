#!/usr/bin/env python3
"""skills/agent-activity/scripts/phase_line.py: the phase word and status line sidecar.

The rules phase from the tool alone; the sidecar keyed by rules `seq` (a model answer counts only for
the record it was made for); locks that never wait; the switch and debounce; what may be sent (owner
tier only, `working` lines only, no connected-account task, normalised, filtered, redacted, then an
allowlist); one probe per leak class; the model line's own gate; and the hook. The model is a fake;
nothing here reaches the network. In process, so coverage sees it.

Run: python3 tests/skills/agent-activity-phase-line.test.py   (stdlib only)
"""
import importlib.util
import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "skills" / "agent-activity" / "scripts"))
sys.path.insert(0, str(REPO / "src"))
import phase_line as pl  # noqa: E402
from file_lock import locked_file  # noqa: E402

ON = {"PHASE_MODEL": "vertex", "PHASE_MODEL_PROJECT": "proj-x"}
OFF = {"PHASE_MODEL": "off", "PHASE_MODEL_PROJECT": "proj-x"}


def _task(ws, tid, goal="Rebase https://github.com/o/r/pull/3331 onto main", tier="owner", extra=""):
    d = Path(ws) / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.txt").write_text(f"id: {tid}\naccess_tier: {tier}\nsender_name: Alice Example\n"
                                  f"room_members: @alice:s.space, @bob.smith:s.space\n{extra}task: {goal}\n")


def _rows(ws, tid, lines, kind="working"):
    p = Path(ws) / "state" / "agent-activity.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        for ln in lines:
            f.write(json.dumps({"ts": time.time(), "kind": kind, "line": ln, "task": {"id": tid}}) + "\n")


def _rules(ws, tid, phase="Running a command", step=None, **kw):
    pl.write(ws, dict({"task_id": tid, "seq": os.urandom(6).hex(), "step": step, "phase": phase,
                       "phase_source": "rules"}, **kw))
    return pl.latest(ws, tid)["seq"]


def _hold(path, ready, release):
    with locked_file(path):
        ready.set()
        release.wait(5)


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

    def test_a_model_answer_counts_only_for_the_rules_record_it_was_made_for(self):
        old = _rules(self.ws, "task-a", "Reading files")
        pl.write(self.ws, {"task_id": "task-a", "for_seq": old, "phase": "Reviewing changes", "phase_source": "model",
                           "line": "Reviewing PR #12", "line_source": "model"})
        self.assertEqual(pl.latest(self.ws, "task-a")["line"], "Reviewing PR #12")
        new = _rules(self.ws, "task-a", "Searching code")   # a Grep: step None, like the Read before it
        late = {"task_id": "task-a", "for_seq": old, "phase": "Reading files", "phase_source": "model", "line": "Old"}
        pl.write(self.ws, late)                               # the old enrichment lands after the newer step
        self.assertEqual(pl.latest(self.ws, "task-a"),
                         {"phase": "Searching code", "phase_source": "rules", "step": None, "seq": new})
        self.assertIsNone(pl.latest(self.ws, "task-other"))

    def test_a_failed_write_returns_false_and_rotation_keeps_one_old_file(self):
        Path(self.ws, "state").mkdir()
        pl.sidecar_path(self.ws).mkdir()
        self.assertFalse(pl.write(self.ws, {"task_id": "t"}))
        ws2 = tempfile.mkdtemp()
        with patch.object(pl, "SIDECAR_MAX_BYTES", 300):
            for _ in range(6):
                _rules(ws2, "task-a")
        self.assertTrue(pl.sidecar_path(ws2).with_name(pl.SIDECAR + ".1").exists())

    def test_the_reader_takes_only_the_tail_and_skips_bad_lines(self):
        p = pl.sidecar_path(self.ws)
        p.parent.mkdir(parents=True)
        filler = "x" * 200 + "\n"
        p.write_text(filler * 2000 + 'not json\n{"task_id":"task-a","seq":"s1","phase":"Waiting","phase_source":"rules"}\n')
        with patch.object(pl, "TAIL_BYTES", 4096):
            self.assertEqual(pl.latest(self.ws, "task-a")["phase"], "Waiting")


class NeverWaits(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def _held(self, path):
        ctx = multiprocessing.get_context("fork")
        ready, release = ctx.Event(), ctx.Event()
        proc = ctx.Process(target=_hold, args=(path, ready, release))
        proc.start()
        self.assertTrue(ready.wait(5))
        self.addCleanup(lambda: (release.set(), proc.join(5)))
        return release

    def test_a_held_sidecar_lock_skips_the_write_at_once(self):
        self._held(pl.sidecar_path(self.ws).with_name(pl.SIDECAR + ".lock"))
        t0 = time.monotonic()
        with patch.dict(os.environ, OFF):
            self.assertEqual(pl.on_step(self.ws, "task-a", "Read", {}, None), "Reading files")
        self.assertLess(time.monotonic() - t0, 0.5, "the hook waited on the lock")
        self.assertIsNone(pl.latest(self.ws, "task-a"))

    def test_a_held_debounce_lock_skips_the_spawn_at_once(self):
        stamp = Path(self.ws) / "state" / "phase-line" / "task-a.lock"
        self._held(stamp)
        spawned = []
        t0 = time.monotonic()
        with patch.dict(os.environ, ON):
            pl.on_step(self.ws, "task-a", "Read", {}, None, spawn=lambda *a: spawned.append(a))
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(spawned, [])


class Switch(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.spawned = []

    def test_off_writes_rules_and_starts_nothing(self):
        with patch.dict(os.environ, OFF):
            pl.on_step(self.ws, "task-a", "Read", {}, None, spawn=lambda *a: self.spawned.append(a))
        self.assertEqual(self.spawned, [])
        self.assertEqual(pl.latest(self.ws, "task-a")["phase"], "Reading files")

    def test_on_without_a_project_is_off(self):
        with patch.dict(os.environ, {"PHASE_MODEL": "vertex", "PHASE_MODEL_PROJECT": ""}):
            self.assertFalse(pl.model_on())

    def test_on_starts_the_model_at_most_once_per_debounce_window(self):
        with patch.dict(os.environ, ON):
            for _ in range(3):
                pl.on_step(self.ws, "task-a", "Bash", {"command": "git fetch"}, "Fetch", spawn=lambda *a: self.spawned.append(a))
            pl.on_step(self.ws, "task-b", "Bash", {"command": "git fetch"}, "Fetch", spawn=lambda *a: self.spawned.append(a))
        self.assertEqual([a[1] for a in self.spawned], ["task-a", "task-b"])

    def test_a_connected_account_tool_marks_the_task_private(self):
        pl.on_step(self.ws, "task-a", "mcp__sutando-station__composio_exec", {}, None, spawn=lambda *a: None)
        self.assertTrue(pl.records(self.ws, "task-a")[-1]["private"])

    def test_spawn_is_detached(self):
        with patch.object(pl.subprocess, "Popen") as po:
            pl._spawn(self.ws, "task-a")
        self.assertTrue(po.call_args.kwargs["start_new_session"])
        self.assertIn("enrich", po.call_args.args[0])


class Probes(unittest.TestCase):
    """One probe per leak class the review listed: each is masked, or the prompt is refused."""

    def test_each_leak_class_is_masked_or_refused(self):
        r = pl.Redactor(["Qingyun Wu", "alice"])
        probes = {
            "aws secret": "use wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY now",
            "aws key id": "key AKIAIOSFODNN7EXAMPLE",
            "short jwt": "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIx.abc",
            "relative path": "read workspace/notes/x.md",
            "volumes path": "open /Volumes/Data/secret",
            "bare repo host": "see github.com/acme/private-repo",
            "owner/repo": "clone acme/private-repo",
            "room without server": "post in !roomOnly",
            "display name": "reply to Bob Jones about it",
            "first name of sender": "ask Qingyun to approve",
            "cjk name": "ask 张伟 to approve",
            "seven digit phone": "call 5550100",
            "zero-width sk token": "use sk-ab​cdefghijklmnop",
            "password assignment": "set password=hunter2 first",
            "email": "mail bob@example.com",
            "matrix id": "ping @bob:s.space",
            "url": "fetch https://intranet.example/x",
        }
        leaked = {"aws secret": "wJalr", "aws key id": "AKIA", "short jwt": "eyJ", "relative path": "notes",
                  "volumes path": "Volumes", "bare repo host": "acme", "owner/repo": "acme",
                  "room without server": "roomOnly", "display name": "Bob", "first name of sender": "Qingyun",
                  "cjk name": "张伟", "seven digit phone": "5550100", "zero-width sk token": "sk-",
                  "password assignment": "hunter2", "email": "bob@", "matrix id": "@bob", "url": "intranet"}
        for name, text in probes.items():
            out = r(text)
            self.assertTrue(leaked[name] not in out or not pl.allowed(out), f"{name}: {out!r}")

    def test_plain_work_lines_pass_and_prs_come_back(self):
        r = pl.Redactor([])
        out = r("Rebase PR #3331 onto main, then rerun the skipping tests")
        self.assertEqual(out, "Rebase PR1 onto main, then rerun the skipping tests")
        self.assertTrue(pl.allowed(out))
        self.assertEqual(r.restore("Rebasing PR1"), "Rebasing PR #3331")
        self.assertEqual(r("PR #3331 again and #12"), "PR1 again and PR2")


class Line(unittest.TestCase):
    def test_the_line_passes_the_same_gate(self):
        prompt = "Task goal: Rebase PR1\nRecent steps:\n[working] Fetch"
        self.assertEqual(pl.validate_line("Rebasing PR1 onto main.", prompt), "Rebasing PR1 onto main")
        self.assertEqual(pl.validate_line("Asking Person2 to review", prompt), "Asking a teammate to review")
        for bad in ("Rebasing PR7", "x" * 61, "Editing src/app.py", "See https://x", "Working on the request", "", None,
                    "Fixing <path>", "Commit 0123456789abcdef", "Using sk-abcdefghijklmnop", "Asking Bob to review",
                    "Asking 张伟", "Paste token=abc"):
            self.assertIsNone(pl.validate_line(bad, prompt), bad)


class Enrich(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        _task(self.ws, "task-a")
        _rows(self.ws, "task-a", ["Fetch origin", "Fetch origin", "Rebase onto main for #3331"])
        _rows(self.ws, "task-a", ["Mail from Bob about the offer"], kind="thinking")
        self.seq = _rules(self.ws, "task-a", step="Rebase onto main for #3331")
        self.prompts = []

    def _call(self, *answers):
        answers = list(answers)

        def call(project, model, system, user):
            self.prompts.append((project, model, user))
            return answers.pop(0) if answers else {}
        return call

    def test_a_good_answer_is_written_for_its_rules_record_with_the_pr_back(self):
        with patch.dict(os.environ, ON):
            rec = pl.enrich(self.ws, "task-a", call=self._call({"phase": "Running a command", "line": "Rebasing PR1 onto main"}))
        self.assertEqual((rec["for_seq"], rec["line"]), (self.seq, "Rebasing PR #3331 onto main"))
        self.assertEqual(pl.latest(self.ws, "task-a")["line"], "Rebasing PR #3331 onto main")
        user = self.prompts[0][2]
        self.assertNotIn("3331", user)
        self.assertNotIn("Mail", user, "thinking rows are never sent")
        self.assertEqual(user.count("[working] Fetch origin"), 1)
        self.assertTrue(pl.allowed(user))

    def test_a_newer_step_during_the_call_gets_its_own_answer(self):
        def call(project, model, system, user):
            self.prompts.append(user)
            if len(self.prompts) == 1:
                _rules(self.ws, "task-a", "Editing code", step="Edit the rebase notes")
            return {"phase": "Editing code" if len(self.prompts) > 1 else "Running a command", "line": "Rebasing PR1"}
        with patch.dict(os.environ, ON):
            rec = pl.enrich(self.ws, "task-a", call=call)
        now = pl.latest(self.ws, "task-a")
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual((rec["for_seq"], now["phase"], now["phase_source"]), (now["seq"], "Editing code", "model"))

    def test_a_bad_phase_writes_nothing_and_a_bad_line_keeps_only_the_phase(self):
        with patch.dict(os.environ, ON):
            self.assertIsNone(pl.enrich(self.ws, "task-a", call=self._call({"phase": "Dancing", "line": "x"})))
            self.assertEqual(pl.latest(self.ws, "task-a")["phase_source"], "rules")
            rec = pl.enrich(self.ws, "task-a", call=self._call({"phase": "Running a command", "line": "Using sk-abcdefghijklmnop"}))
        self.assertNotIn("line", rec)
        self.assertEqual(pl.latest(self.ws, "task-a")["phase_source"], "model")
        self.assertNotIn("line", pl.latest(self.ws, "task-a"))

    def test_nothing_is_sent_for_tasks_that_must_not_leave(self):
        cases = {"task-room": dict(tier="team"), "task-collab": dict(extra="collaborator: true\n"),
                 "task-cjk": dict(goal="ask 张伟 to approve")}
        for tid, kw in cases.items():
            _task(self.ws, tid, **kw)
            _rules(self.ws, tid)
        _task(self.ws, "task-private")
        pl.on_step(self.ws, "task-private", "mcp__claude_ai_Gmail__search", {}, None, spawn=lambda *a: None)
        _rules(self.ws, "task-private")
        with patch.dict(os.environ, ON):
            for tid in list(cases) + ["task-private", "task-missing"]:
                self.assertIsNone(pl.enrich(self.ws, tid, call=self._call({"phase": "Waiting", "line": "x"})), tid)
        self.assertEqual(self.prompts, [])

    def test_off_does_nothing(self):
        with patch.dict(os.environ, OFF):
            self.assertIsNone(pl.enrich(self.ws, "task-a", call=self._call({"phase": "Waiting", "line": "x"})))
        self.assertEqual(self.prompts, [])

    def test_gemini_failures_answer_empty(self):
        import urllib.error
        import urllib.request
        with patch.object(pl.subprocess, "run", side_effect=OSError("no gcloud")):
            self.assertEqual(pl.gemini("p", "m", "s", "u"), {})
        fake = type("R", (), {"stdout": "tok"})()
        with patch.object(pl.subprocess, "run", return_value=fake), \
             patch.object(urllib.request, "urlopen", side_effect=urllib.error.URLError("down")):
            self.assertEqual(pl.gemini("p", "m", "s", "u"), {})


class Hook(unittest.TestCase):
    def _hook(self):
        spec = importlib.util.spec_from_file_location("activity_hook_pl", REPO / "skills" / "agent-activity" / "hooks" / "activity-hook.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_a_bound_read_gets_a_rules_record_without_a_row(self):
        hook = self._hook()
        ws = Path(tempfile.mkdtemp())
        with patch.object(hook, "bound_task", return_value=({"id": "task-a"}, "!r:x")), patch.dict(os.environ, OFF):
            out = hook.handle({"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Read",
                               "tool_input": {"file_path": "/x"}}, {"ws": ws, "bind": ws / "bind.json"},
                              run=lambda *a, **k: None)
        self.assertEqual(out, [])
        self.assertEqual(pl.latest(ws, "task-a")["phase"], "Reading files")

    def test_the_hook_survives_a_broken_phase_module(self):
        hook = self._hook()
        with patch.object(pl, "on_step", side_effect=RuntimeError("boom")):
            hook.step_phase(Path(tempfile.mkdtemp()), "task-a", "Read", {}, None)


if __name__ == "__main__":
    unittest.main()
