#!/usr/bin/env python3
"""Executable model of the pool's on-disk protocol: one immutable payload, and one
delivery sentinel per recipient whose existence is the assignment.

A model, not the shipped code — the design lands before the implementation. It runs
against a real temp directory so the transitions it claims are atomic are exercised
by the kernel that will run them.

    tasks/<id>.json                     the payload; immutable, never copied
    deliveries/<recipient>/<id>         a sentinel — existing IS the assignment
    deliveries/<recipient>/<id>.claimed the same sentinel, suffix substituted
    (sentinel removed, payload archived) finish

Creating the sentinel assigns; renaming it claims. The suffix substitutes, never
appends, and the canonical id is stable throughout.
"""
import os
import pathlib
import re
import shutil
import tempfile
import unittest

BEAT_STALE_S = 90
SUFFIX = re.compile(r"^(?P<id>task-[A-Za-z0-9_-]+?)(?:\.(?P<state>claimed))?$")


def parse(name):
    """(id, state) from a sentinel filename. `None` state means unclaimed."""
    m = SUFFIX.match(name)
    if not m:
        return None
    return m.group("id"), m.group("state")


class Pool:
    """The shared workspace. Every party below writes only through this."""

    def __init__(self, root):
        self.root = pathlib.Path(root)
        for d in ("tasks/archive", "deliveries", "results", "state/workers"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        # roster: core-compiled, router-readonly. states: live|recovering|abandoned|retired
        self.roster = None
        self.states = {}
        self.beats = {}
        self.now = 1000

    # --- layout ----------------------------------------------------------
    def payload(self, task_id):
        return self.root / "tasks" / f"{task_id}.json"

    def inbox(self, worker):
        """The recipient's delivery folder — sentinels only, never payloads."""
        d = self.root / "deliveries" / worker
        d.mkdir(parents=True, exist_ok=True)
        return d

    def result(self, task_id):
        return self.root / "results" / f"{task_id}.txt"

    def flag(self, worker, task_id):
        d = self.root / "state" / "workers" / worker / "done"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{task_id}.flag"

    def archived(self, task_id):
        return self.root / "tasks" / "archive" / f"{task_id}.json"

    def archive_target(self, task_id):
        """No-clobber: a colliding name mints .1, .2 rather than replacing."""
        base = self.archived(task_id)
        if not base.exists():
            return base
        n = 1
        while base.with_name(f"{base.name}.{n}").exists():
            n += 1
        return base.with_name(f"{base.name}.{n}")

    def watcher(self, recipient):
        return self.root / "state" / "watchers" / f"{recipient}.alive"

    def find(self, worker, task_id):
        for p in self.inbox(worker).iterdir():
            got = parse(p.name)
            if got and got[0] == task_id:
                return p
        return None

    def progressed(self, worker):
        d = self.root / "state" / "workers" / worker / "done"
        return d.exists() and any(d.iterdir())

    def ordered(self, tasks):
        """urgent > normal > low, then oldest created_at first."""
        rank = {"urgent": 0, "normal": 1, "low": 2}
        return sorted(tasks, key=lambda t: (rank[t["priority"]], t["created_at"]))

    def beat_is_stale(self, worker):
        # A future-dated beat counts as stale: clock skew degrades, never wedges.
        return not (0 <= self.now - self.beats.get(worker, -10**9) < BEAT_STALE_S)


class TaskBridge:
    """The one admission gate. Nothing reaches an inbox except through here."""

    def __init__(self, pool):
        self.pool, self.admitted = pool, []

    def admit(self, task_id, submitter_tier, body=""):
        if submitter_tier != "owner":
            raise PermissionError(f"{task_id}: tier {submitter_tier} not authorised")
        self.admitted.append(task_id)
        return {"id": task_id, "body": body}


class Core:
    """Compiles the roster, owns worker states and worker recovery."""

    def __init__(self, pool):
        self.pool = pool
        self.diagnosed = []

    def compile_roster(self, bindings):
        """Deterministic, two branches: a declared target, or the core."""
        self.pool.roster = dict(bindings)

    def set_state(self, worker, state):
        self.pool.states[worker] = state

    def inspect(self):
        """Work-not-process sampling. Returns the signal, never the remedy."""
        out = []
        for w, st in self.pool.states.items():
            if st == "paused":
                continue
            if self.pool.beat_is_stale(w):
                out.append((w, "process-death"))
            elif any(parse(q.name)[1] == "claimed"
                     for q in self.pool.inbox(w).iterdir()
                     if parse(q.name)) and not self.pool.progressed(w):
                out.append((w, "task-stalled"))
        return out

    def create_worker(self, name):
        """A worker with no delivery mechanism cannot receive work, so it gets
        one at creation."""
        self.pool.states[name] = "live"
        self.pool.beats[name] = self.pool.now
        self.pool.watcher(name).parent.mkdir(parents=True, exist_ok=True)
        self.pool.watcher(name).touch()
        self.pool.inbox(name)

    def remove_worker(self, name):
        self.pool.states[name] = "retired"
        if self.pool.watcher(name).exists():
            self.pool.watcher(name).unlink()


class Router:
    """Placement only. Its whole input is the roster and the task."""

    def __init__(self, pool):
        self.pool = pool

    def place(self, task, declared):
        if self.pool.roster is None:
            raise LookupError("no roster: refusing to place")
        if declared is None:
            targets = ["core"]
        elif isinstance(declared, (list, tuple)):
            targets = list(declared)          # every member; no subset
        else:
            targets = [declared]
        for t in targets:
            if t != "core" and t not in self.pool.roster:
                raise KeyError(f"{t} does not exist")
        placed = []
        for t in targets:
            if self.pool.states.get(t) not in (None, "live"):
                placed.append((t, None))      # holds; substitutes nobody
                continue
            tid = task["id"] if len(targets) == 1 else f"{task['id']}-{t}"
            # the payload is written ONCE and never copied per recipient
            pay = self.pool.payload(tid)
            if not pay.exists():
                pay.write_text(f'{{"id": "{tid}", "body": "{task.get("body","")}"}}')
            dst = self.pool.inbox(t) / tid          # a sentinel: no content needed
            if self.pool.find(t, tid) is not None:
                placed.append((t, self.pool.find(t, tid)))
                continue                             # already delivered, claimed or not
            try:
                os.close(os.open(dst, os.O_CREAT | os.O_EXCL, 0o644))
            except FileExistsError:
                pass                                 # the pass is idempotent
            placed.append((t, dst))
        return placed

    def reclaim(self, worker, task_id):
        """Authorised by the core's `abandoned` verdict, never by a stale beat."""
        if self.pool.states.get(worker) != "abandoned":
            return None
        p = self.pool.find(worker, task_id)
        if p is None:
            return None
        dst = p.with_name(task_id)
        os.rename(p, dst)
        return dst


class FinishRefused(Exception):
    pass


class Worker:
    """Single-purpose: it acts on assignments in its own inbox."""

    def __init__(self, pool, name):
        self.pool, self.name = pool, name

    def claimable(self):
        out = []
        for p in sorted(self.pool.inbox(self.name).iterdir()):
            got = parse(p.name)
            if got and got[1] is None:
                out.append(p)
        return out

    def claim(self, path):
        tid, state = parse(path.name)
        if state is not None or path.parent != self.pool.inbox(self.name):
            raise PermissionError(f"{self.name} may not claim {path}")
        dst = path.with_name(f"{tid}.claimed")
        os.rename(path, dst)          # raises OSError if the router won the race
        return dst

    def finish(self, path, body):
        """The single completion path. A refusal writes nothing at all."""
        tid, state = parse(path.name)
        if state != "claimed" or path.parent != self.pool.inbox(self.name):
            raise FinishRefused("caller does not hold this claim")
        first = body.splitlines()[0] if body.strip() else ""
        if first.strip() != f"task: {tid}":
            raise FinishRefused(f"pairing echo missing or wrong: {first!r}")
        # Fixed order: result, then done-flag, then archive.
        self.pool.result(tid).write_text(body)
        self.pool.flag(self.name, tid).write_text("")
        path.unlink()                                    # sentinel removed
        if self.pool.payload(tid).exists():
            os.rename(self.pool.payload(tid), self.pool.archive_target(tid))
        return tid

    def residue(self, task_id):
        """What a crash left behind, read without a journal."""
        has_result = self.pool.result(task_id).is_file()
        has_flag = self.pool.flag(self.name, task_id).is_file()
        held = self.pool.find(self.name, task_id)
        state = parse(held.name)[1] if held else None
        if has_result and not has_flag:
            return "completed"
        if state == "claimed" and not has_result:
            return "died-mid-work"
        if held is not None and not self.pool.payload(task_id).is_file():
            return "stale-sentinel"
        return "clean"


class PoolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pool = Pool(self.tmp)
        self.core = Core(self.pool)
        self.bridge = TaskBridge(self.pool)
        self.router = Router(self.pool)
        self.core.compile_roster({"worker-1": {}, "worker-2": {}})
        for w in ("worker-1", "worker-2"):
            self.core.set_state(w, "live")
            self.pool.beats[w] = self.pool.now
        self.w1 = Worker(self.pool, "worker-1")
        self.w2 = Worker(self.pool, "worker-2")

    def admit_and_place(self, tid, declared, tier="owner"):
        task = self.bridge.admit(tid, tier)
        return self.router.place(task, declared)


class Admission(PoolCase):
    def test_a_non_owner_submitter_never_reaches_an_inbox(self):
        with self.assertRaises(PermissionError):
            self.bridge.admit("task-1", "guest")
        self.assertEqual(list(self.pool.inbox("worker-1").iterdir()), [])

    def test_placement_without_a_roster_refuses_rather_than_defaulting(self):
        self.pool.roster = None
        with self.assertRaises(LookupError):
            self.router.place({"id": "task-1"}, "worker-1")


class Placement(PoolCase):
    def test_nothing_declared_goes_to_the_core(self):
        [(target, path)] = self.admit_and_place("task-1", None)
        self.assertEqual(target, "core")
        self.assertEqual(parse(path.name), ("task-1", None))
        self.assertEqual(path.parent.name, "core")

    def test_a_declared_target_is_assigned_to_that_worker(self):
        [(target, path)] = self.admit_and_place("task-1", "worker-2")
        self.assertEqual(target, "worker-2")
        self.assertEqual(path.parent.name, "worker-2")
        self.assertTrue(self.pool.payload("task-1").is_file(), "payload not written")

    def test_a_set_writes_one_payload_and_one_sentinel_per_member(self):
        """The reason content and delivery are separate: N recipients must not
        mean N copies of the task."""
        placed = self.admit_and_place("task-1", ["worker-1", "worker-2"])
        for _, path in placed:
            self.assertEqual(path.stat().st_size, 0, "a sentinel carried content")
        payloads = sorted(q.name for q in (self.pool.root / "tasks").glob("*.json"))
        self.assertEqual(payloads, ["task-1-worker-1.json", "task-1-worker-2.json"])
        for _, path in placed:
            self.assertTrue(self.pool.payload(parse(path.name)[0]).is_file())

    def test_a_set_delivers_to_every_member_and_selects_no_subset(self):
        placed = self.admit_and_place("task-1", ["worker-1", "worker-2"])
        self.assertEqual([t for t, _ in placed], ["worker-1", "worker-2"])
        # Ids derive from (parent, worker), so a restart re-mints the same names.
        self.assertEqual([parse(p.name)[0] for _, p in placed],
                         ["task-1-worker-1", "task-1-worker-2"])

    def test_an_unavailable_target_holds_and_is_never_substituted(self):
        self.core.set_state("worker-2", "recovering")
        [(target, path)] = self.admit_and_place("task-1", "worker-2")
        self.assertEqual((target, path), ("worker-2", None))
        self.assertEqual(list(self.pool.inbox("worker-1").iterdir()), [],
                         "a held task was substituted onto another worker")

    def test_a_target_that_does_not_exist_fails_loudly(self):
        with self.assertRaises(KeyError):
            self.admit_and_place("task-1", "worker-9")


class FilenameIsTheState(PoolCase):
    def test_suffixes_are_substituted_not_appended(self):
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        claimed = self.w1.claim(path)
        self.assertEqual(claimed.name, "task-1.claimed")
        self.assertEqual(claimed.name.count("claimed"), 1, "suffix accumulated")
        self.assertEqual(parse(claimed.name)[0], "task-1", "canonical id moved")

    def test_an_assigned_glob_cannot_re_match_a_file_already_claimed(self):
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        self.w1.claim(path)
        self.assertEqual(self.w1.claimable(), [])

    def test_archiving_restores_the_bare_canonical_name(self):
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        claimed = self.w1.claim(path)
        self.w1.finish(claimed, "task: task-1\ndone")
        self.assertTrue(self.pool.archived("task-1").is_file())
        self.assertEqual(self.pool.archived("task-1").name, "task-1.json")
        self.assertIsNone(self.pool.find("worker-1", "task-1"), "sentinel outlived finish")


class WorkersDoNotSelectWork(PoolCase):
    def test_a_worker_cannot_claim_an_assignment_addressed_to_another(self):
        [(_, path)] = self.admit_and_place("task-1", "worker-2")
        with self.assertRaises(PermissionError):
            self.w1.claim(path)

    def test_a_payload_alone_is_claimable_by_nobody(self):
        """A task with no delivery reaches no one — existence in tasks/ is not
        an assignment."""
        self.pool.payload("task-1").write_text('{"id": "task-1"}')
        self.assertEqual(self.w1.claimable(), [])
        self.assertEqual(list(self.pool.inbox("worker-1").iterdir()), [])

    def test_a_worker_sees_only_its_own_inbox(self):
        self.admit_and_place("task-1", "worker-2")
        self.assertEqual(self.w1.claimable(), [])
        self.assertEqual(len(self.w2.claimable()), 1)


class ReclaimKeysOnAbandoned(PoolCase):
    def test_a_stale_beat_alone_reclaims_nothing(self):
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        claimed = self.w1.claim(path)
        self.pool.now += BEAT_STALE_S + 60          # the beat is now stale
        self.assertTrue(self.pool.beat_is_stale("worker-1"))
        self.assertIsNone(self.router.reclaim("worker-1", "task-1"))
        self.assertTrue(claimed.is_file(), "a stale beat repooled held work")

    def test_the_cores_abandoned_verdict_authorises_reclaim(self):
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        self.w1.claim(path)
        self.core.set_state("worker-1", "abandoned")
        back = self.router.reclaim("worker-1", "task-1")
        self.assertEqual(parse(back.name), ("task-1", None))
        self.assertEqual(back.parent.name, "worker-1",
                         "release handed the task to a different worker")

    def test_a_host_sleep_stales_every_beat_at_once_and_reclaims_none(self):
        for w, worker in (("worker-1", self.w1), ("worker-2", self.w2)):
            [(_, path)] = self.admit_and_place(f"task-{w}", w)
            worker.claim(path)
        self.pool.now += 4 * 3600                    # the host slept
        signals = self.core.inspect()
        self.assertEqual(sorted(s[0] for s in signals), ["worker-1", "worker-2"])
        for w in ("worker-1", "worker-2"):
            self.assertIsNone(self.router.reclaim(w, f"task-{w}"))

    def test_an_owner_paused_worker_outranks_the_death_signal(self):
        self.core.set_state("worker-1", "paused")
        self.pool.now += 4 * 3600
        self.assertNotIn("worker-1", [s[0] for s in self.core.inspect()])


class CompletionIsOrdered(PoolCase):
    def test_the_happy_path_writes_result_then_flag_then_archive(self):
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        claimed = self.w1.claim(path)
        self.w1.finish(claimed, "task: task-1\nthe answer")
        self.assertTrue(self.pool.result("task-1").is_file())
        self.assertTrue(self.pool.flag("worker-1", "task-1").is_file())
        self.assertTrue(self.pool.archived("task-1").is_file())
        self.assertIsNone(self.pool.find("worker-1", "task-1"))

    def test_a_result_with_no_flag_reads_as_completed_and_never_re_runs(self):
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        claimed = self.w1.claim(path)
        self.pool.result("task-1").write_text("task: task-1\npartial")   # crash here
        self.assertEqual(self.w1.residue("task-1"), "completed")
        self.assertTrue(claimed.is_file())

    def test_a_claim_with_no_result_reads_as_died_mid_work(self):
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        self.w1.claim(path)
        self.assertEqual(self.w1.residue("task-1"), "died-mid-work")

    def test_done_flags_are_namespaced_per_worker(self):
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        self.w1.finish(self.w1.claim(path), "task: task-1\nok")
        self.assertTrue(self.pool.flag("worker-1", "task-1").is_file())
        self.assertFalse(self.pool.flag("worker-2", "task-1").is_file())


class TheFinishGate(PoolCase):
    def setUp(self):
        super().setUp()
        for tid in ("task-a", "task-b"):
            [(_, path)] = self.admit_and_place(tid, "worker-1")
            setattr(self, tid.replace("-", "_"), self.w1.claim(path))

    def test_a_body_echoing_the_wrong_claim_is_refused(self):
        with self.assertRaises(FinishRefused):
            self.w1.finish(self.task_a, "task: task-b\nanswer meant for b")

    def test_a_refusal_writes_nothing_at_all(self):
        with self.assertRaises(FinishRefused):
            self.w1.finish(self.task_a, "task: task-b\nanswer meant for b")
        self.assertFalse(self.pool.result("task-a").is_file())
        self.assertFalse(self.pool.result("task-b").is_file())
        self.assertFalse(self.pool.flag("worker-1", "task-a").is_file())
        self.assertTrue(self.task_a.is_file(), "a refused finish moved the task")

    def test_an_empty_body_is_refused(self):
        with self.assertRaises(FinishRefused):
            self.w1.finish(self.task_a, "   \n")

    def test_a_worker_cannot_finish_a_claim_it_does_not_hold(self):
        with self.assertRaises(FinishRefused):
            self.w2.finish(self.task_a, "task: task-a\nnot mine")

    def test_the_correct_echo_completes(self):
        self.assertEqual(self.w1.finish(self.task_a, "task: task-a\nok"), "task-a")


class ClaimsTheDocMakesThatNothingElseChecked(PoolCase):
    """One test per claim the design states and the suite did not yet exercise."""

    def test_a_repeated_router_pass_delivers_once(self):
        """The pass must be idempotent, or a retry double-assigns."""
        a = self.admit_and_place("task-1", "worker-1")
        b = self.router.place({"id": "task-1"}, "worker-1")
        self.assertEqual(a[0][1], b[0][1])
        self.assertEqual(len(list(self.pool.inbox("worker-1").iterdir())), 1)

    def test_a_repeated_pass_does_not_resurrect_a_claimed_delivery(self):
        """A claimed sentinel is renamed, so a pass that only checks the unclaimed
        name would create a second one and deliver the work twice."""
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        self.w1.claim(path)
        self.router.place({"id": "task-1"}, "worker-1")
        names = sorted(q.name for q in self.pool.inbox("worker-1").iterdir())
        self.assertEqual(names, ["task-1.claimed"])

    def test_archiving_never_clobbers_an_existing_record(self):
        self.pool.archived("task-1").parent.mkdir(parents=True, exist_ok=True)
        self.pool.archived("task-1").write_text("an earlier run")
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        self.w1.finish(self.w1.claim(path), "task: task-1\nsecond run")
        self.assertEqual(self.pool.archived("task-1").read_text(), "an earlier run")
        self.assertTrue(self.pool.archived("task-1").with_suffix(".json.1").is_file())

    def test_priority_orders_before_age(self):
        q = [{"priority": "normal", "created_at": 1}, {"priority": "urgent", "created_at": 9},
             {"priority": "low", "created_at": 0}, {"priority": "normal", "created_at": 0}]
        self.assertEqual([(t["priority"], t["created_at"]) for t in self.pool.ordered(q)],
                         [("urgent", 9), ("normal", 0), ("normal", 1), ("low", 0)])

    def test_a_sentinel_whose_payload_is_gone_is_removed(self):
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        self.pool.payload("task-1").unlink()
        self.assertEqual(self.w1.residue("task-1"), "stale-sentinel")

    def test_a_payload_with_no_sentinel_is_left_for_the_router(self):
        self.bridge.admit("task-1", "owner")
        self.pool.payload("task-1").write_text('{"id": "task-1"}')
        self.assertEqual(self.w1.residue("task-1"), "clean")
        self.assertTrue(self.pool.payload("task-1").is_file())

    def test_a_stall_is_a_different_signal_from_a_death(self):
        """Both are anomalies; only one authorises a restart."""
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        self.w1.claim(path)
        self.pool.beats["worker-1"] = self.pool.now      # beating, but not progressing
        self.assertIn(("worker-1", "task-stalled"), self.core.inspect())
        self.assertNotIn(("worker-1", "process-death"), self.core.inspect())

    def test_creating_a_worker_creates_its_delivery_mechanism(self):
        """The invariant: a worker with no watcher cannot receive work."""
        self.core.create_worker("worker-9")
        self.assertTrue(self.pool.watcher("worker-9").is_file())
        self.assertTrue(self.pool.inbox("worker-9").is_dir())
        self.core.remove_worker("worker-9")
        self.assertFalse(self.pool.watcher("worker-9").exists())


class TheOneContestedTransition(PoolCase):
    def test_claim_and_reclaim_race_leaves_exactly_one_winner(self):
        [(_, path)] = self.admit_and_place("task-1", "worker-1")
        self.core.set_state("worker-1", "abandoned")
        # Both parties resolved the same source name; the kernel picks one.
        self.w1.claim(path)
        self.assertFalse(path.exists())
        with self.assertRaises(OSError):
            os.rename(path, path.with_name("task-1"))
        survivors = [p.name for p in self.pool.inbox("worker-1").iterdir()]
        self.assertEqual(survivors, ["task-1.claimed"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
