#!/usr/bin/env python3
"""The wedge remedies and readers refuse rather than guess.

A timer with nobody watching runs these, so every expected failure is a value:
a paused or unrecorded worker is not restarted, an unread pane authorises no
kill, a kill or a tmux start that fails is reported, and an unreadable queue or
pane is None (not evidence), never False.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rem = _load("pool_remedy")
sup, ps, wi = rem.sup, rem.ps, rem.wi

WID = "w1"
SOCK = "/tmp/pool-wedge-refusals.sock"
FOOTER = "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
WORKING = f"✻ Thinking… (12s · esc to interrupt)\n❯ \n{FOOTER}\n"


class Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Runner:
    """Answers tmux by subcommand: `answers` maps a subcommand to a Done or an exception."""

    def __init__(self, **answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        sub = next((a for a in argv if a in ("capture-pane", "kill-session", "has-session",
                                             "new-session")), None)
        got = self.answers.get(sub.replace("-", "_") if sub else "", Done(0))
        if isinstance(got, BaseException):
            raise got
        return got


def pool(with_run=True):
    ws = Path(tempfile.mkdtemp(prefix="pool-wedge-refusals-"))
    (ws / "state").mkdir()
    (ws / "state" / "roster.json").write_text(json.dumps(
        {"workers": {WID: {"state": "live", "label": WID}}}))
    wi.record_session(ws, WID, "s1", runtime="claude", relation=wi.RELATION_NEW)
    if with_run:
        wi.start_incarnation(ws, WID, "s1", tmux_socket=SOCK,
                             tmux_session=wi.tmux_session_name(WID))
    return ws


def never_spawn(*a, **k):
    raise AssertionError("spawn must not run")


class RestartRefuses(unittest.TestCase):
    def outcome(self, ws, runner):
        return rem.restart_wedged(ws, REPO, WID, runner=runner, spawn=never_spawn)["outcome"]

    def test_paused(self):
        ws = pool()
        (wi.worker_dir(ws, WID) / sup.PAUSED_MARKER).touch()
        self.assertEqual(self.outcome(ws, Runner()), rem.PAUSED)

    def test_no_recorded_run(self):
        self.assertEqual(self.outcome(pool(with_run=False), Runner()), rem.NO_SESSION)

    def test_pane_unread_authorises_no_kill(self):
        r = Runner(capture_pane=Done(1, err="no server running"))
        self.assertEqual(self.outcome(pool(), r), rem.INDETERMINATE)
        self.assertFalse([c for c in r.calls if "kill-session" in c])

    def test_kill_that_raises(self):
        r = Runner(capture_pane=Done(0, WORKING), kill_session=OSError("tmux gone"))
        self.assertEqual(self.outcome(pool(), r), rem.FAILED)

    def test_kill_that_fails(self):
        r = Runner(capture_pane=Done(0, WORKING), kill_session=Done(1, err="denied"))
        self.assertEqual(self.outcome(pool(), r), rem.FAILED)


class InputWatchRefuses(unittest.TestCase):
    def outcome(self, ws, runner):
        return rem.ensure_input_watch(ws, REPO, WID, runner=runner)["outcome"]

    def test_no_recorded_run(self):
        self.assertEqual(self.outcome(pool(with_run=False), Runner()), rem.NOT_RUNNING)

    def test_an_unanswerable_probe_starts_nothing(self):
        r = Runner(has_session=OSError("no tmux"))
        self.assertEqual(self.outcome(pool(), r), rem.NOT_RUNNING)
        self.assertFalse([c for c in r.calls if "new-session" in c])

    def test_a_start_that_raises(self):
        def call(argv, **kw):
            if "has-session" in argv:
                return Done(0 if not argv[-1].endswith("-input") else 1)
            raise OSError("exec failed")
        out = rem.ensure_input_watch(pool(), REPO, WID, runner=call)
        self.assertEqual(out["outcome"], rem.SUPERVISOR_FAILED)

    def test_a_start_that_fails(self):
        def call(argv, **kw):
            if "has-session" in argv:
                return Done(0 if not argv[-1].endswith("-input") else 1)
            return Done(1, err="duplicate session")
        out = rem.ensure_input_watch(pool(), REPO, WID, runner=call)
        self.assertEqual((out["outcome"], out["why"]), (rem.SUPERVISOR_FAILED, "duplicate session"))


class ReadersSayUnknown(unittest.TestCase):
    def test_an_unreadable_queue_is_none(self):
        ws = pool()
        orig = sup.td.owned_task_ids
        sup.td.owned_task_ids = lambda *a: (_ for _ in ()).throw(sup.td.WorkerHoldUnreadable("EIO"))
        try:
            self.assertIsNone(sup.work_outstanding(ws, WID))
        finally:
            sup.td.owned_task_ids = orig

    def test_an_empty_or_unrecognised_pane_is_unknown(self):
        self.assertEqual(sup.classify_pane_text(""), ps.PANE_UNKNOWN)
        self.assertEqual(sup.classify_pane_text("no prompt here\n"), ps.PANE_UNKNOWN)

    def test_no_run_or_a_failed_capture_reads_nothing(self):
        self.assertEqual(sup.observe_pane(pool(with_run=False), WID, runner=Runner()), (None, None))
        self.assertEqual(sup.observe_pane(pool(), WID, runner=Runner(capture_pane=OSError("x"))),
                         (None, None))


if __name__ == "__main__":
    unittest.main()
