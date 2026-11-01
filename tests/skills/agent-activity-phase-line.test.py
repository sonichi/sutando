#!/usr/bin/env python3
"""skills/agent-activity/scripts/phase_line.py: the phase word and status line sidecar.

The rules phase from the tool alone; the sidecar keyed by rules `seq` (a model answer counts only for
the record it was made for); locks that never wait; the switch and debounce; what may be sent (owner
tier only, `working` lines only, no connected-account task, normalised, filtered, redacted, then an
allowlist); one probe per leak class; the model line's own gate; and the hook. The model is a fake;
nothing here reaches the network. In process, so coverage sees it.

Run: python3 tests/skills/agent-activity-phase-line.test.py   (stdlib only)
"""
import contextlib
import importlib.util
import io
import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

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
    """A rules record as on_step writes it: the task's first one says so, beside its marker directory."""
    if not pl.records(ws, tid):
        kw.setdefault("first", True)
        pl._marker_dir(ws).mkdir(parents=True, exist_ok=True)
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
        pl._TOKEN.clear()
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


class Edges(unittest.TestCase):
    """The defensive branches: each degrades to the rules record, never raises."""

    def setUp(self):
        pl._TOKEN.clear()
        self.ws = tempfile.mkdtemp()

    def test_unreadable_settings_and_stamps_read_as_off(self):
        with patch.dict(sys.modules, {"skill_config": None}):
            self.assertEqual(pl.setting("PHASE_MODEL", "off"), "off")
        with patch.object(pl, "_try_locked", side_effect=OSError("x")):
            self.assertFalse(pl._debounced(self.ws, "task-a"))

    def test_without_the_shared_filters_the_allowlist_still_decides(self):
        import importlib
        with patch.dict(sys.modules, {"chat_secret_filter": None}):
            mod = importlib.reload(pl)
            out = mod.Redactor([])("mail bob@example.com about PR #3")
        importlib.reload(pl)
        self.assertEqual(out, "mail <email> about PR1")
        self.assertTrue(pl.allowed(out))

    def test_gemini_parses_an_answer_and_refuses_an_empty_token(self):
        import urllib.request
        empty = type("R", (), {"stdout": ""})()
        with patch.object(pl.subprocess, "run", return_value=empty):
            self.assertEqual(pl.gemini("p", "m", "s", "u"), {})
        tok = type("R", (), {"stdout": "tok"})()
        body = json.dumps({"candidates": [{"content": {"parts": [{"text": '{"phase": "Waiting", "line": "x"}'}]}}]})

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body.encode()
        with patch.object(pl.subprocess, "run", return_value=tok), patch.object(urllib.request, "urlopen", return_value=Resp()):
            self.assertEqual(pl.gemini("p", "m", "s", "u"), {"phase": "Waiting", "line": "x"})

    def test_a_task_mid_file_sends_only_its_task_line(self):
        d = Path(self.ws) / "tasks"
        d.mkdir(parents=True)
        (d / "task-m.txt").write_text("id: task-m\naccess_tier: owner\ntask: Merge #5 with evidence.\n"
                                      "room_name: Dev\nuser_id: @alice:s.space\n")
        goal, _people = pl._task_context(self.ws, "task-m")
        self.assertEqual(goal, "Merge #5 with evidence.")
        self.assertEqual(pl._task_context(self.ws, "task-none"), (None, []))
        with patch("local_task_protocol.find_archived_task", side_effect=RuntimeError("x")):
            self.assertEqual(pl._task_context(self.ws, "task-m"), (None, []))

    def test_steps_tolerate_a_missing_log_and_bad_lines(self):
        self.assertEqual(pl._steps(self.ws, "task-a"), [])
        p = Path(self.ws) / "state" / "agent-activity.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text("not json\n" + json.dumps({"kind": "working", "line": "Fetch", "task": {"id": "task-a"}}) + "\n")
        self.assertEqual(pl._steps(self.ws, "task-a"), ["[working] Fetch"])

    def test_enrich_stops_when_its_answer_is_current_or_the_record_is_gone(self):
        _task(self.ws, "task-a")
        _rows(self.ws, "task-a", ["Fetch origin"])
        _rules(self.ws, "task-a")
        calls = []

        def call(*a):
            calls.append(1)
            _rules(self.ws, "task-a")          # a newer step lands during every call
            return {"phase": "Waiting", "line": "Fetching"}
        with patch.dict(os.environ, ON):
            rec = pl.enrich(self.ws, "task-a", call=call)
        self.assertEqual(len(calls), 1 + pl.RERUNS, "bounded re-runs")
        self.assertIsNotNone(rec)
        with patch.dict(os.environ, ON), patch.object(pl, "latest", side_effect=[{"seq": "s", "phase": "Waiting"}, None]), \
                patch.object(pl, "prompt_for", return_value=None):
            self.assertIsNone(pl.enrich(self.ws, "task-a", call=call))

    def test_the_cli_prints_latest_and_runs_enrich(self):
        _rules(self.ws, "task-a", "Waiting")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pl.main(["latest", "--task", "task-a", "--workspace", self.ws]), 0)
        self.assertEqual(json.loads(out.getvalue())["phase"], "Waiting")
        with patch.dict(os.environ, OFF):
            self.assertEqual(pl.main(["enrich", "--task", "task-a", "--workspace", self.ws]), 0)
        with patch("workspace_default.resolve_workspace", return_value=Path(self.ws)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pl.main(["latest", "--task", "task-a"]), 0)


class WhyNotMoving(unittest.TestCase):
    """status_for: every open or waiting task has a reason, and no age is baked into the line."""

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.tasks = Path(self.ws) / "tasks"
        self.tasks.mkdir(parents=True)

    def _pending(self, tid, at):
        p = self.tasks / f"{tid}.txt"
        p.write_text(f"id: {tid}\ntimestamp: 2026-11-01T00:00:00Z\ntask: hi\n")
        os.utime(p, (at, at))

    def test_queued_tasks_say_how_many_are_ahead(self):
        t0 = time.time() - 600
        self._pending("task-q1", t0)
        self._pending("task-q2", t0 + 1)
        first, second = pl.status_for(self.ws, "task-q1"), pl.status_for(self.ws, "task-q2")
        self.assertEqual((first["phase"], first["line"]), ("Waiting", "Queued · next"))
        self.assertEqual(second["line"], "Queued · 1 ahead")
        self.assertNotRegex(second["line"], r"\d+\s*(s|m|h|min)\b", "no age in the line: the client renders it")
        self.assertIsInstance(second["since"], float)

    def test_a_queued_task_behind_another_request_says_so_without_naming_it(self):
        self._pending("task-q1", time.time() - 60)
        _rules(self.ws, "task-busy", "Running tests", step="Run the secret-project suite")
        st = pl.status_for(self.ws, "task-q1")
        self.assertEqual((st["phase"], st["line"]), ("Waiting", "Waiting: working on another request"))
        self.assertNotIn("secret", json.dumps(st))
        self.assertEqual(pl.status_for(self.ws, "task-q1", active="task-busy")["line"], "Waiting: working on another request")
        self.assertEqual(pl.status_for(self.ws, "task-q1", active="task-q1")["line"], "Queued · next")
        old = Path(self.ws)
        with patch.object(pl, "BUSY_WINDOW_S", -1):
            self.assertEqual(pl.status_for(old, "task-q1")["line"], "Queued · next", "a stale step elsewhere is not busy")

    def test_a_running_task_shows_its_step_with_the_steps_own_start(self):
        _rules(self.ws, "task-r", "Running tests", step="Run the phase tests")
        st = pl.status_for(self.ws, "task-r")
        self.assertEqual((st["phase"], st["line"]), ("Running tests", "Run the phase tests"))
        self.assertAlmostEqual(st["since"], pl.records(self.ws, "task-r")[-1]["at"], places=2)
        _rules(self.ws, "task-n", "Reading files")
        self.assertEqual(pl.status_for(self.ws, "task-n")["line"], "Reading files", "no step text: the phase word")

    def test_a_delegated_task_says_who_has_it(self):
        fake = type("H", (), {"delegated_status": staticmethod(lambda ws, tid, now=None: {"line": "Delegated: a worker"}),
                              "open_handoffs": staticmethod(lambda ws, tid, now=None: [{"at": "2026-11-01T00:00:00Z"}]),
                              "_epoch": staticmethod(lambda at: 1793491200.0)})
        with patch.dict(sys.modules, {"handoffs": fake}):
            st = pl.status_for(self.ws, "task-d")
        self.assertEqual(st, {"phase": "Delegated", "line": "Delegated: a worker", "since": 1793491200.0,
                              "waiting": {"reason": "delegated"}})

    def test_picked_up_without_a_step_reads_the_request_and_finished_shows_nothing(self):
        idx = Path(self.ws) / "state" / "agent-activity.index.json"
        idx.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        idx.write_text(json.dumps({"task-p": {"started": now - 30}, "task-old": {"started": now - 86400}}))
        self.assertEqual(pl.status_for(self.ws, "task-p")["line"], "Reading the request")
        self.assertIsNone(pl.status_for(self.ws, "task-old"), "the index is never pruned: old entries are not open")
        self.assertIsNone(pl.status_for(self.ws, "task-unknown"))

    def test_a_pool_workers_own_inbox_is_its_queue(self):
        inbox = Path(self.ws) / "deliveries" / "w1"
        inbox.mkdir(parents=True)
        for i, tid in enumerate(("task-w1", "task-w2")):
            p = inbox / f"{tid}.txt"
            p.write_text(f"id: {tid}\ntask: hi\n")
            os.utime(p, (time.time() - 100 + i, time.time() - 100 + i))
        self.assertEqual(pl.status_for(self.ws, "task-w2", inbox=inbox)["line"], "Queued · 1 ahead")
        self.assertIsNone(pl.status_for(self.ws, "task-w2"), "not in the core's queue")

    def test_unreadable_inputs_degrade_quietly(self):
        with patch("task_queue.position", side_effect=OSError("x")), patch("activity_rows.open_task_index", side_effect=OSError("x")):
            self.assertIsNone(pl.status_for(self.ws, "task-x"))
        self.assertFalse(pl._recent_other_task(tempfile.mkdtemp(), "task-x", time.time()))

    def test_a_bad_sidecar_line_or_a_queue_read_error_is_tolerated(self):
        p = pl.sidecar_path(self.ws)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json\n")
        self.assertFalse(pl._recent_other_task(self.ws, "task-x", time.time()))
        self._pending("task-q1", time.time() - 60)
        import task_queue
        real, calls = task_queue.pending, []

        def first_then_fail(*a, **k):
            calls.append(1)
            if len(calls) > 1:
                raise OSError("x")
            return real(*a, **k)
        with patch.object(task_queue, "pending", side_effect=first_then_fail):
            st = pl.status_for(self.ws, "task-q1", now=123.0)
        self.assertEqual((st["line"], st["since"]), ("Queued · next", 123.0))

    def test_the_cli_prints_the_status(self):
        _rules(self.ws, "task-r", "Waiting", step="Wait for CI")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pl.main(["status", "--task", "task-r", "--workspace", self.ws])
        self.assertEqual(json.loads(out.getvalue())["line"], "Wait for CI")


class Published(unittest.TestCase):
    """The server ledger gets the Basic fields only: the phase word or a fixed waiting line, never step text."""

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        (Path(self.ws) / "tasks").mkdir(parents=True)

    def _pending(self, tid, at):
        p = Path(self.ws) / "tasks" / f"{tid}.txt"
        p.write_text(f"id: {tid}\ntimestamp: 2026-11-01T00:00:00Z\ntask: hi\n")
        os.utime(p, (at, at))

    def test_a_running_task_publishes_its_phase_word_not_its_step(self):
        _rules(self.ws, "task-r", "Running tests", step="Run the suite for Alice's mail import")
        pl.write(self.ws, {"task_id": "task-r", "for_seq": pl.latest(self.ws, "task-r")["seq"], "phase": "Running tests",
                           "phase_source": "model", "line": "Testing the mail import", "line_source": "model"})
        out = pl.published_status(self.ws, "task-r", share_line=False)
        self.assertEqual((out["phase"], out["summary"]), ("Running tests", "Running tests"))
        self.assertEqual(set(out), {"phase", "summary", "since"})
        self.assertNotIn("mail", json.dumps(out))

    def test_waiting_tasks_publish_the_fixed_waiting_line_and_fields(self):
        t0 = time.time() - 600
        self._pending("task-q1", t0)
        self._pending("task-q2", t0 + 1)
        for share in (True, False):
            out = pl.published_status(self.ws, "task-q2", share_line=share)
            self.assertEqual((out["summary"], out["waiting"]), ("Queued · 1 ahead", {"reason": "queued", "position": 2}))
        _rules(self.ws, "task-busy", "Running tests", step="Run the secret-project suite")
        out = pl.published_status(self.ws, "task-q1")
        self.assertEqual((out["summary"], out["waiting"]), ("Waiting: working on another request", {"reason": "busy"}))
        self.assertNotIn("secret", json.dumps(out))

    def test_a_delegated_task_publishes_no_delegate_name(self):
        fake = type("H", (), {"delegated_status": staticmethod(lambda ws, tid, now=None: {"line": "Delegated: Engine 2"}),
                              "open_handoffs": staticmethod(lambda ws, tid, now=None: [{"at": "2026-11-01T00:00:00Z"}]),
                              "_epoch": staticmethod(lambda at: 1793491200.0)})
        with patch.dict(sys.modules, {"handoffs": fake}):
            outs = [pl.published_status(self.ws, "task-d", share_line=share) for share in (True, False)]
        for out in outs:
            self.assertEqual(out, {"phase": "Delegated", "summary": "Delegated", "since": 1793491200.0,
                                   "waiting": {"reason": "delegated"}})

    def test_sharing_on_publishes_the_scrubbed_line(self):
        _task(self.ws, "task-s")
        pl.on_step(self.ws, "task-s", "Bash", {}, "Run the tests again", spawn=lambda *a: None)
        self.assertEqual(pl.published_status(self.ws, "task-s")["summary"], "Run the tests again")
        _task(self.ws, "task-m")
        pl.on_step(self.ws, "task-m", "mcp__claude_ai_Gmail__search", {}, "Search mail from Dana", spawn=lambda *a: None)
        self.assertEqual(pl.published_status(self.ws, "task-m")["summary"], "Checking your email")

    def test_nothing_for_a_finished_task_and_the_cli(self):
        self.assertIsNone(pl.published_status(self.ws, "task-unknown"))
        _rules(self.ws, "task-r", "Reading files")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pl.main(["published", "--task", "task-r", "--workspace", self.ws])
        self.assertEqual(json.loads(buf.getvalue())["summary"], "Reading files")


class Shareable(unittest.TestCase):
    """The opt-in share of the detail line: an account kind for connected-account tasks, the phase
    word whenever anything in the line would need masking."""

    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def _step(self, tid, tool, step, goal="Sort out the release"):
        _task(self.ws, tid, goal=goal)
        pl.on_step(self.ws, tid, tool, {}, step, spawn=lambda *a: None)
        return pl.shareable_line(self.ws, tid)

    def test_connected_account_steps_say_only_the_kind_of_account(self):
        self.assertEqual(self._step("task-m", "mcp__claude_ai_Gmail__search", "Search mail from Dana about the offer"),
                         "Checking your email")
        self.assertEqual(self._step("task-c", "mcp__claude_ai_Google_Calendar__list_events", "List Friday with Dana"),
                         "Looking at your calendar")
        pl.on_step(self.ws, "task-m", "Read", {}, "Read the offer letter", spawn=lambda *a: None)
        self.assertEqual(pl.shareable_line(self.ws, "task-m"), "Checking your email", "the whole task stays private")
        self.assertEqual(pl.account_kind("mcp__sutando-station__composio_exec"), "app")
        self.assertEqual(pl.account_kind("mcp__claude_ai_Notion__search"), "files")

    def test_an_evicted_private_record_still_shares_no_detail(self):
        _task(self.ws, "task-e")
        pl._marker_dir(self.ws).mkdir(parents=True, exist_ok=True)
        pl._marker(self.ws, "task-e", "private").write_text("")
        _rules(self.ws, "task-e", "Reading files", step="Read the offer letter")
        self.assertEqual(pl.shareable_line(self.ws, "task-e"), "Using a connected app")

    def test_a_name_an_email_or_a_token_falls_back_to_the_phase_word(self):
        for tid, step in (("task-n", "Ask Dana to review"), ("task-k", "Reply to alice about it"),
                          ("task-x", "Mail bob@example.com the notes"), ("task-t", "Use ghp_abcdefghijklmnop1234 now")):
            self.assertEqual(self._step(tid, "Bash", step), "Running a command", step)

    def test_a_plain_line_and_the_fixed_lines_pass(self):
        self.assertEqual(self._step("task-p", "Bash", "Run the tests again"), "Run the tests again")
        self.assertIsNone(pl.shareable_line(self.ws, "task-none"))
        self.assertEqual(pl.shareable_line(self.ws, "task-none", {"phase": "Waiting", "line": "Queued · next",
                                                                    "waiting": {"reason": "queued", "position": 1}}),
                         "Queued · next")
        self.assertEqual(pl.shareable_line(self.ws, "task-none", {"phase": "Delegated", "line": "Delegated: Engine 2",
                                                                    "waiting": {"reason": "delegated"}}), "Delegated")


class PrivacyKept(unittest.TestCase):
    """A connected-account task stays unsendable after its private record leaves the sidecar's tail,
    and when its marker cannot be written."""

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        _task(self.ws, "task-p")
        _rows(self.ws, "task-p", ["Rebase onto main"])

    def _private_step_then_busy_host(self, **patches):
        with patch.dict(os.environ, ON):
            pl.on_step(self.ws, "task-p", "Read", {}, None, spawn=lambda *a: None)
            pl.on_step(self.ws, "task-p", "mcp__claude_ai_Gmail__search", {}, None, spawn=lambda *a: None)
            for i in range(60):  # other tasks fill the shared sidecar
                pl.on_step(self.ws, f"task-o{i}", "Read", {}, None, spawn=lambda *a: None)
            pl.on_step(self.ws, "task-p", "Bash", {"command": "git fetch"}, "Fetch", spawn=lambda *a: None)

    def test_the_flag_survives_the_tail_dropping_it(self):
        with patch.object(pl, "TAIL_BYTES", 2048):
            self._private_step_then_busy_host()
            self.assertFalse(any(r.get("private") for r in pl.records(self.ws, "task-p")), "precondition: evicted")
            self.assertIsNone(pl.prompt_for(self.ws, "task-p"))

    def test_the_flag_survives_rotation(self):
        with patch.object(pl, "SIDECAR_MAX_BYTES", 4096):
            self._private_step_then_busy_host()
            self.assertTrue(pl.sidecar_path(self.ws).with_name(pl.SIDECAR + ".1").exists(), "precondition: rotated")
            self.assertIsNone(pl.prompt_for(self.ws, "task-p"))

    def test_a_failed_marker_write_fails_closed(self):
        Path(self.ws, "state").mkdir(exist_ok=True)
        Path(self.ws, "state", "phase-line").write_text("not a directory")  # no marker can be written
        with patch.object(pl, "TAIL_BYTES", 2048):
            self._private_step_then_busy_host()
            self.assertIsNone(pl.prompt_for(self.ws, "task-p"))

    def test_a_task_whose_history_is_out_of_sight_is_not_sent(self):
        with patch.object(pl, "TAIL_BYTES", 2048), patch.dict(os.environ, ON):
            pl.on_step(self.ws, "task-p", "Read", {}, None, spawn=lambda *a: None)
            for i in range(60):
                pl.on_step(self.ws, f"task-o{i}", "Read", {}, None, spawn=lambda *a: None)
            pl.on_step(self.ws, "task-p", "Read", {}, None, spawn=lambda *a: None)
            self.assertIsNone(pl.prompt_for(self.ws, "task-p"), "its first step is out of sight: unknown, not sent")

    def test_a_plain_task_in_sight_is_still_sent(self):
        with patch.dict(os.environ, ON):
            pl.on_step(self.ws, "task-p", "Read", {}, None, spawn=lambda *a: None)
        self.assertIsNotNone(pl.prompt_for(self.ws, "task-p"))


class SentenceStartNames(unittest.TestCase):
    """An unknown name is masked wherever it stands, and a line naming anyone is refused."""

    def test_prompt_boundary(self):
        r = pl.Redactor(["alice", "Bob Smith"])
        for text, name in (("Zelda wants the deploy fixed", "Zelda"), ("[working] Kofi reviewed the diff", "Kofi"),
                           ("Done. Priya asked for a rerun", "Priya"), ("Ngozi: please rebase", "Ngozi"),
                           ("alice wants it merged", "alice"), ("ping bob smith about it", "bob smith")):
            out = r(text)
            self.assertNotIn(name, out, text)
        self.assertEqual(r("Please rebase PR #3, then fix the tests"), "Please rebase PR1, then fix the tests")

    def test_prompt_for_masks_a_sentence_initial_name_in_goal_and_steps(self):
        ws = tempfile.mkdtemp()
        _task(ws, "task-n", goal="Zelda asked to rebase PR #3")
        _rows(ws, "task-n", ["Kofi branch rebased"])
        with patch.dict(os.environ, ON):
            pl.on_step(ws, "task-n", "Read", {}, None, spawn=lambda *a: None)
        prompt, _red = pl.prompt_for(ws, "task-n")
        self.assertNotIn("Zelda", prompt)
        self.assertNotIn("Kofi", prompt)

    def test_line_boundary(self):
        prompt = "Task goal: Rebase PR1\nRecent steps:\n[working] Fetch"
        for bad in ("Zelda is rebasing PR1", "Kofi rebasing PR1", "Reviewing alice changes", "Asking bob smith"):
            self.assertIsNone(pl.validate_line(bad, prompt, people=["alice", "Bob Smith"]), bad)
        for good in ("Rebasing PR1 onto main", "Reviewing the diff", "Waiting for the tests"):
            self.assertEqual(pl.validate_line(good, prompt, people=["alice"]), good)


class Token(unittest.TestCase):
    """The access token: fetched within the call's budget, kept in memory until it expires."""

    def setUp(self):
        pl._TOKEN.clear()

    def _resp(self):
        body = json.dumps({"candidates": [{"content": {"parts": [{"text": '{"phase": "Waiting", "line": "x"}'}]}}]})

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body.encode()
        return Resp()

    def test_one_fetch_per_process_until_expiry_within_the_budget(self):
        import urllib.request
        tok = type("R", (), {"stdout": "tok"})()
        with patch.object(pl.subprocess, "run", return_value=tok) as run, \
             patch.object(urllib.request, "urlopen", side_effect=lambda *a, **k: self._resp()):
            pl.gemini("p", "m", "s", "u")
            pl.gemini("p", "m", "s", "u")
            self.assertEqual(run.call_count, 1)
            self.assertLessEqual(run.call_args.kwargs["timeout"], pl.TIMEOUT_S)
            with patch.object(pl.time, "monotonic", return_value=time.monotonic() + pl.TOKEN_TTL_S + 1):
                pl.gemini("p", "m", "s", "u")
            self.assertEqual(run.call_count, 2)

    def test_a_rejected_token_is_dropped_and_fetched_again_once(self):
        import urllib.error
        import urllib.request
        tok = type("R", (), {"stdout": "tok"})()
        calls = []

        def urlopen(req, timeout=None):
            calls.append(req.headers.get("Authorization"))
            if len(calls) == 1:
                raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
            return self._resp()
        with patch.object(pl.subprocess, "run", return_value=tok) as run, patch.object(urllib.request, "urlopen", urlopen):
            self.assertEqual(pl.gemini("p", "m", "s", "u"), {"phase": "Waiting", "line": "x"})
        self.assertEqual((run.call_count, len(calls)), (2, 2))

    def test_a_rejected_token_with_no_fresh_one_and_other_http_errors_answer_empty(self):
        import urllib.error
        import urllib.request
        answers = iter([type("R", (), {"stdout": "tok"})(), type("R", (), {"stdout": ""})()])
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with patch.object(pl.subprocess, "run", side_effect=lambda *a, **k: next(answers)), \
             patch.object(urllib.request, "urlopen", side_effect=err):
            self.assertEqual(pl.gemini("p", "m", "s", "u"), {})
        pl._TOKEN.clear()
        tok = type("R", (), {"stdout": "tok"})()
        with patch.object(pl.subprocess, "run", return_value=tok) as run, \
             patch.object(urllib.request, "urlopen", side_effect=urllib.error.HTTPError("u", 500, "x", {}, None)):
            self.assertEqual(pl.gemini("p", "m", "s", "u"), {})
        self.assertEqual(run.call_count, 1, "only a rejected token is fetched again")


class Markers(unittest.TestCase):
    def test_old_markers_are_pruned_when_a_task_starts(self):
        ws = tempfile.mkdtemp()
        d = pl._marker_dir(ws)
        d.mkdir(parents=True)
        old, kept = d / "task-old.private", d / "task-new.last"
        old.write_text(""); kept.write_text("")
        past = time.time() - pl.MARKER_KEEP_S - 60
        os.utime(old, (past, past))
        pl.on_step(ws, "task-a", "Read", {}, None, spawn=lambda *a: None)
        self.assertEqual((old.exists(), kept.exists()), (False, True))
        pl._prune(Path(ws) / "missing")  # an absent directory is nothing to prune

    def test_a_first_record_that_did_not_land_leaves_the_next_step_first(self):
        ws = tempfile.mkdtemp()
        with patch.object(pl, "write", return_value=False):
            pl.on_step(ws, "task-a", "Read", {}, None, spawn=lambda *a: None)
        self.assertFalse(pl._marker(ws, "task-a", "seen").exists())
        pl.on_step(ws, "task-a", "Read", {}, None, spawn=lambda *a: None)
        self.assertTrue(pl.records(ws, "task-a")[-1].get("first"))


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
