#!/usr/bin/env python3
"""#4389: an unattended core held at a CLI gate.

(a) A model picker Sutando opened itself (a /model send that left it on screen) is dismissed
    with Escape after its configured delay; a picker without that attribution, one that
    appeared outside the claim window, a spend/usage gate, or a picker someone is moving
    through is never touched.
(b) Every task queued behind a blocked core gets one on-hold notice under its own message,
    the task stays queued (no result is written), and the owner's card counts them.

Pane fixtures: tests/fixtures/pane-claude-{model-picker,usage-credits-refused,idle-ready}.txt.
The picker is reconstructed from the text quoted in #4389, not captured live.

Run: python3 tests/core-gate-unattended.test.py
"""
import importlib.util as u
import json
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))
spec = u.spec_from_file_location("ciw_4389", SRC / "core-input-watch.py")
M = u.module_from_spec(spec)
spec.loader.exec_module(M)

import core_gate_notice  # noqa: E402
import self_opened_gate as G  # noqa: E402

from hitl.manager import HitlManager, HitlStore  # noqa: E402
from unittest.mock import patch  # noqa: E402


def _pane(name):
    return (HERE / "fixtures" / f"pane-claude-{name}.txt").read_text(encoding="utf-8")


PICKER = _pane("model-picker")
REFUSED = _pane("usage-credits-refused")
IDLE = _pane("idle-ready")
SESSION = "sutando-core"
T0 = 1_000_000.0


def _gate(pane):
    state, _detail, prompt, kind = M.compose_state(pane, "idle", True)
    return state, kind, prompt


def _run(state_dir, ticks, enabled=True):
    """Drive the monitor's clock + dismissal over (t, pane) ticks; returns [(t, key)] sent."""
    clock, sent = (None, None, None), []
    for t, pane in ticks:
        state, kind, prompt = _gate(pane)
        clock = M.gate_clock(clock, state, prompt, t)
        key = M.dismiss_step(state_dir, SESSION, state, kind, prompt, clock, t, enabled)
        if key:
            sent.append((t, key))
            G.clear(state_dir, SESSION)
    return sent


def _every(start, end, pane, step=3.0):
    t, out = start, []
    while t <= end:
        out.append((t, pane))
        t += step
    return out


class TestFixturesClassify(unittest.TestCase):
    def test_the_gates_from_the_incident_read_as_human_only(self):
        self.assertEqual(_gate(PICKER)[:2], ("blocked-human", "selection"))
        self.assertEqual(_gate(REFUSED)[:2], ("blocked-human", "turn-rejected"))
        self.assertEqual(_gate(IDLE)[:2], ("idle-ready", None))


class TestSelfOpenedPickerDismissal(unittest.TestCase):
    def setUp(self):
        self.sd = tempfile.mkdtemp()

    def _record(self, dismiss_after=300.0, at=T0, session=SESSION):
        G.record(self.sd, session, "model-switch", dismiss_after, claim_window_s=70.0, now=at)

    def test_a_picker_a_human_opened_is_never_dismissed(self):
        """No attribution record: an hour at the picker sends nothing."""
        self.assertEqual(_run(self.sd, _every(T0, T0 + 3600, PICKER)), [])

    def test_our_picker_is_dismissed_once_with_escape_after_the_delay(self):
        self._record()
        sent = _run(self.sd, _every(T0 + 2, T0 + 900, PICKER))
        self.assertEqual(len(sent), 1)
        t, key = sent[0]
        self.assertEqual(key, "Escape")
        self.assertGreaterEqual(t - (T0 + 2), 300.0)
        self.assertIsNone(G.load(self.sd, SESSION), "the record is consumed by the dismissal")

    def test_not_before_the_configured_delay(self):
        self._record(dismiss_after=600.0)
        self.assertEqual(_run(self.sd, _every(T0 + 2, T0 + 590, PICKER)), [])

    def test_a_picker_first_seen_outside_the_claim_window_is_not_ours(self):
        """A stale record from an earlier switch must not cover a /model a human typed later."""
        self._record()
        self.assertEqual(_run(self.sd, _every(T0 + 120, T0 + 3600, PICKER)), [])

    def test_someone_moving_through_the_picker_restarts_the_clock(self):
        self._record()
        moved = PICKER.replace(" ❯ 5. Haiku", "   5. Haiku").replace("   4. Fable 5.1", " ❯ 4. Fable 5.1")
        self.assertEqual(_gate(moved)[:2], ("blocked-human", "selection"))
        ticks = _every(T0 + 2, T0 + 290, PICKER) + _every(T0 + 293, T0 + 580, moved)
        self.assertEqual(_run(self.sd, ticks), [], "290s + 287s, each under the 300s delay")
        sent = _run(self.sd, _every(T0 + 2, T0 + 290, PICKER) + _every(T0 + 293, T0 + 620, moved))
        self.assertEqual(len(sent), 1)
        self.assertGreaterEqual(sent[0][0] - (T0 + 293), 300.0)

    def test_usage_credit_gates_are_never_answered_even_with_a_record(self):
        self._record()
        self.assertEqual(_run(self.sd, _every(T0 + 2, T0 + 3600, REFUSED)), [])
        spendy = PICKER.replace("Haiku 4.5 · Fastest for quick answers", "uses usage credits")
        self.assertEqual(_run(self.sd, _every(T0 + 2, T0 + 3600, spendy)), [])

    def test_off_switches_and_bad_records_send_nothing(self):
        self._record(dismiss_after=0)
        self.assertEqual(_run(self.sd, _every(T0 + 2, T0 + 3600, PICKER)), [], "0 = never")
        self._record()
        self.assertEqual(_run(self.sd, _every(T0 + 2, T0 + 3600, PICKER), enabled=False), [],
                         "--no-auto-answer")
        G.clear(self.sd, SESSION)
        self._record(session="worker-1")
        self.assertEqual(_run(self.sd, _every(T0 + 2, T0 + 3600, PICKER)), [], "another seat's record")
        pathlib.Path(G.record_path(self.sd, SESSION)).write_text("{not json")
        self.assertEqual(_run(self.sd, _every(T0 + 2, T0 + 3600, PICKER)), [], "malformed")

    def test_leaving_the_gate_resets_the_clock(self):
        self._record()
        ticks = _every(T0 + 2, T0 + 200, PICKER) + [(T0 + 203, IDLE)] + _every(T0 + 206, T0 + 400, PICKER)
        self.assertEqual(_run(self.sd, ticks), [], "the second picker was first seen past the claim window")

    def test_a_record_that_does_not_parse_as_numbers_is_no_permission(self):
        pathlib.Path(G.record_path(self.sd, SESSION)).write_text(json.dumps(
            {"session": SESSION, "kind": "selection", "opened_at": "soon",
             "claim_window_s": 70, "dismiss_after_s": 1}))
        self.assertIsNone(G.load(self.sd, SESSION))
        pathlib.Path(G.record_path(self.sd, SESSION)).write_text(json.dumps(["not", "a", "record"]))
        self.assertIsNone(G.load(self.sd, SESSION))

    def test_clearing_twice_is_not_an_error(self):
        G.clear(self.sd, SESSION)
        G.clear(self.sd, SESSION)
        self.assertIsNone(G.load(self.sd, SESSION))

    def test_the_cli_reports_an_unwritable_state_dir(self):
        blocker = pathlib.Path(self.sd) / "file"
        blocker.write_text("x")
        rc = G.main(["record", "--state-dir", str(blocker / "state"), "--session", SESSION,
                     "--opener", "x", "--dismiss-after", "1"])
        self.assertEqual(rc, 1, "the shell opener must see the record did not land")

    def test_the_cli_writes_and_clears_the_record(self):
        self.assertEqual(G.main(["record", "--state-dir", self.sd, "--session", SESSION,
                                 "--opener", "x", "--dismiss-after", "120"]), 0)
        rec = G.load(self.sd, SESSION)
        self.assertEqual((rec["opener"], rec["dismiss_after_s"], rec["kind"]), ("x", 120.0, "selection"))
        self.assertEqual(G.main(["clear", "--state-dir", self.sd, "--session", SESSION]), 0)
        self.assertIsNone(G.load(self.sd, SESSION))


def _task(ws, n, room="!room:ag2.space"):
    body = (f"id: task-{n}\nsource: ag2space\nchannel_id: {room}\nsource_message_id: $ev{n}\n"
            f"user_id: @owner:ag2.space\naccess_tier: owner\ntask: do thing {n}\n")
    (ws / "tasks" / f"task-{n}.txt").write_text(body)


def _rows(ws):
    p = ws / "state" / "agent-activity.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


class TestQueuedTasksHearWhy(unittest.TestCase):
    def setUp(self):
        self.ws = pathlib.Path(tempfile.mkdtemp())
        (self.ws / "tasks").mkdir()
        (self.ws / "results").mkdir()
        self.mgr = HitlManager(HitlStore(self.ws / "state" / "hitl"))

    def _escalate(self, pane, **force):
        state, kind, prompt = _gate(pane)
        state, kind = force.get("state", state), force.get("kind", kind)
        queued = core_gate_notice.queued_count(self.ws)
        req = M.escalate(self.mgr, state, f"awaiting user: {kind}", kind, prompt, SESSION, queued=queued)
        return req, M.notice_queued(self.mgr, req, self.ws, state, kind)

    def test_each_queued_task_gets_one_on_hold_notice_and_stays_queued(self):
        _task(self.ws, 1)
        _task(self.ws, 2)
        req, noticed = self._escalate(REFUSED)
        self.assertEqual(sorted(noticed), ["task-1", "task-2"])
        rows = _rows(self.ws)
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual((r["kind"], r["audience"], r["room"]), ("notice", "room", "!room:ag2.space"))
            self.assertIn("usage limit", r["line"])
            self.assertIn("kept", r["line"])
            self.assertNotIn("usage-credits", r["line"], "the room sees a category, not the prompt")
        self.assertEqual({r["task"]["event"] for r in rows}, {"$ev1", "$ev2"})
        self.assertEqual(list((self.ws / "results").iterdir()), [], "no result: the task is not closed")
        self.assertEqual(len(list((self.ws / "tasks").glob("task-*.txt"))), 2)
        self.assertIn("2 queued task(s)", req.message)

    def test_a_tick_later_only_new_tasks_are_noticed(self):
        _task(self.ws, 1)
        self._escalate(PICKER)
        req, again = self._escalate(PICKER)
        self.assertEqual(again, [], "same episode, same task: once")
        self.assertIn("1 queued task(s)", req.message, "the choice card counts them too")
        _task(self.ws, 3)
        self.assertEqual(self._escalate(PICKER)[1], ["task-3"])
        self.assertEqual(len(_rows(self.ws)), 2)
        self.assertNotIn("Haiku", _rows(self.ws)[0]["line"])

    def test_no_queue_no_rows_and_no_count_on_the_card(self):
        req, noticed = self._escalate(PICKER)
        self.assertEqual(noticed, [])
        self.assertNotIn("queued task", req.message)

    def test_a_signed_out_core_says_so(self):
        _task(self.ws, 1)
        self._escalate(_pane("idle-ready"), state="logged-out", kind=None)
        self.assertIn("it is signed out", _rows(self.ws)[0]["line"])

    def test_an_unreadable_queue_is_no_count_and_no_notice(self):
        _task(self.ws, 1)
        with patch.object(core_gate_notice.task_queue, "pending_files", side_effect=PermissionError("denied")):
            self.assertEqual(core_gate_notice.queued_count(self.ws), 0)
            req = M.escalate(self.mgr, "blocked-human", "d", "selection", PICKER, SESSION)
            self.assertEqual(core_gate_notice.notice_queued(self.mgr, req, self.ws, "blocked-human", "selection"), [])
        self.assertEqual(_rows(self.ws), [])

    def test_no_requirement_no_notice(self):
        _task(self.ws, 1)
        self.assertEqual(core_gate_notice.notice_queued(self.mgr, None, self.ws, "blocked-human", "selection"), [],
                         "escalate() returned None: nothing to dedup against")
        gone = type("Req", (), {"id": "hitl_gone"})()
        self.assertEqual(core_gate_notice.notice_queued(self.mgr, gone, self.ws, "blocked-human", "selection"), [])
        self.assertEqual(_rows(self.ws), [])

    def test_a_notice_that_could_not_be_written_is_retried_next_tick(self):
        _task(self.ws, 1)
        _task(self.ws, 2)
        real = core_gate_notice.activity_rows.append

        def flaky(line, **kw):
            if kw["task"]["id"] == "task-2":
                raise OSError("disk full")
            return real(line, **kw)
        with patch.object(core_gate_notice.activity_rows, "append", flaky):
            self.assertEqual(self._escalate(PICKER)[1], ["task-1"])
        self.assertEqual(self._escalate(PICKER)[1], ["task-2"], "not marked noticed, so tried again")
        self.assertEqual(len(_rows(self.ws)), 2)

    def test_a_notice_failure_never_takes_down_the_monitor(self):
        _task(self.ws, 1)
        self.assertEqual(M.notice_queued(object(), object(), self.ws, "blocked-human", "selection"), [])


class _Stop(Exception):
    pass


class _Clock:
    """The monitor's `time`: sleep advances it, and ends the loop after `ticks` sleeps."""

    def __init__(self, ticks):
        self.t, self.left = T0, ticks

    def time(self):
        return self.t

    def sleep(self, s):
        self.left -= 1
        if self.left <= 0:
            raise _Stop()
        self.t += s


class TestMonitorLoop(unittest.TestCase):
    """main() itself, across ticks, with tmux replaced by a pane that an Escape clears.
    115 ticks of 3s end inside the 120s the auto-answer stays in the signal file."""

    def _loop(self, with_record, ticks=115):
        ws = pathlib.Path(tempfile.mkdtemp())
        (ws / "tasks").mkdir()
        _task(ws, 1)
        out = ws / "state" / "core-supervisor.json"
        out.parent.mkdir()
        if with_record:
            G.record(str(out.parent), SESSION, "model-switch", 300.0, claim_window_s=70.0, now=T0)
        screen, sent = {"pane": PICKER}, []

        def send(sock, sess, key):
            sent.append(key)
            if key == "Escape":
                screen["pane"] = IDLE
            return True

        class _RH:
            TMUX_SOCKET = SESSION = None

            def derive(self):
                return {"health": "idle"}
        clock = _Clock(ticks)
        argv = ["core-input-watch.py", "--socket", "/x.sock", "--session", SESSION, "--out", str(out)]
        with patch.object(M, "capture", lambda s, sess: screen["pane"]), \
                patch.object(M, "send_keys", send), \
                patch.object(M, "_load_runtime_health", lambda: _RH()), \
                patch.object(M, "gateway_alive", lambda *a: True), \
                patch.object(M, "_ensure_tmux_on_path", lambda: None), \
                patch.object(M, "time", clock), patch.object(sys, "argv", argv):
            with self.assertRaises(_Stop):
                M.main()
        return ws, out, sent

    def test_our_picker_is_escaped_and_the_core_and_its_queue_move_on(self):
        ws, out, sent = self._loop(with_record=True)
        self.assertEqual(sent, ["Escape"])
        sig = json.loads(out.read_text())
        self.assertEqual(sig["state"], "idle-ready")
        self.assertEqual((sig["auto_answered"]["key"], sig["auto_answered"]["self_opened"]), ("Escape", True))
        self.assertIsNone(G.load(str(out.parent), SESSION), "the record is spent")
        self.assertEqual([r["task"]["id"] for r in _rows(ws)], ["task-1"])
        self.assertEqual(os.listdir(ws / "tasks"), ["task-1.txt"])

    def test_without_a_record_the_loop_never_types(self):
        ws, out, sent = self._loop(with_record=False)
        self.assertEqual(sent, [])
        self.assertEqual(json.loads(out.read_text())["state"], "blocked-human")


if __name__ == "__main__":
    unittest.main(verbosity=2)
