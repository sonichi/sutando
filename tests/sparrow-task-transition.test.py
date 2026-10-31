"""The attempt ledger's one writer (`ag2_sparrow/task_attempts.py`): its transitions and leaf commit.

Pins what every change to a task's attempt relies on: per-task transitions that never lose each
other's writes, a leaf commit that re-reads, compares, writes durably and fails closed, an
ownership epoch that revokes an older process, claims that commit only for the exact attempt
they were made for, read-only snapshots, the lock order, the run-id grammar, and that no provider
call is made while a transition is held. Each guard has a positive control.

Run:
    python3 tests/sparrow-task-transition.test.py
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO / "packages" / "ag2-sparrow"
sys.path.insert(0, str(PKG_ROOT))

from ag2_sparrow import agent_turn, gateway_instance, local_task_protocol, task_attempts  # noqa: E402

UNKNOWN = task_attempts.UNKNOWN


def _ack(gen=1):
    return {"ok": True, "execution_generation": gen, "attempt": gen, "lease_expires_at": 1.0}


class Ledger(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        self.logs = []

    def mk(self, **k):
        a = task_attempts.TaskAttempts(self.state, log=self.logs.append, **k)
        self.assertTrue(a.take_ownership())
        return a

    # ── the leaf commit ──────────────────────────────────────────────────
    def test_transitions_of_different_tasks_never_lose_each_others_writes(self):
        a = self.mk()
        tids = [f"tsk_{i}" for i in range(8)]
        start = threading.Barrier(len(tids))

        def writer(tid):
            start.wait()
            for n in range(25):
                with a.transition(tid) as t:
                    self.assertTrue(t.put(n=n))
        threads = [threading.Thread(target=writer, args=(t,)) for t in tids]
        for th in threads:
            th.start()
        for th in threads:
            th.join(30)
        self.assertEqual({t: a.get(t)["n"] for t in tids}, {t: 24 for t in tids}, "every task's last write stands")

    def test_without_the_leaf_lock_two_writers_lose_updates(self):
        """Positive control for the test above: two commits with the leaf serialisation removed."""
        a = self.mk()
        real_read, gate = a._read, threading.Barrier(2)

        def read_then_wait():
            data = real_read()
            try:
                gate.wait(1)                     # both read before either writes
            except threading.BrokenBarrierError:
                pass
            return data

        def write(tid):
            a._commit(lambda d: d.setdefault("tasks", {}).__setitem__(tid, {"x": 1}), why=tid)
        with unittest.mock.patch.object(a, "_leaf", unittest.mock.MagicMock()), \
                unittest.mock.patch.object(task_attempts.file_lock, "locked_file",
                                           lambda _p: unittest.mock.MagicMock()), \
                unittest.mock.patch.object(a, "_read", side_effect=read_then_wait):
            threads = [threading.Thread(target=write, args=(t,)) for t in ("tsk_p", "tsk_q")]
            for th in threads:
                th.start()
            for th in threads:
                th.join(10)
        self.assertEqual(len([t for t in ("tsk_p", "tsk_q") if a.get(t)]), 1, "one update is lost")
        for t in ("tsk_p", "tsk_q"):
            a._commit(lambda d, t=t: d.setdefault("tasks", {}).__setitem__(t, {"x": 1}), why=t)
        self.assertEqual(len([t for t in ("tsk_p", "tsk_q") if a.get(t)]), 2, "with the lock both stand")

    def test_a_commit_rereads_compares_and_refuses_a_record_changed_behind_it(self):
        a = self.mk()
        with a.transition("tsk_A") as t:
            self.assertTrue(t.put(x=1))
            data = json.loads(a.path.read_text())
            data["tasks"]["tsk_A"]["x"] = 99            # another writer, bypassing the lock
            a.path.write_text(json.dumps(data))
            self.assertFalse(t.put(y=2), "the record is not the one this transition last saw")
        self.assertEqual(dict(a.get("tsk_A")), {"x": 99})
        self.assertTrue(any("changed outside this transition" in m for m in self.logs))

    def test_a_commit_fsyncs_the_temp_file_and_the_directory(self):
        a = self.mk()
        with unittest.mock.patch.object(task_attempts.os, "fsync", wraps=os.fsync) as fsync, \
                unittest.mock.patch.object(agent_turn, "_fsync_dir", wraps=agent_turn._fsync_dir) as fsync_dir:
            with a.transition("tsk_A") as t:
                t.put(x=1)
        self.assertGreaterEqual(fsync.call_count, 1)
        self.assertEqual([c.args[0] for c in fsync_dir.call_args_list], [a.path.parent])

    def test_a_lock_read_or_write_failure_writes_nothing_and_fails_closed(self):
        a = self.mk()
        with a.transition("tsk_A") as t:
            t.put(x=1)
        before = a.path.read_bytes()
        faults = {
            "lock": unittest.mock.patch.object(task_attempts.file_lock, "locked_file",
                                               side_effect=OSError("no lock")),
            "read": unittest.mock.patch.object(a, "_read", return_value=None),
            "write": unittest.mock.patch.object(task_attempts.os, "replace", side_effect=OSError("full")),
        }
        for name, fault in faults.items():
            with self.subTest(fault=name), fault, a.transition("tsk_A") as t:
                self.assertFalse(t.put(x=2))
                self.assertIsNone(t.claim("ack", serve="L1"))
                self.assertIsNone(t.new_run("tsk_A"))
            self.assertEqual(a.path.read_bytes(), before, f"{name}: nothing written")
            self.assertEqual([p for p in self.state.iterdir() if p.name.startswith(f".{a.path.name}.")], [],
                             "no temp file is left")

    def test_another_format_or_a_damaged_ledger_is_unknown_and_never_owned(self):
        for text in ("{x", "[]", json.dumps({"format": 2, "tasks": {}}), json.dumps({"tsk_A": {}})):
            with self.subTest(text=text):
                self.state = Path(tempfile.mkdtemp())
                a = task_attempts.TaskAttempts(self.state, log=self.logs.append)
                a.path.write_text(text)
                self.assertFalse(a.take_ownership())
                self.assertIs(a.entry("tsk_A"), UNKNOWN)
                self.assertIsNone(a.records())
                self.assertEqual(a.path.read_text(), text)

    # ── ownership ────────────────────────────────────────────────────────
    def test_no_write_before_ownership_is_taken(self):
        a = task_attempts.TaskAttempts(self.state, log=self.logs.append)
        with a.transition("tsk_A") as t:
            self.assertFalse(t.put(x=1))
        self.assertTrue(any("ownership not taken" in m for m in self.logs))
        self.assertTrue(a.take_ownership())
        with a.transition("tsk_A") as t:
            self.assertTrue(t.put(x=1), "positive control: once owned it writes")

    def test_a_newer_owner_revokes_the_older_before_its_next_claim_or_effect(self):
        old, revoked = task_attempts.TaskAttempts(self.state, log=self.logs.append), []
        old.on_revoked = lambda: revoked.append(True)
        self.assertTrue(old.take_ownership())
        with old.transition("tsk_A") as t:
            t.remember_task({}, lease="L1")
            claim = t.claim("ack", serve="L1")
        new = self.mk()
        self.assertEqual(new.epoch, old.epoch + 1)
        with old.transition("tsk_A") as t:
            self.assertEqual(t.record_ack(claim, _ack(1)), None, "the older process's commit is refused")
            self.assertIsNone(t.claim("cancel-ack"), "and it claims nothing new")
        self.assertTrue(old.revoked)
        self.assertEqual(revoked, [True], "the bridge is told once")
        with self.assertRaises(task_attempts.Revoked):
            with old.transition("tsk_B"):
                pass
        with self.assertRaises(task_attempts.Revoked):
            with old.transition("tsk_A"):
                pass
        self.assertIsNone(new.get("tsk_A").get("execution_generation"))

    def test_the_new_owner_reconciles_the_older_claim_under_its_operation_id_exactly_once(self):
        old = self.mk()
        with old.transition("tsk_A") as t:
            t.remember_task({}, lease="L1")
            claim = t.claim("ack", serve="L1")
        effects = [claim["op"]]                          # the old process's effect, outcome unknown
        new = self.mk()
        with new.transition("tsk_A") as t:
            adopted = t.claim("ack", serve="L1")
            self.assertEqual((adopted["op"], adopted["epoch"], adopted.get("adopted")),
                             (claim["op"], new.epoch, True), "the same operation, now this process's")
        effects.append(adopted["op"])                    # the retry: the same idempotency key
        with new.transition("tsk_A") as t:
            self.assertEqual(t.record_ack(adopted, _ack(1))["execution_generation"], 1)
        self.assertEqual(set(effects), {claim["op"]}, "one operation id across both processes")
        before = old.path.read_bytes()
        with old.transition("tsk_A") as t:
            self.assertNotIsInstance(t.record_ack(claim, _ack(1)), dict, "the old process cannot commit it again")
            self.assertIsNone(t.claim("ack", serve="L1"), "nor claim it anew")
        self.assertEqual(old.path.read_bytes(), before, "nothing written")
        self.assertTrue(old.revoked)
        self.assertIsNone(new.get("tsk_A").get("claims"), "nothing left open")

    def test_two_processes_overlap_and_the_older_never_commits(self):
        """Old stalls after its claim; a new process takes the ledger over; old resumes, cannot
        commit or claim again; new reconciles the claim exactly once."""
        stall, go = self.state / "stalled", self.state / "go"
        script = textwrap.dedent(f"""
            import json, os, sys, time
            sys.path.insert(0, {str(PKG_ROOT)!r})
            from pathlib import Path
            from ag2_sparrow import task_attempts
            a = task_attempts.TaskAttempts(Path({str(self.state)!r}), log=print)
            assert a.take_ownership()
            with a.transition("tsk_A") as t:
                t.remember_task({{}}, lease="L1")
                c = t.claim("ack", serve="L1")
            Path({str(stall)!r}).write_text(c["op"])
            while not Path({str(go)!r}).exists():
                time.sleep(0.02)
            with a.transition("tsk_A") as t:
                committed = t.record_ack(c, {{"execution_generation": 1}})
            try:
                with a.transition("tsk_A") as t:
                    more = t.claim("cancel-ack")
            except task_attempts.Revoked:
                more = "revoked"
            print(json.dumps({{"committed": committed, "more": more, "revoked": a.revoked}}))
        """)
        old = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        for _ in range(500):
            if stall.exists():
                break
            threading.Event().wait(0.02)
        op = stall.read_text()
        new = self.mk()
        go.write_text("")
        out, _ = old.communicate(timeout=30)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(result, {"committed": None, "more": "revoked", "revoked": True},
                         "the older process neither commits nor starts more work")
        with new.transition("tsk_A") as t:
            adopted = t.claim("ack", serve="L1")
            self.assertEqual(adopted["op"], op)
            self.assertEqual(t.record_ack(adopted, _ack(1))["execution_generation"], 1)
        self.assertIsNone(new.get("tsk_A").get("claims"))

    # ── claims ───────────────────────────────────────────────────────────
    def test_a_claim_commits_only_while_it_owns_the_exact_attempt(self):
        a = self.mk()
        with a.transition("tsk_A") as t:
            t.remember_task({}, lease="L1")
            t.record_ack(t.claim("ack", serve="L1"), _ack(1))
            c = t.claim("result", file=[1, 2, 3])
            self.assertTrue(t.holds_claim(c), "positive control")
            self.assertEqual(t.claim("result", file=[1, 2, 3])["op"], c["op"], "the same claim is reused")
        with a.transition("tsk_A") as t:
            c = t.claim("result", file=[1, 2, 3])
            self.assertTrue(t.put(lease_id="L2"))
            self.assertTrue(t.holds_claim(c), "a lease rotated onto the same run is the same attempt")
            t.put(lease_id="L1")
        for change in ({"execution_generation": 2}, {"wake_episode": "e1"}, {"run": "tsk_A~~2"}):
            with self.subTest(change=change):
                with a.transition("tsk_A") as t:
                    c = t.claim("result", file=[1, 2, 3])
                    self.assertTrue(t.put(**change))
                    self.assertFalse(t.holds_claim(c))
                    self.assertFalse(t.commit_claim(c, x=1))
                    self.assertIsNone(t.rec.get("claims"), "a claim for the attempt left behind is dropped")
                    t.put(**{k: a_ for k, a_ in (("lease_id", "L1"), ("execution_generation", 1),
                                                 ("wake_episode", None), ("run", None))})

    def test_a_newer_result_ruling_replaces_the_earlier_claim(self):
        a = self.mk()
        with a.transition("tsk_A") as t:
            first = t.claim("result", sole=True, file=[1])
            second = t.claim("result", sole=True, file=[2])
            self.assertEqual([c["op"] for c in t.claims("result")], [second["op"]])
            self.assertFalse(t.holds_claim(first))

    def test_a_settle_without_a_post_waits_for_its_cancel_ack_and_a_post_moots_the_rest(self):
        a = self.mk()
        with a.transition("tsk_A") as t:
            t.record_ack(t.claim("ack", serve="L1"), _ack(1))
            t.fence({"error_code": "task_cancelled"})
            t.claim("cancel-ack", body=t.cancel_ack_body())
            self.assertFalse(t.finish(), "a settle without a post waits for its cancel-ack")
            result = t.claim("result")
            self.assertTrue(t.finish(result), "a posted result closes the lease: nothing else is owed")
        self.assertEqual(a.get("tsk_A"), {})

    # ── read-only snapshots ──────────────────────────────────────────────
    def test_every_record_read_outside_a_transition_is_read_only(self):
        a = self.mk()
        with a.transition("tsk_A") as t:
            t.put(x={"y": [1]}, taken_ctl=["c1"])
            inside = t.rec
        for rec in (a.entry("tsk_A"), a.get("tsk_A"), a.records()["tsk_A"], inside):
            for write in (lambda r: r.__setitem__("x", 1), lambda r: r.update(x=1), lambda r: r.pop("x"),
                          lambda r: r["x"].__setitem__("y", 2), lambda r: r["x"]["y"].append(2),
                          lambda r: r["taken_ctl"].append("c2")):
                with self.assertRaises(TypeError):
                    write(rec)
        copy = {**a.get("tsk_A"), "x": 1}
        self.assertEqual(copy["x"], 1, "positive control: a copy is the caller's own")
        self.assertEqual(a.get("tsk_A")["x"], {"y": [1]}, "and never reaches the ledger")

    # ── locks ────────────────────────────────────────────────────────────
    def test_one_task_at_a_time_reentry_shares_the_transition_and_an_error_releases_it(self):
        a = self.mk()
        with a.transition("tsk_A") as t:
            with a.transition("tsk_A") as again:
                self.assertIs(again, t, "re-entered by the same thread, it is the same transition")
            with self.assertRaises(task_attempts.LockOrder):
                with a.transition("tsk_B"):
                    pass
        self.assertEqual(task_attempts.holding(), ())
        with self.assertRaises(RuntimeError):
            with a.transition("tsk_A"):
                raise RuntimeError("boom")
        self.assertEqual(task_attempts.holding(), ())
        done = threading.Event()
        threading.Thread(target=lambda: (a.transition("tsk_A").__enter__(), done.set())).start()
        self.assertTrue(done.wait(5), "the lock was released by the error")

    def test_a_transition_used_after_it_closed_refuses(self):
        a = self.mk()
        with a.transition("tsk_A") as t:
            self.assertTrue(t.put(x=1), "positive control")
        with self.assertRaises(task_attempts.LockOrder):
            t.put(x=2)
        self.assertEqual(a.get("tsk_A")["x"], 1)

    def test_one_tasks_held_transition_never_stops_anothers(self):
        a = self.mk()
        held, release = threading.Event(), threading.Event()

        def hold_a():
            with a.transition("tsk_A"):
                held.set()
                release.wait(10)
        th = threading.Thread(target=hold_a)
        th.start()
        self.assertTrue(held.wait(5))
        with a.transition("tsk_B") as t:
            self.assertTrue(t.put(x=1), "task B goes on while task A is held")
        blocked = threading.Thread(target=lambda: a.transition("tsk_A").__enter__())
        blocked.daemon = True
        blocked.start()
        blocked.join(0.2)
        self.assertTrue(blocked.is_alive(), "positive control: task A itself waits")
        release.set()
        th.join(5)

    def test_no_provider_call_inside_a_transition(self):
        a = self.mk()
        task_attempts.assert_unlocked("outside")         # positive control: allowed with nothing held
        with a.transition("tsk_A"):
            with self.assertRaises(task_attempts.UnderLock):
                task_attempts.assert_unlocked("POST /v1/tasks/tsk_A/ack")

    def test_the_module_makes_no_provider_call(self):
        src = (PKG_ROOT / "ag2_sparrow" / "task_attempts.py").read_text()
        tree = ast.parse(src)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse({"request", "urlopen", "_req"} & names, "the bridge makes every provider call")
        imports = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        self.assertFalse({"urllib.request", "http.client", "socket"} & imports)

    def test_the_lock_table_holds_only_tasks_in_use(self):
        a = self.mk()
        for i in range(50):
            with a.transition(f"tsk_{i}") as t:
                t.put(x=i)
        self.assertEqual(a._locks, {}, "no lock is kept for a task no thread is in")
        held, release = threading.Event(), threading.Event()

        def hold():
            with a.transition("tsk_live"):
                held.set()
                release.wait(5)
        th = threading.Thread(target=hold)
        th.start()
        self.assertTrue(held.wait(5))
        self.assertEqual(list(a._locks), ["tsk_live"], "positive control: the one in use is kept")
        release.set()
        th.join(5)

    # ── runs ─────────────────────────────────────────────────────────────
    def test_a_run_id_is_injective_and_parsed_by_one_owner(self):
        cases = [("task-abc", 1, "task-abc"), ("task-abc", 2, "task-abc~~2"), ("task-foo.a2", 1, "task-foo.a2"),
                 ("task-foo", 2, "task-foo~~2"), ("task-dev~ask-1", 3, "task-dev~ask-1~~3"),
                 ("ask-x.y", 12, "ask-x.y~~12")]
        seen = set()
        for base, seq, local in cases:
            self.assertEqual(local_task_protocol.run_task_id(base, seq), local)
            self.assertEqual(local_task_protocol.split_run_id(local), (base, seq))
            seen.add(local)
        self.assertEqual(len(seen), len(cases), "no two (task, run) pairs share an id")
        self.assertNotEqual(local_task_protocol.run_task_id("task-foo", 2), "task-foo.a2",
                            "a dot in a broker id is never read as a run")
        for bad in ("task-a~~1", "task-a~~02", "task-a~~", "task-a~~2~~3", "~~2", "task-a~~x"):
            with self.assertRaises(ValueError):
                local_task_protocol.split_run_id(bad)
            self.assertFalse(local_task_protocol.valid_local_task_id(bad))
        for bad in (("task-a~~2", 2), ("task-a", 0), ("task-a", "2")):
            with self.assertRaises(ValueError):
                local_task_protocol.run_task_id(*bad)
        self.assertTrue(local_task_protocol.valid_local_task_id("task-dev~ask-1~~3"))
        self.assertTrue(local_task_protocol.valid_archive_lookup_id("task-dev~ask-1~~3"))
        self.assertEqual(gateway_instance.broker_tid("task-dev~ask-1~~3", "dev"), "ask-1")
        self.assertEqual(gateway_instance.broker_tid("task-abc~~2", ""), "task-abc")
        self.assertTrue(gateway_instance.owns("task-abc~~2", ""), "a primary's later run is still its own")
        self.assertFalse(gateway_instance.owns("task-dev~x~~2", ""))
        self.assertEqual(gateway_instance.broker_tid("task-a~~1", ""), "task-a~~1", "a malformed run is left as it is")

    def test_runs_are_allocated_before_publication_and_never_reused(self):
        a = self.mk()
        with a.transition("tsk_A") as t:
            self.assertEqual(t.new_run("task-A"), "task-A")
            self.assertFalse(t.run_published("task-A"))
            self.assertTrue(t.mark_published("task-A"))
            self.assertEqual(t.new_run("task-A"), "task-A~~2")
            self.assertTrue(t.finish())
        self.assertEqual(a.run_of("task-A"), ("tsk_A", 1, False), "a closed run still maps to its task")
        with a.transition("tsk_A") as t:
            self.assertEqual(t.last_run("task-A"), "task-A~~2")
            self.assertEqual(t.new_run("task-A"), "task-A~~3", "a retired task's ids are not handed out again")
        self.assertEqual(a.run_of("task-A~~3"), ("tsk_A", 3, True))
        self.assertIsNone(a.run_of("task-A~~9"), "an id this node never allocated has no mapping")
        self.assertIsNone(a.run_of("task-other"))
        with a.transition("tsk_B") as t:
            self.assertFalse(t.mark_published("task-A~~3"), "a run is published only by its own task")

    def test_a_run_id_is_never_reused_after_the_counters_are_trimmed(self):
        a = self.mk()
        on_disk = {"task-T", "task-T~~2"}
        with unittest.mock.patch.object(task_attempts, "RUNS_KEPT", 3):
            with a.transition("tsk_T") as t:
                self.assertEqual(t.new_run("task-T", used=on_disk.__contains__), "task-T~~3",
                                 "an id with anything on disk is skipped, the bare one included")
            with a.transition("tsk_live") as t:
                live = t.new_run("task-live")
            for i in range(6):                          # finished tasks push the counters out
                with a.transition(f"tsk_{i}") as t:
                    t.new_run(f"task-{i}")
                    t.forget()
            seqs = json.loads(a.path.read_text())["seqs"]
            self.assertIn("tsk_live", seqs, "a task the ledger holds keeps its counter")
            self.assertNotIn("tsk_0", seqs, "positive control: a finished task's counter is trimmed")
            with a.transition("tsk_live") as t:
                self.assertEqual(t.new_run("task-live"), "task-live~~2", "never the live run's own id")
            with a.transition("tsk_0") as t:
                self.assertEqual(t.new_run("task-0", used={"task-0"}.__contains__), "task-0~~2",
                                 "a trimmed task's first id is on disk, so it is not handed out again")
        del live


if __name__ == "__main__":
    unittest.main()
