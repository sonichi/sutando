#!/usr/bin/env python3
"""stop_interrupt: a Stop interrupts the core's turn only when the pane is mid-turn on that task.

Run: python3 tests/stop-interrupt.test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import activity_bus  # noqa: E402
import stop_interrupt as si  # noqa: E402

TARGET = "task-ag2space_dlocal~task-0e542ed29e181e5510"
BUSY = "✻ Running… (12s · esc to interrupt)\n> "
IDLE = "> \n  ? for shortcuts"
DIALOG = ("✻ Running… (40m · esc to interrupt)\n│ Bash command\n│   gh pr merge 1\n│ Do you want to proceed?\n"
          "│ ❯ 1. Yes\n│   2. No, and tell Claude what to do differently (esc)\n")


class FakeTmux:
    def __init__(self, pane_text=BUSY):
        self.pane_text, self.calls = pane_text, []

    def __call__(self, argv):
        self.calls.append(argv)
        if "list-windows" in argv:
            return "0\n"
        if "capture-pane" in argv:
            return self.pane_text
        if argv[-1] == "tmux-socket":
            return "/run/core.sock\n"
        return ""

    def escapes(self):
        return [c for c in self.calls if c[-1] == "Escape"]


CRON_TURN = "  ⎿  ran the cron\n✻ Running… (3s · esc to interrupt)\n> "
PRINTED = ("  ⎿  diff: - the footer says (esc to interrupt) while busy\n✻ Worked for 3m 2s\n"
           "> \n  ⏵⏵ bypass permissions on")


class NoSocket(FakeTmux):
    def __call__(self, argv):
        return "" if argv[-1] == "tmux-socket" else super().__call__(argv)


class StopInterrupt(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        self.tasks = self.ws / "tasks"
        self.tasks.mkdir()
        (self.ws / "state").mkdir()
        (self.tasks / f"{TARGET}.txt").write_text(f"id: {TARGET}\nchannel_id: !room:x\ntask: sleep 200\n")
        self.now = time.time()
        self.logs = []
        env = {k: v for k, v in os.environ.items() if not k.startswith("SUTANDO_TMUX")}
        mock.patch.dict(os.environ, {**env, si.ENABLE_ENV: "1"}, clear=True).start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.td.cleanup()

    def row(self, task_id, age=5.0, **extra):
        rec = {"ts": self.now - age, "line": "Bash", "kind": "working", "task": {"id": task_id}, **extra}
        with open(self.ws / "state" / "agent-activity.jsonl", "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def run_it(self, tmux):
        return si.interrupt(TARGET, workspace=self.ws, tasks_dir=self.tasks, run=tmux,
                            log=self.logs.append, now=self.now)

    def owner_rows(self):
        rows = [json.loads(l) for l in (self.ws / "state" / "agent-activity.jsonl").read_text().splitlines()]
        return [r for r in rows if r.get("audience") == "owner"]

    def test_mid_turn_on_the_cancelled_task_sends_one_escape(self):
        self.row(TARGET)
        tmux = FakeTmux()
        self.assertTrue(self.run_it(tmux))
        esc = tmux.escapes()
        self.assertEqual(len(esc), 1)
        self.assertIn("tmux-pane-keys.sh", esc[0][1])
        self.assertEqual(esc[0][esc[0].index("-t") + 1], "=sutando-core:0")
        self.assertEqual(esc[0][esc[0].index("-S") + 1], "/run/core.sock", "the launcher's socket, via the config helper")
        self.assertIn(["bash", str(si.CONFIG_SH), "tmux-socket"], tmux.calls)
        self.assertIn("interrupted the running turn", self.owner_rows()[-1]["line"])
        self.assertEqual(self.owner_rows()[-1]["room"], "!room:x")

    def live_rows(self, with_hook_rows):
        """The rig's rows for a Stop at +31 s on a `sleep 90` task: only the bus's TASK_STATUS rows
        existed live, because the hook could not bind an id carrying `~`."""
        base = {"room": "!r:x", "task": {"id": TARGET}}
        self.row(TARGET, age=42, **{**base, "kind": "notice", "line": "queued", "audience": "room",
                                    "projection": "TASK_STATUS"})
        self.row(TARGET, age=42, **{**base, "kind": "processing", "line": "picked up", "audience": "room",
                                    "projection": "TASK_STATUS"})
        if with_hook_rows:
            self.row(TARGET, age=40, **{**base, "kind": "processing", "line": "Your agent is working on a task"})
            self.row(TARGET, age=35, **{**base, "kind": "working", "line": "Sleep 90 seconds"})

    def test_the_live_stop_interrupts_once_the_hook_writes_rows_for_the_task(self):
        self.live_rows(with_hook_rows=True)
        tmux = FakeTmux()
        self.assertTrue(self.run_it(tmux))
        self.assertEqual(len(tmux.escapes()), 1)

    def test_delivery_rows_alone_never_name_the_sessions_task(self):
        self.live_rows(with_hook_rows=False)
        tmux = FakeTmux()
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(tmux.escapes(), [])
        self.assertIn("the session is on no task", self.logs[-1])

    def test_the_session_on_another_task_gets_no_key(self):
        self.row("task-other")
        tmux = FakeTmux()
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(tmux.escapes(), [])
        self.assertEqual([c for c in tmux.calls if "capture-pane" in c], [], "the pane is not even read")
        self.assertIn("the session is on task-other", self.owner_rows()[-1]["line"])

    def test_an_idle_pane_gets_no_key(self):
        self.row(TARGET)
        tmux = FakeTmux(IDLE)
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(tmux.escapes(), [])
        self.assertIn("no turn is running", self.owner_rows()[-1]["line"])

    def test_no_fresh_session_row_gets_no_key(self):
        self.row(TARGET, age=si.FRESH_S + 1)
        tmux = FakeTmux()
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(tmux.escapes(), [])

    def tui_card(self, session="sutando-core"):
        from hitl.cards import manager_for
        from hitl.schema import HumanRequirement
        return manager_for(self.ws).create(HumanRequirement(
            kind="confirmation", runtime="claude", message="waiting", guard=f"g-{session}",
            subject={"source": "tui", "session": session}, device={"id": session, "name": session}))

    def card_status(self, req):
        from hitl.cards import manager_for
        return manager_for(self.ws).get(req.id).status

    def test_a_prompt_parked_past_the_freshness_window_is_answered_with_one_escape(self):
        self.row(TARGET, age=si.FRESH_S + 600)
        tmux = FakeTmux(DIALOG.split("\n", 1)[1])
        self.assertTrue(self.run_it(tmux), "a turn parked on a prompt writes no rows")
        self.assertEqual(len(tmux.escapes()), 1)
        self.assertIn("a dialog in the pane holds this task", self.owner_rows()[-1]["line"])

    def test_an_open_terminal_card_waives_the_row_age_and_is_expired_by_the_escape(self):
        self.row(TARGET, age=si.FRESH_S + 600)
        req, other = self.tui_card(), self.tui_card("sutando-worker-2")
        tmux = FakeTmux(DIALOG)
        self.assertTrue(self.run_it(tmux))
        self.assertEqual(len(tmux.escapes()), 1)
        self.assertEqual(self.card_status(req), "expired")
        self.assertEqual(self.card_status(other), "pending", "another session's card is left alone")
        self.assertIn("1 terminal prompt card(s) expired", self.owner_rows()[-1]["line"])

    def test_an_open_card_with_an_idle_pane_gets_no_key_and_stays(self):
        self.row(TARGET, age=si.FRESH_S + 600)
        req = self.tui_card()
        tmux = FakeTmux(IDLE)
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(tmux.escapes(), [])
        self.assertEqual(self.card_status(req), "pending")

    def test_a_prompt_parked_on_another_task_gets_no_key(self):
        self.row("task-other", age=si.FRESH_S + 600)
        self.tui_card()
        tmux = FakeTmux(DIALOG)
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(tmux.escapes(), [])
        self.assertEqual([c for c in tmux.calls if "capture-pane" in c], [], "the pane is not even read")

    def test_a_dialog_quoted_above_the_turn_row_is_not_live(self):
        self.assertTrue(si.live_dialog(DIALOG))
        self.assertFalse(si.live_dialog("│ ❯ 1. Yes\n✻ Running… (3s · esc to interrupt)\n"))
        self.assertFalse(si.live_dialog(None))

    def test_delivery_rows_and_its_own_rows_never_count_as_the_session_turn(self):
        self.row(TARGET)
        self.row("task-other", projection="TASK_STATUS", audience="room")
        self.row("task-other", audience="owner")
        self.assertEqual(si.current_task(self.ws, self.now), TARGET)

    def test_a_duplicate_cancel_sends_a_single_escape(self):
        self.row(TARGET)
        tmux = FakeTmux()
        self.assertTrue(self.run_it(tmux))
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(len(tmux.escapes()), 1)
        self.assertIn("already interrupted once", self.owner_rows()[-1]["line"])

    def test_a_task_a_pool_worker_holds_is_not_interrupted_from_the_core_pane(self):
        self.row(TARGET)
        (self.ws / "deliveries" / "w1").mkdir(parents=True)
        (self.ws / "deliveries" / "w1" / f"{TARGET}.claimed").write_text("")
        tmux = FakeTmux()
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(tmux.escapes(), [])

    def test_the_worker_holding_it_interrupts_its_own_pane(self):
        self.row(TARGET)
        inbox = self.ws / "deliveries" / "w1"
        inbox.mkdir(parents=True)
        (inbox / f"{TARGET}.claimed").write_text("")
        os.environ["SUTANDO_TMUX_PANE"] = "%7"
        tmux = FakeTmux()
        self.assertTrue(si.interrupt(TARGET, workspace=self.ws, tasks_dir=inbox, run=tmux,
                                     log=self.logs.append, now=self.now))
        self.assertEqual(tmux.escapes()[0][tmux.escapes()[0].index("-t") + 1], "%7")

    def test_a_failed_send_is_logged_and_reports_false(self):
        self.row(TARGET)
        tmux = FakeTmux()
        real = tmux.__call__
        tmux_fail = lambda argv: None if argv[-1] == "Escape" else real(argv)  # noqa: E731
        self.assertFalse(si.interrupt(TARGET, workspace=self.ws, tasks_dir=self.tasks, run=tmux_fail,
                                      log=self.logs.append, now=self.now))
        self.assertIn("could not be sent", self.logs[-1])

    def test_an_unreadable_or_rowless_log_names_no_current_task(self):
        self.assertIsNone(si.current_task(self.ws, self.now), "no log")
        log = self.ws / "state" / "agent-activity.jsonl"
        log.write_text('{"ts": 1, "kind": "working", "task": {"id": "x"\n{"ts": 2, "kind": "notice"}\n')
        self.assertIsNone(si.current_task(self.ws, self.now), "malformed and taskless rows are skipped")

    def test_an_unreadable_pane_or_pool_state_sends_nothing(self):
        self.row(TARGET)
        tmux = FakeTmux(None)
        self.assertFalse(self.run_it(tmux))
        self.assertIn("could not be read", self.logs[-1])
        with mock.patch.object(si, "worker_holds", side_effect=OSError("unlistable")):
            (self.ws / "deliveries").mkdir()
            self.assertTrue(si._held_elsewhere(self.ws, self.tasks, TARGET))

    def test_a_row_that_cannot_be_written_never_blocks_the_interrupt(self):
        self.row(TARGET)
        tmux = FakeTmux()
        with mock.patch.object(si, "append_row", side_effect=OSError("disk full")):
            self.assertTrue(self.run_it(tmux))
        self.assertEqual(len(tmux.escapes()), 1)

    def test_the_cli_takes_the_canonical_workspace_and_never_fails(self):
        seen = []
        with mock.patch.object(si, "interrupt", lambda t, **k: seen.append((t, k["workspace"]))), \
                mock.patch.object(si, "resolve_workspace", return_value=self.ws):
            self.assertEqual(si.main([TARGET, "--tasks-dir", str(self.ws / "deliveries" / "w1")]), 0)
            self.assertEqual(si.main([TARGET, "--tasks-dir", str(self.tasks), "--workspace", "/w"]), 0)
        self.assertEqual(seen, [(TARGET, self.ws), (TARGET, Path("/w"))])
        with mock.patch.object(si, "interrupt", side_effect=RuntimeError("boom")):
            self.assertEqual(si.main([TARGET, "--tasks-dir", str(self.tasks)]), 0, "advisory: never fails")

    def test_a_stop_after_the_task_finished_never_escapes_the_turn_that_followed(self):
        self.row(TARGET, kind="done")
        tmux = FakeTmux(CRON_TURN)
        self.assertFalse(self.run_it(tmux), "the pane is mid-turn, but on an unbound turn after T")
        self.assertEqual(tmux.escapes(), [])
        self.assertIn("its newest row (done) was not written mid-turn", self.logs[-1])

    def test_a_turn_that_ended_on_a_thinking_row_is_not_escaped_later(self):
        # A T turn that ends without a result (waiting on a background subagent) gets only a Stop-hook row.
        for kind in ("thinking", "notice"):
            with self.subTest(kind=kind):
                (self.ws / "state" / "agent-activity.jsonl").unlink(missing_ok=True)
                self.row(TARGET, age=25 * 60, kind=kind)
                tmux = FakeTmux(CRON_TURN)
                self.assertFalse(self.run_it(tmux), "the pane is mid-turn on a cron turn, not T")
                self.assertEqual(tmux.escapes(), [])
                self.assertIn("was not written mid-turn", self.logs[-1])

    def test_a_turn_that_ends_while_the_pane_is_read_is_not_escaped(self):
        self.row(TARGET)
        for late in ("done row", "result"):
            with self.subTest(late=late):
                ws = self

                class Racing(FakeTmux):
                    def __call__(self, argv):
                        if "capture-pane" in argv:      # T finishes between the snapshot and the key
                            if late == "done row":
                                ws.row(TARGET, age=0, kind="done")
                            else:
                                (ws.ws / "results").mkdir(exist_ok=True)
                                (ws.ws / "results" / f"{TARGET}.txt").write_text("the answer")
                        return super().__call__(argv)
                tmux = Racing()
                self.assertFalse(self.run_it(tmux))
                self.assertEqual(tmux.escapes(), [])
                self.assertIn("changed while the pane was read", self.logs[-1])
                self.assertFalse((self.ws / "state" / "stop-interrupts" / TARGET).exists(), "not claimed")
                (self.ws / "results" / f"{TARGET}.txt").unlink(missing_ok=True)
                self.row(TARGET)

    def test_a_task_with_its_result_is_never_escaped(self):
        self.row(TARGET)
        (self.ws / "results").mkdir()
        (self.ws / "results" / f"{TARGET}.txt").write_text("the answer")
        tmux = FakeTmux()
        self.assertFalse(self.run_it(tmux))
        self.assertEqual(tmux.escapes(), [])
        self.assertIn("it already has its result", self.logs[-1])

    def test_esc_to_interrupt_counts_only_on_the_live_status_line(self):
        self.row(TARGET)
        tmux = FakeTmux(PRINTED)
        self.assertFalse(self.run_it(tmux), "printed above a finished turn, at an idle prompt")
        self.assertEqual(tmux.escapes(), [])
        self.assertFalse(si.mid_turn("esc to interrupt\n> "), "no turn row: not provably mid-turn")
        self.assertTrue(si.mid_turn(BUSY))
        self.assertTrue(si.mid_turn("✻ Thinking… (4s)\n> \n  esc to interrupt"), "the footer below the spinner")

    def test_the_launchers_window_is_used_over_the_lowest_one(self):
        self.row(TARGET)
        os.environ["SUTANDO_TMUX_WINDOW"] = "2"
        tmux = FakeTmux()
        self.assertTrue(self.run_it(tmux))
        self.assertEqual(tmux.escapes()[0][tmux.escapes()[0].index("-t") + 1], "=sutando-core:2")
        self.assertFalse([c for c in tmux.calls if "list-windows" in c])

    def test_the_session_rows_are_read_alike_by_the_stall_watch_and_the_stop(self):
        sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
        from ag2_sparrow import task_attempts
        self.assertEqual(task_attempts.ACTIVITY_TAIL_BYTES, si.TAIL_BYTES)
        rows = [{"ts": 1.0, "kind": "working", "task": {"id": "t"}},
                {"ts": 2.0, "kind": "done", "task": {"id": "t"}},
                {"ts": 3.0, "kind": "notice", "task": {"id": "t"}, "audience": "owner"},
                {"ts": 4.0, "kind": "notice", "task": {"id": "t"}, "projection": "TASK_STATUS"},
                {"ts": True, "task": {"id": "t"}}, {"ts": 5.0, "task": "t"}, {"ts": 6.0}, [1], "x", None,
                {"ts": 7, "kind": "thinking", "task": {"id": "u"}}]
        for rec in rows:
            with self.subTest(rec=rec):
                self.assertEqual(si.session_row(rec), task_attempts.session_row(rec))

    def test_an_explicit_socket_wins_and_no_socket_sends_no_key(self):
        self.row(TARGET)
        os.environ["SUTANDO_TMUX_SOCK"] = "/explicit.sock"
        tmux = FakeTmux()
        self.assertTrue(self.run_it(tmux))
        self.assertEqual(tmux.escapes()[0][tmux.escapes()[0].index("-S") + 1], "/explicit.sock")
        os.environ["SUTANDO_TMUX_SOCKET"] = "/canonical.sock"
        both = FakeTmux()
        (self.ws / "state" / "stop-interrupts" / TARGET).unlink()
        self.assertTrue(self.run_it(both))
        self.assertEqual(both.escapes()[0][both.escapes()[0].index("-S") + 1], "/canonical.sock",
                         "with both set, the canonical name the launchers use wins")
        del os.environ["SUTANDO_TMUX_SOCK"], os.environ["SUTANDO_TMUX_SOCKET"]
        silent = NoSocket()
        self.assertFalse(self.run_it(silent))
        self.assertEqual(silent.escapes(), [])
        self.assertIn("no tmux socket", self.logs[-1])

    def emit(self, switch):
        ctl = self.tasks / "task-ctl-1.txt"
        ctl.write_text(f"id: task-ctl-1\ntask: CANCEL_INSTRUCTION: stop processing {TARGET}\n")
        stub = self.ws / "py"
        record = self.ws / "calls"
        record.unlink(missing_ok=True)
        stub.write_text(f'#!/bin/bash\necho "$*" >> "{record}"\n')
        stub.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != si.ENABLE_ENV}
        if switch is not None:
            env[si.ENABLE_ENV] = switch
        subprocess.run(["bash", "-c", f'source "{REPO}/src/task-emit.sh"; activity_cancel_target "{ctl.name}"; wait'],
                       env={**env, "SUTANDO_PY_BIN": str(stub), "TASKS_DIR": str(self.tasks)}, check=True)
        want = ("stop-requested", "stop_interrupt.py") if switch and switch.lower() in si.TRUE else ("CANCELLED",)
        for _ in range(100):
            if record.exists() and all(w in record.read_text() for w in want):
                break
            time.sleep(0.05)
        return ctl, [c.split("/src/", 1)[-1] for c in record.read_text().splitlines()]

    def test_with_the_switch_off_a_cancel_is_cancelled_at_once_as_before(self):
        for switch in (None, "", "0", "no", "off", "maybe"):
            _, calls = self.emit(switch)
            self.assertEqual(calls, [f"activity_bus.py transition CANCELLED --task-id {TARGET} --reason cancel requested"],
                             f"switch {switch!r}: no Escape, no confirm poller")
            self.assertFalse(si.enabled({si.ENABLE_ENV: switch} if switch is not None else {}))

    def test_the_shell_and_python_read_the_switch_alike(self):
        for switch in ("1", "true", "YES", "On"):
            _, calls = self.emit(switch)
            self.assertTrue([c for c in calls if c.startswith("stop_interrupt.py")], switch)
            self.assertTrue(si.enabled({si.ENABLE_ENV: switch}))

    def test_the_cli_does_nothing_with_the_switch_off(self):
        os.environ.pop(si.ENABLE_ENV)
        with mock.patch.object(si, "interrupt", side_effect=AssertionError("must not run")), \
                mock.patch.object(si, "confirm_stop", side_effect=AssertionError("must not run")):
            self.assertEqual(si.main([TARGET, "--tasks-dir", str(self.tasks), "--control-file", "x"]), 0)

    def test_run_returns_stdout_only_on_success(self):
        self.assertEqual(si._run(["echo", "hi"]), "hi\n")
        self.assertIsNone(si._run(["false"]))
        self.assertIsNone(si._run(["/nonexistent/tmux"]))

    def test_the_watcher_emit_hands_the_cancelled_task_to_stop_interrupt(self):
        ctl, calls = self.emit("1")
        self.assertIn(f"stop_interrupt.py {TARGET} --tasks-dir {self.tasks} --control-file {ctl}", calls)
        self.assertIn(f"activity_bus.py stop-requested --task-id {TARGET}", calls)
        self.assertFalse([c for c in calls if "CANCELLED" in c], "nothing is cancelled before the stop is confirmed")



class ConfirmStop(unittest.TestCase):
    """CANCELLED only once the core answered the control task without answering the stopped one."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        (self.ws / "results").mkdir()
        (self.ws / "state").mkdir()
        self.logs = []
        self.store = si.ActivityStore(self.ws)

    def tearDown(self):
        self.td.cleanup()

    def running(self, tid):
        self.store.apply(si.LifecycleTransition(tid, "RUNNING", room="!r:x"))

    def phase(self, tid):
        return json.loads(self.store.path(tid).read_text())["phase"]

    def test_a_stop_is_not_confirmed_while_a_worker_holds_the_task(self):
        self.running(TARGET)
        (self.ws / "results" / "task-ctl-w.txt").write_text("Cancelled")
        (self.ws / "deliveries" / "w1").mkdir(parents=True)
        (self.ws / "deliveries" / "w1" / f"{TARGET}.claimed").write_text("")
        self.assertIsNone(self.confirm(TARGET, "task-ctl-w"))
        self.assertEqual(self.phase(TARGET), "RUNNING", "the worker may still post its answer")
        self.assertIn("a pool worker still holds it", self.logs[-1])

    def confirm(self, tid, ctl, **kw):
        return si.confirm_stop(tid, ctl, workspace=self.ws, sleep=lambda s: None, log=self.logs.append, **kw)

    def test_a_stop_request_is_seen_at_once_and_does_not_end_the_task(self):
        self.running(TARGET)
        self.store.apply(activity_bus.StopRequest(TARGET))
        self.store.apply(activity_bus.StopRequest(TARGET))
        self.assertEqual(self.phase(TARGET), "RUNNING")
        rows = [json.loads(l) for l in (self.ws / "state" / "agent-activity.jsonl").read_text().splitlines()]
        stop = [r for r in rows if r["line"] == "stop requested"]
        self.assertEqual(len(stop), 1, "once per task")
        self.assertEqual((stop[0]["kind"], stop[0]["projection"], stop[0].get("done", False)), ("notice", "TASK_STATUS", False))

    def test_handled_control_and_no_result_confirms_the_stop(self):
        self.running(TARGET)
        (self.ws / "results" / "task-ctl-1.txt").write_text("[no-send] stopped it")
        self.assertEqual(self.confirm(TARGET, "task-ctl-1"), "cancelled")
        self.assertEqual(self.phase(TARGET), "CANCELLED")

    def test_a_no_send_or_empty_result_is_a_stop_and_a_reply_is_a_finish(self):
        for body, want in (("[no-send] stopped", "cancelled"), ("  ", "cancelled"), ("DONE", "finished")):
            with self.subTest(body=body):
                tid = f"task-t{len(body)}"
                self.running(tid)
                (self.ws / "results" / f"{tid}.txt").write_text(body)
                (self.ws / "results" / f"task-ctl-{tid}.txt").write_text("ok")
                self.assertEqual(self.confirm(tid, f"task-ctl-{tid}"), want)
                self.assertEqual(self.phase(tid), "CANCELLED" if want == "cancelled" else "RUNNING")

    def test_an_archived_control_answer_counts_and_an_unanswered_one_never_rules(self):
        self.running(TARGET)
        (self.ws / "results" / "archive").mkdir()
        (self.ws / "results" / "archive" / "task-ctl-2-1793057500.txt").write_text("[no-send]")
        self.assertEqual(self.confirm(TARGET, "task-ctl-2"), "cancelled")
        t = [0.0]
        def clock():
            t[0] += 1000
            return t[0]
        self.running("task-other")
        self.assertIsNone(self.confirm("task-other", "task-ctl-none", clock=clock, max_s=1500))
        self.assertEqual(self.phase("task-other"), "RUNNING")
        self.assertIn("was not answered", self.logs[-1])

    def test_the_voice_cancel_path_still_reaches_cancelled_on_a_confirmed_stop(self):
        voice, ctl = "task-1793057400000", "task-1793057459000"
        self.running(voice)
        (self.ws / "tasks").mkdir()
        cancel = self.ws / "tasks" / f"{ctl}.txt"
        cancel.write_text(f"id: {ctl}\nsource: voice-local\ntask: CANCEL_INSTRUCTION: stop processing {voice} "
                          "if still in flight. If already completed, no-op. Reply briefly confirming.\n")
        self.assertEqual(activity_bus.cancel_target(cancel.read_text()), voice)
        self.store.apply(activity_bus.StopRequest(voice))
        self.assertEqual(self.phase(voice), "RUNNING", "the request alone does not cancel it")
        (self.ws / "results" / f"{ctl}.txt").write_text("Cancelled task-1793057400000 (was in progress)")
        with mock.patch.object(si, "interrupt"), mock.patch.dict(os.environ, {si.ENABLE_ENV: "1"}):
            self.assertEqual(si.main([voice, "--tasks-dir", str(self.ws / "tasks"), "--workspace", str(self.ws),
                                      "--control-file", str(cancel)]), 0)
        self.assertEqual(self.phase(voice), "CANCELLED")

if __name__ == "__main__":
    unittest.main()
