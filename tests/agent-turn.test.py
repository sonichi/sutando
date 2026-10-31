#!/usr/bin/env python3
"""Turn context file and the 3-outcome turn gate (src/agent_turn.py)."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import agent_turn as at  # noqa: E402


def _ctx(state, task="tsk_A", gen=1, room="!a:ag2.space"):
    return at.build_turn_context(task_id=task, execution_generation=gen, room_id=room,
                                 agent_mxid="@agent:ag2.space", state_dir=state)


def _journal(state, *records):
    with open(at.room_actions_path(state), "a") as fh:
        for r in records:
            fh.write(json.dumps({"ts": 1.0, **r}) + "\n")


def _intent(turn, seq, status="working", action="room.message.send"):
    return {"record_type": at.INTENT, "turn_id": turn, "seq": seq,
            "operation_id": f"op_{turn}_{seq}", "action": action,
            "requested_task_status": status}


def _outcome(turn, seq, op_status="succeeded", phase="working", terminal=False,
             requested="working", blockers=0):
    return {"record_type": at.OUTCOME, "turn_id": turn, "seq": seq,
            "operation_id": f"op_{turn}_{seq}", "operation_status": op_status,
            "requested_task_status": requested, "applied_execution_phase": phase,
            "applied_terminal": terminal, "active_blocker_count": blockers}


class TurnContext(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def test_immutable_turn_context_file(self):
        ctx = _ctx(self.state)
        path = at.write_turn_context(self.state, ctx)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
        self.assertEqual(at.read_turn_context(path), ctx)
        self.assertIsInstance(ctx["issued_at"], float, "epoch seconds")
        env = at.child_env(path, base={})
        self.assertEqual(env[at.TURN_CONTEXT_ENV], str(path.resolve()))
        with self.assertRaises(FileExistsError):
            at.write_turn_context(self.state, {**ctx, "room_id": "!other:ag2.space"})
        self.assertEqual(at.read_turn_context(path)["room_id"], "!a:ag2.space")
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name], "no temp left behind")

    def test_concurrent_turns_in_different_rooms_never_overwrite(self):
        paths, errors = {}, []

        def run(i):
            try:
                c = _ctx(self.state, task=f"tsk_{i}", room=f"!r{i}:ag2.space")
                paths[i] = at.write_turn_context(self.state, c)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=run, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        for i, p in paths.items():
            self.assertEqual(at.read_turn_context(p)["room_id"], f"!r{i}:ag2.space")

    def test_turn_id_shape_and_validation(self):
        tid = at.new_turn_id("tsk_A", 3, now_ms=1761500000000)
        self.assertRegex(tid, r"^tsk_A_g3_[0-9A-Z]{26}$")
        self.assertLess(at.new_turn_id("t", 1, 1), at.new_turn_id("t", 1, 2), "ulid sorts by time")
        for bad in ("../x", "a/b", ""):
            with self.assertRaises(ValueError):
                at.new_turn_id(bad, 1)
        with self.assertRaises(ValueError):
            at.new_turn_id("t", 0)

    def test_proxy_resolves_newest_open_turn_and_close_fences_only_its_own(self):
        old = at.write_turn_context(self.state, {**_ctx(self.state, gen=1)})
        new_ctx = _ctx(self.state, gen=2)
        new = at.write_turn_context(self.state, new_ctx)
        other = at.write_turn_context(self.state, _ctx(self.state, task="tsk_B"))
        self.assertEqual(at.open_turn_for_task(self.state, "tsk_A"), new)
        at.close_turn(self.state, new_ctx["turn_id"])
        self.assertTrue(at.is_closed(self.state, new_ctx["turn_id"]))
        self.assertFalse(new.exists())
        self.assertTrue(old.exists() and other.exists())
        self.assertEqual(at.open_turn_for_task(self.state, "tsk_A"), old)
        self.assertIsNone(at.open_turn_for_task(self.state, "../etc"))
        self.assertEqual(sorted(at.open_turns(self.state)), sorted([old, other]))

    def test_newest_open_turn_is_by_created_at_not_name(self):
        def mk(gen, ms):
            return at.write_turn_context(self.state, at.build_turn_context(
                task_id="tsk_N", execution_generation=gen, state_dir=self.state, now_ms=ms))
        mk(9, 2000)
        g10 = mk(10, 3000)
        self.assertEqual(at.open_turn_for_task(self.state, "tsk_N"), g10, "g10 sorts before g9 by name")


class Edges(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())

    def test_turn_paths_refuse_a_traversal(self):
        with self.assertRaises(ValueError):
            at.is_closed(self.state, "../x")

    def test_a_vanished_temp_file_or_an_unsyncable_directory_does_not_fail_the_write(self):
        real_unlink = os.unlink

        def unlink(path, *a, **k):
            if ".tsk_A_g1_" in str(path):
                real_unlink(path)
                raise FileNotFoundError(path)
            return real_unlink(path, *a, **k)
        with mock.patch.object(at.os, "unlink", unlink):
            self.assertTrue(at.write_turn_context(self.state, _ctx(self.state)).exists())
        at._fsync_dir(self.state / "missing")
        with mock.patch.object(at.os, "fsync", side_effect=OSError("no")):
            at._fsync_dir(self.state)

    def test_unreadable_closed_and_foreign_turn_files_are_never_bound(self):
        self.assertIsNone(at.read_turn_context(self.state / "nope.json"))
        ctx = _ctx(self.state)
        path = at.write_turn_context(self.state, ctx)
        (self.state / "turns" / f"{ctx['turn_id']}.closed").touch()
        self.assertIsNone(at.open_turn_for_task(self.state, "tsk_A"), "closed but not yet unlinked")
        forged = self.state / "turns" / f"tsk_A_g2_{'A' * 26}.json"
        forged.write_text(json.dumps({"turn_id": forged.stem, "task_id": "tsk_B", "created_at_ms": 9}))
        self.assertIsNone(at.open_turn_for_task(self.state, "tsk_A"), "names task A, says task B")
        at.close_turn(self.state, ctx["turn_id"])
        at.close_turn(self.state, ctx["turn_id"])
        self.assertFalse(path.exists())

    def test_odd_journal_lines_and_outcome_shapes(self):
        turn = _ctx(self.state)["turn_id"]
        at.room_actions_path(self.state).write_text(
            "[1, 2]\n" + json.dumps({**_intent(turn, "x", status="done")}) + "\n"
            + json.dumps({"record_type": at.OUTCOME, "turn_id": turn, "seq": "x",
                          "operation_id": f"op_{turn}_x", "operation_status": "succeeded",
                          "applied_execution_phase": "responded"}) + "\n")
        v = at.classify_turn(at.read_turn_records(self.state, turn))
        self.assertEqual(v.outcome, at.TERMINAL_CONFIRMED, "a phase alone says terminal")


class CloseUnderJournalLock(unittest.TestCase):
    """The .closed marker is written under the proxy's room-actions.jsonl.lock."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.ctx = _ctx(self.state)
        at.write_turn_context(self.state, self.ctx)
        self.lock = self.state / (at.ROOM_ACTIONS_NAME + ".lock")

    def hold(self, pid):
        self.lock.write_text(f"{pid} proxytoken {int(time.time() * 1000)}")

    def test_a_close_takes_and_releases_the_lock(self):
        seen = []
        real_open = os.open

        def spy(path, flags, *a):
            if str(path).endswith(".closed"):
                seen.append(self.lock.read_text().split()[0])
            return real_open(path, flags, *a)
        with mock.patch.object(at.os, "open", spy):
            self.assertTrue(at.close_turn(self.state, self.ctx["turn_id"]))
        self.assertEqual(seen, [str(os.getpid())], "the marker is written while this process holds the lock")
        self.assertFalse(self.lock.exists())
        self.assertTrue(at.is_closed(self.state, self.ctx["turn_id"]))

    def test_a_live_holder_is_waited_on_and_the_marker_lands_after_its_release(self):
        self.hold(os.getpid())
        done = threading.Event()
        threading.Thread(target=lambda: (at.close_turn(self.state, self.ctx["turn_id"]), done.set()),
                         daemon=True).start()
        time.sleep(0.3)
        self.assertFalse(at.is_closed(self.state, self.ctx["turn_id"]), "no close inside the proxy's section")
        self.lock.unlink()
        self.assertTrue(done.wait(2))
        self.assertTrue(at.is_closed(self.state, self.ctx["turn_id"]))

    def test_a_live_holder_past_the_wait_changes_nothing_and_a_later_close_lands(self):
        self.hold(os.getpid())
        start = time.monotonic()
        with mock.patch.object(at, "JOURNAL_LOCK_WAIT_S", 0.2):
            self.assertFalse(at.close_turn(self.state, self.ctx["turn_id"]))
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertFalse(at.is_closed(self.state, self.ctx["turn_id"]), "never closed outside the lock")
        self.assertTrue(at._turn_path(self.state, self.ctx["turn_id"], ".json").exists(), "the retry record")
        self.assertIn("proxytoken", self.lock.read_text(), "a live holder's lock is never broken")
        self.lock.unlink()
        self.assertTrue(at.close_turn(self.state, self.ctx["turn_id"]))
        self.assertTrue(at.is_closed(self.state, self.ctx["turn_id"]))

    def test_lock_edges(self):
        with mock.patch.object(at.os, "kill", side_effect=PermissionError):
            self.assertTrue(at._pid_alive(1), "another user's live process is alive")
        at._unlink_if_holding(self.lock, "1 t 2")          # already gone
        self.hold(4242)
        at._unlink_if_holding(self.lock, "1 t 2")
        self.assertTrue(self.lock.exists(), "another holder's lock stays")
        at._release_journal_lock(None)
        self.lock.unlink()
        self.lock.mkdir()                                   # unreadable: waited on, never broken
        with mock.patch.object(at, "JOURNAL_LOCK_WAIT_S", 0.05):
            self.assertIsNone(at._take_journal_lock(self.state))
        self.lock.rmdir()
        real_open, calls = os.open, []

        def vanishing(path, flags, *a):              # released between the O_EXCL refusal and the read
            calls.append(path)
            if len(calls) == 1:
                raise FileExistsError(path)
            return real_open(path, flags, *a)
        with mock.patch.object(at.os, "open", vanishing):
            held = at._take_journal_lock(self.state)
        self.assertIsNotNone(held)
        at._release_journal_lock(held)
        self.assertFalse(self.lock.exists())

    def test_the_default_wait_is_two_seconds(self):
        self.assertEqual(at.JOURNAL_LOCK_WAIT_S, 2.0)

    def test_a_dead_holder_is_broken(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.hold(proc.pid)
        self.assertTrue(at.close_turn(self.state, self.ctx["turn_id"]))
        self.assertFalse(self.lock.exists())

    def test_a_half_written_lock_is_waited_on_not_broken(self):
        self.lock.write_text("")
        with mock.patch.object(at, "JOURNAL_LOCK_WAIT_S", 0.1):
            self.assertFalse(at.close_turn(self.state, self.ctx["turn_id"]))
        self.assertTrue(self.lock.exists())
        self.assertFalse(at.is_closed(self.state, self.ctx["turn_id"]))


class TurnGate(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.turn = _ctx(self.state)["turn_id"]

    def _verdict(self, stdout="the answer", **kw):
        return at.classify_turn(at.read_turn_records(self.state, self.turn), stdout=stdout, **kw)

    def test_case1_text_only_turn_may_send_stdout(self):
        _journal(self.state, _intent("someone_else_g1_X", 1))
        v = self._verdict(server_operation_count=0)
        self.assertEqual((v.outcome, v.may_send_stdout, v.intent_count),
                         (at.INCOMPLETE_OR_ERROR, True, 0))
        self.assertFalse(self._verdict(stdout="  \n", server_operation_count=0).may_send_stdout)

    def test_case1_needs_core_api_to_confirm_zero_operations(self):
        self.assertFalse(self._verdict().may_send_stdout, "unknown server count")
        self.assertFalse(self._verdict(server_operation_count=1).may_send_stdout)

    def test_an_unbound_intent_since_the_turn_began_blocks_case1(self):
        unbound = {**_intent(self.turn, 1), "turn_id": None}
        _journal(self.state, {**unbound, "ts": 100.0})
        recs = at.read_turn_records(self.state, self.turn, issued_at=200.0)
        self.assertTrue(at.classify_turn(recs, stdout="x", server_operation_count=0).may_send_stdout,
                        "an unbound intent from before the turn does not count")
        _journal(self.state, {**unbound, "ts": 300.0})
        recs = at.read_turn_records(self.state, self.turn, issued_at=200.0)
        self.assertFalse(at.classify_turn(recs, stdout="x", server_operation_count=0).may_send_stdout)

    def test_turn_gate_suppresses_stdout_when_dispatch_intent_exists(self):
        _journal(self.state, _intent(self.turn, 1))
        records = at.read_turn_records(self.state, self.turn)
        self.assertEqual(len(at.unmatched_intents(records)), 1)
        v = at.classify_turn(records, stdout="the answer")
        self.assertEqual((v.outcome, v.may_send_stdout), (at.INCOMPLETE_OR_ERROR, False))
        _journal(self.state, _outcome(self.turn, 1))
        v = self._verdict()
        self.assertEqual((v.outcome, v.may_send_stdout), (at.INCOMPLETE_OR_ERROR, False))
        _journal(self.state, _intent(self.turn, 2, status="done"),
                 _outcome(self.turn, 2, op_status="failed", requested="done"))
        self.assertFalse(self._verdict().may_send_stdout)

    def test_terminal_confirmed_never_sends_stdout(self):
        _journal(self.state, _intent(self.turn, 1, status="done"),
                 _outcome(self.turn, 1, phase="responded", terminal=True, requested="done"))
        v = self._verdict()
        self.assertEqual((v.outcome, v.may_send_stdout), (at.TERMINAL_CONFIRMED, False))

    def test_turn_gate_durably_suspended_on_waiting_input_and_202(self):
        _journal(self.state, _intent(self.turn, 1, action="room.question.ask"),
                 _outcome(self.turn, 1, phase="waiting_input", blockers=1))
        v = self._verdict()
        self.assertEqual((v.outcome, v.may_send_stdout), (at.DURABLY_SUSPENDED, False))

        state = Path(tempfile.mkdtemp())
        turn = _ctx(state)["turn_id"]
        _journal(state, _intent(turn, 1, status="done"),
                 _outcome(turn, 1, op_status="accepted", requested="done"))
        v = at.classify_turn(at.read_turn_records(state, turn), stdout="x")
        self.assertEqual((v.outcome, v.may_send_stdout), (at.DURABLY_SUSPENDED, False))

    def test_a_resolved_blocker_is_not_a_suspension(self):
        _journal(self.state, _intent(self.turn, 1, action="room.question.ask"),
                 _outcome(self.turn, 1, phase="waiting_input", blockers=1),
                 _intent(self.turn, 2), _outcome(self.turn, 2, phase="working"))
        self.assertEqual(self._verdict().outcome, at.INCOMPLETE_OR_ERROR)
        _journal(self.state, _intent(self.turn, 3), _outcome(self.turn, 3, phase="working"))
        v = self._verdict(server_state={"terminal": False, "execution_phase": "waiting_input",
                                        "active_blocker_count": 1})
        self.assertEqual(v.outcome, at.DURABLY_SUSPENDED, "the task row is authoritative")

    def test_reconciled_outcome_fills_an_unmatched_intent(self):
        _journal(self.state, _intent(self.turn, 1, status="done"))
        records = at.read_turn_records(self.state, self.turn)
        rec = {"operation_id": f"op_{self.turn}_1", "operation_status": "succeeded",
               "applied_execution_phase": "responded", "applied_terminal": True}
        self.assertEqual(at.classify_turn(records, reconciled=[rec]).outcome, at.TERMINAL_CONFIRMED)
        unknown = {**rec, "operation_status": "dispatched_unknown", "applied_terminal": False}
        v = at.classify_turn(records, stdout="x", reconciled=[unknown])
        self.assertEqual((v.outcome, v.may_send_stdout), (at.INCOMPLETE_OR_ERROR, False))

    def test_unreadable_journal_or_closed_task_fails_closed(self):
        at.room_actions_path(self.state).mkdir()
        v = self._verdict()
        self.assertEqual((v.outcome, v.may_send_stdout), (at.INCOMPLETE_OR_ERROR, False))
        os.rmdir(at.room_actions_path(self.state))
        v = self._verdict(server_state={"terminal": True, "execution_phase": "cancelled"})
        self.assertFalse(v.may_send_stdout)

    def test_rotated_journal_is_read(self):
        rotated = at.room_actions_path(self.state).with_name(at.ROOM_ACTIONS_NAME + ".1")
        rotated.write_text(json.dumps(_intent(self.turn, 1)) + "\nnot json\n")
        self.assertFalse(self._verdict().may_send_stdout)

    def test_legacy_effective_task_status_outcome(self):
        _journal(self.state, _intent(self.turn, 1, status="done"),
                 {"record_type": at.OUTCOME, "turn_id": self.turn, "seq": 1,
                  "operation_id": f"op_{self.turn}_1", "operation_status": "succeeded",
                  "effective_task_status": "done"})
        self.assertEqual(self._verdict().outcome, at.TERMINAL_CONFIRMED)



class ResultFileDecision(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.turn = _ctx(self.state)["turn_id"]

    def _decide(self, body="final answer", **kw):
        return at.result_file_decision(at.read_turn_records(self.state, self.turn), body, **kw)

    def test_a_deliberate_result_after_progress_messages_is_posted(self):
        _journal(self.state, {**_intent(self.turn, 1), "body_sha256": at.body_sha256("on it")},
                 _outcome(self.turn, 1))
        d = self._decide()
        self.assertEqual((d.action, d.fields), (at.POST, {"status": "completed"}))

    def test_a_result_already_sent_as_a_room_message_is_not_resent(self):
        for outcome in ([_outcome(self.turn, 1)], [_outcome(self.turn, 1, op_status="dispatched_unknown")], []):
            with self.subTest(outcome=outcome):
                state = Path(tempfile.mkdtemp())
                turn = _ctx(state)["turn_id"]
                _journal(state, {**_intent(turn, 1), "body_sha256": at.body_sha256(" final answer\n")},
                         *[{**o, "turn_id": turn, "operation_id": f"op_{turn}_1"} for o in outcome])
                d = at.result_file_decision(at.read_turn_records(state, turn), "final answer")
                self.assertEqual(d.fields, {"status": "completed", "no_send": True})

    def test_terminal_turn_or_closed_task_settles_without_posting(self):
        _journal(self.state, _intent(self.turn, 1, status="done"),
                 _outcome(self.turn, 1, phase="responded", terminal=True, requested="done"))
        self.assertEqual(self._decide().action, at.SETTLE)
        state = Path(tempfile.mkdtemp())
        turn = _ctx(state)["turn_id"]
        d = at.result_file_decision(at.read_turn_records(state, turn), "x",
                                    server_state={"terminal": True, "execution_phase": "cancelled"})
        self.assertEqual(d.action, at.SETTLE)

    def test_a_suspended_turn_is_never_posted_as_completed(self):
        cases = [dict(phase="waiting_input", blockers=1), dict(phase="input_ready", blockers=0),
                 dict(phase="working", blockers=2), dict(phase="waiting_input", blockers=0)]
        for case in cases:
            with self.subTest(**case):
                state = Path(tempfile.mkdtemp())
                turn = _ctx(state)["turn_id"]
                _journal(state, _intent(turn, 1, action="room.question.ask"), _outcome(turn, 1, **case))
                for body, no_send in (("I asked the owner", False), ("", True)):
                    d = at.result_file_decision(at.read_turn_records(state, turn), body, no_send=no_send)
                    self.assertEqual((d.action, d.fields), (at.POST, {"status": "waiting_input", "no_send": True}))

    def test_a_turn_closed_terminally_through_mcp_skips_the_result_file(self):
        for phase in ("responded", "suppressed", "consolidated"):
            with self.subTest(phase=phase):
                state = Path(tempfile.mkdtemp())
                turn = _ctx(state)["turn_id"]
                _journal(state, _intent(turn, 1, status="done"),
                         _outcome(turn, 1, phase=phase, terminal=True, requested="done"))
                d = at.result_file_decision(at.read_turn_records(state, turn), "final answer")
                self.assertEqual((d.action, d.fields), (at.SETTLE, {}))

    def test_a_lost_or_unknown_ask_outcome_is_never_posted_as_the_reply(self):
        for outcome in (None, "dispatched_unknown"):
            with self.subTest(outcome=outcome):
                state = Path(tempfile.mkdtemp())
                turn = _ctx(state)["turn_id"]
                recs = [_intent(turn, 1, action="room.question.ask")]
                if outcome:
                    recs.append(_outcome(turn, 1, op_status=outcome))
                _journal(state, *recs)
                d = at.result_file_decision(at.read_turn_records(state, turn), "I asked which colour")
                self.assertEqual((d.action, d.fields), (at.POST, {"status": "waiting_input", "no_send": True}))
        state = Path(tempfile.mkdtemp())
        turn = _ctx(state)["turn_id"]
        _journal(state, _intent(turn, 1, action="room.question.ask"))
        failed = {"operation_id": f"op_{turn}_1", "operation_status": "failed", "turn_id": turn, "seq": 1}
        self.assertEqual(at.result_file_decision(at.read_turn_records(state, turn), "I asked",
                                                 reconciled=[failed]).fields,
                         {"status": "failed", "no_send": True}, "the ask failed: 'I asked' is never the reply")
        state = Path(tempfile.mkdtemp())
        turn = _ctx(state)["turn_id"]
        _journal(state, _intent(turn, 1, action="room.question.ask"), _outcome(turn, 1, op_status="failed"))
        self.assertEqual(at.result_file_decision(at.read_turn_records(state, turn), "I asked").fields,
                         {"status": "failed", "no_send": True}, "a failed ask in the journal itself")
        state = Path(tempfile.mkdtemp())
        turn = _ctx(state)["turn_id"]
        _journal(state, _intent(turn, 1), _outcome(turn, 1, op_status="dispatched_unknown"))
        self.assertEqual(at.result_file_decision(at.read_turn_records(state, turn), "the answer").fields,
                         {"status": "completed"}, "a lost message send is not a question")

    def test_an_outcome_whose_task_the_ledger_does_not_know_is_unknown_not_terminal_or_suspended(self):
        def unknown(turn, seq, action="room.message.send", status="done"):
            return {"record_type": at.OUTCOME, "turn_id": turn, "seq": seq, "action": action,
                    "operation_id": f"op_{turn}_{seq}", "operation_status": "succeeded",
                    "requested_task_status": status, "task": None, "applied_execution_phase": None,
                    "applied_terminal": None, "active_blocker_count": None, "blocker_id": None,
                    "effective_task_status": "done"}
        for action in ("room.message.send", "room.question.ask"):
            with self.subTest(action=action):
                state = Path(tempfile.mkdtemp())
                turn = _ctx(state)["turn_id"]
                _journal(state, _intent(turn, 1, status="done", action=action), unknown(turn, 1, action))
                recs = at.read_turn_records(state, turn)
                self.assertEqual(at.classify_turn(recs).outcome, at.INCOMPLETE_OR_ERROR)
                d = at.result_file_decision(recs, "final answer")
                self.assertEqual((d.action, d.fields), (at.POST, {"status": "completed"}),
                                 "no skip, no waiting_input: the result file is posted")
        state = Path(tempfile.mkdtemp())
        turn = _ctx(state)["turn_id"]
        _journal(state, {**_intent(turn, 1, status="done"), "body_sha256": at.body_sha256("final answer")},
                 unknown(turn, 1))
        d = at.result_file_decision(at.read_turn_records(state, turn), "final answer")
        self.assertEqual(d.fields, {"status": "completed", "no_send": True}, "the dedupe still applies")

    def test_a_corrupt_journal_line_is_unreadable_not_absent(self):
        valid = {**_intent(self.turn, 1), "body_sha256": at.body_sha256("final answer")}
        line = json.dumps({"ts": 1.0, **valid})
        path = at.room_actions_path(self.state)
        path.write_text(line + "\n")
        self.assertEqual(self._decide().fields, {"status": "completed", "no_send": True})
        path.write_text(line[:-2] + "\n")
        d = self._decide()
        self.assertEqual(d.fields, {"status": "failed", "no_send": True}, "never read as no send")
        path.write_text(line[:-2] + "\n" + json.dumps({"ts": 1.0, **_intent("other_g1_X", 1)}) + "\n")
        self.assertEqual(self._decide().fields, {"status": "failed", "no_send": True}, "mid-file too")

    def test_a_reply_sent_under_an_earlier_generation_is_not_sent_again(self):
        g1 = _ctx(self.state, gen=1)["turn_id"]
        g2 = _ctx(self.state, gen=2)["turn_id"]
        _journal(self.state, {**_intent(g1, 1), "body_sha256": at.body_sha256("final answer")},
                 {**_intent("tsk_B_g1_X", 1), "body_sha256": at.body_sha256("other answer")})
        records = at.read_turn_records(self.state, g2, task_id="tsk_A")
        self.assertEqual(at.result_file_decision(records, "final answer").fields,
                         {"status": "completed", "no_send": True})
        self.assertEqual(at.result_file_decision(records, "a new answer").fields, {"status": "completed"})
        other = at.read_turn_records(self.state, "tsk_B_g2_Y", task_id="tsk_B")
        self.assertEqual(at.result_file_decision(other, "final answer").fields, {"status": "completed"},
                         "another task's send never counts")

    def test_an_earlier_generations_failed_send_does_not_silence_the_new_reply(self):
        g1 = _ctx(self.state, gen=1)["turn_id"]
        g2 = _ctx(self.state, gen=2)["turn_id"]
        sent = {**_intent(g1, 1), "body_sha256": at.body_sha256("final answer")}
        for status, silenced in (("failed", False), ("rejected", False), ("succeeded", True),
                                 ("dispatched_unknown", True), (None, True)):
            with self.subTest(status=status):
                at.room_actions_path(self.state).unlink(missing_ok=True)
                _journal(self.state, sent, *([_outcome(g1, 1, op_status=status)] if status else []))
                d = at.result_file_decision(at.read_turn_records(self.state, g2, task_id="tsk_A"), "final answer")
                self.assertEqual(d.fields, {"status": "completed", "no_send": True} if silenced
                                 else {"status": "completed"})

    def test_a_stale_torn_line_with_no_turn_is_skipped_not_a_crash(self):
        path = at.room_actions_path(self.state)
        path.write_text('{"record_type": "dispatch_intent", "turn_id": "other_g1_X", "task_id": "oth\n')
        old = time.time() - at.PARTIAL_LINE_GRACE_S - 5
        os.utime(path, (old, old))
        recs = at.read_turn_records(self.state, None, None, task_id="tsk_A")
        self.assertTrue(recs.readable, "no turn of its own: another task's torn line is skipped")
        path.write_text('{"record_type": "dispatch_intent", "task_id": "tsk_A", "x\n')
        os.utime(path, (old, old))
        self.assertFalse(at.read_turn_records(self.state, None, None, task_id="tsk_A").readable)

    def test_another_tasks_torn_line_is_skipped(self):
        torn = json.dumps({"ts": 1.0, **_intent("other_g1_X", 1), "task_id": "other"})[:-2]
        valid = json.dumps({"ts": 1.0, **_intent(self.turn, 1), "body_sha256": at.body_sha256("final answer")})
        path = at.room_actions_path(self.state)
        path.write_text(torn + "\n" + valid + "\n")
        old = time.time() - at.PARTIAL_LINE_GRACE_S - 5
        os.utime(path, (old, old))
        self.assertEqual(self._decide().fields, {"status": "completed", "no_send": True},
                         "one task's torn append never fails every other task")
        self.assertTrue(at.read_turn_records(self.state, self.turn).readable)
        path.write_text(json.dumps({"ts": 1.0, "task_id": "tsk_A", "record_type": at.INTENT})[:-3] + "\n" + valid + "\n")
        self.assertFalse(at.read_turn_records(self.state, self.turn, task_id="tsk_A").readable,
                         "a torn line naming this task still fails it closed")

    def test_an_append_in_progress_defers_and_a_stale_one_is_corruption(self):
        path = at.room_actions_path(self.state)
        path.write_text(json.dumps({"ts": 1.0, **_intent(self.turn, 1)})[:-2])
        d = self._decide()
        self.assertEqual((d.action, d.fields), (at.DEFER, {}), "the proxy may still be writing it")
        self.assertFalse(at.classify_turn(at.read_turn_records(self.state, self.turn), stdout="x",
                                          server_operation_count=0).may_send_stdout)
        old = time.time() - at.PARTIAL_LINE_GRACE_S - 5
        os.utime(path, (old, old))
        self.assertEqual(self._decide().fields, {"status": "failed", "no_send": True})

    def test_unbound_records_naming_the_task_belong_to_its_turn(self):
        def unbound(rec, ts=300.0):
            return {**rec, "turn_id": None, "task_id": "tsk_A", "ts": ts}
        _journal(self.state, unbound({**_intent(self.turn, 1), "body_sha256": at.body_sha256("final answer")}))
        recs = at.read_turn_records(self.state, self.turn, issued_at=200.0, task_id="tsk_A")
        self.assertEqual((len(recs.intents), recs.unbound_intents), (1, ()))
        self.assertEqual(at.result_file_decision(recs, "final answer").fields,
                         {"status": "completed", "no_send": True})
        _journal(self.state, unbound(_outcome(self.turn, 1, phase="responded", terminal=True, requested="done")))
        recs = at.read_turn_records(self.state, self.turn, issued_at=200.0, task_id="tsk_A")
        self.assertEqual(at.result_file_decision(recs, "final answer").action, at.SETTLE)
        self.assertEqual(at.read_turn_records(self.state, self.turn, issued_at=400.0, task_id="tsk_A").intents,
                         [], "records from before the window are not this turn's")
        other = at.read_turn_records(self.state, self.turn, issued_at=200.0, task_id="tsk_B")
        self.assertEqual((other.intents, len(other.unbound_intents)), ([], 1))

    def test_suspended_no_send_and_unreadable(self):
        _journal(self.state, _intent(self.turn, 1, action="room.question.ask"),
                 _outcome(self.turn, 1, phase="waiting_input", blockers=1))
        self.assertEqual(self._decide().fields, {"status": "waiting_input", "no_send": True})
        state = Path(tempfile.mkdtemp())
        turn = _ctx(state)["turn_id"]
        recs = at.read_turn_records(state, turn)
        self.assertEqual(at.result_file_decision(recs, "", no_send=True).fields,
                         {"status": "no_action_needed"})
        at.room_actions_path(state).mkdir()
        self.assertEqual(at.result_file_decision(at.read_turn_records(state, turn), "x").fields,
                         {"status": "failed", "no_send": True})


if __name__ == "__main__":
    unittest.main()
