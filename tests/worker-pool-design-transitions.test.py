#!/usr/bin/env python3
"""Model of the task store's state machine: guarded transitions, a lease clock,
a crash seam between any two writes, and supervisor restart re-reading the store.
"""
import itertools
import unittest

TERMINAL = ("SUCCEEDED", "FAILED")
NON_TERMINAL = ("PENDING", "OFFERED", "ACCEPTED", "RUNNING")
LEASE_S = 60
PROBE_TIMEOUT_S = 30


class Clock:
    def __init__(self, now=1000):
        self.now = now

    def advance(self, secs):
        self.now += secs
        return self.now


class Store:
    """Every method is one guarded UPDATE returning the rows it matched.
    A zero is the authoritative answer that the transition did not happen."""

    def __init__(self):
        self.rows = {}

    def _match(self, task_id, states, owner=None, attempt=None):
        r = self.rows.get(task_id)
        if r is None or r["state"] not in states:
            return None
        if owner is not None and r["lease_owner"] != owner:
            return None
        if attempt is not None and r["attempt"] != attempt:
            return None
        return r

    def insert(self, task_id, room_id, requested_worker, now):
        if task_id in self.rows:
            return 0
        self.rows[task_id] = dict(
            task_id=task_id, room_id=room_id, requested_worker=requested_worker,
            assigned_worker=None, state="PENDING", lease_owner=None,
            lease_until=None, attempt=0, created_at=now, accepted_at=None,
            finished_at=None)
        return 1

    def _apply(self, r, **fields):
        if r is None:
            return 0
        r.update(**fields)
        return 1

    def offer(self, task_id, worker, owner, until):
        return self._apply(self._match(task_id, ("PENDING",)), state="OFFERED",
                           assigned_worker=worker, lease_owner=owner, lease_until=until)

    def accept(self, task_id, attempt, owner, until, now):
        r = self._match(task_id, ("PENDING", "OFFERED"), attempt=attempt)
        return self._apply(r, state="ACCEPTED", lease_owner=owner,
                           lease_until=until, accepted_at=now)

    def start(self, task_id, owner, until):
        r = self._match(task_id, ("ACCEPTED",), owner=owner)
        return self._apply(r, state="RUNNING", lease_until=until)

    def renew(self, task_id, owner, until):
        r = self._match(task_id, ("ACCEPTED", "RUNNING"), owner=owner)
        return self._apply(r, lease_until=until)

    def finish(self, task_id, owner, terminal, now):
        r = self._match(task_id, ("RUNNING",), owner=owner)
        return self._apply(r, state=terminal, finished_at=now,
                           lease_owner=None, lease_until=None)

    def expire(self, task_id, now):
        r = self._match(task_id, ("OFFERED", "ACCEPTED", "RUNNING"))
        if r is None or r["lease_until"] is None or r["lease_until"] >= now:
            return 0
        return self._apply(r, state="PENDING", assigned_worker=None, lease_owner=None,
                           lease_until=None, attempt=r["attempt"] + 1)


class Supervisor:
    """The only scheduler: routes once per task, leases in the same pass, and
    reconciles expired leases. It holds no state a restart could lose."""

    def __init__(self, store, clock):
        self.store, self.clock = store, clock
        self.bindings = {}          # room_id -> [worker names]
        self.health = {"core": "HEALTHY"}
        self.results = set()        # canonical task IDs whose result is on disk
        self.run_on_core = set()    # rooms the owner explicitly released
        self.delivered = []

    def target_for(self, row):
        rw = row["requested_worker"]
        if rw:
            return rw if self.health.get(rw) == "HEALTHY" else None
        bound = self.bindings.get(row["room_id"])
        if not bound:
            return "core"
        healthy = sorted(w for w in bound if self.health.get(w) == "HEALTHY")
        if healthy:
            return healthy[0]
        return "core" if row["room_id"] in self.run_on_core else None

    def route(self, task_id, deliver=True):
        row = self.store.rows[task_id]
        target = self.target_for(row)
        if target is None:
            return None
        if self.store.offer(task_id, target, target, self.clock.now + LEASE_S) == 0:
            return None
        if deliver:
            self.delivered.append((task_id, row["attempt"], target))
        return target

    def reconcile_leases(self, deliver=True):
        """Order is normative: expire, then settle from disk, then re-route.
        Settling before re-routing is what stops a finished task being re-offered."""
        for tid in list(self.store.rows):
            self.store.expire(tid, self.clock.now)
        for tid, row in self.store.rows.items():
            if tid in self.results and row["state"] not in TERMINAL:
                row.update(state="SUCCEEDED", finished_at=self.clock.now,
                           lease_owner=None, lease_until=None)
        for tid, row in list(self.store.rows.items()):
            if row["state"] == "PENDING":
                self.route(tid, deliver=deliver)

    def restart(self):
        self.delivered = []
        self.reconcile_leases()


def seed(sup, task_id="task-1", room="room-A", requested=None):
    sup.store.insert(task_id, room, requested, sup.clock.now)
    return task_id


class GuardedTransitions(unittest.TestCase):
    def setUp(self):
        self.clock, self.store = Clock(), Store()
        self.sup = Supervisor(self.store, self.clock)

    def test_happy_path_reaches_a_terminal_state(self):
        t = seed(self.sup)
        self.sup.route(t)
        until = self.clock.now + LEASE_S
        self.assertEqual(self.store.rows[t]["state"], "OFFERED")
        self.assertEqual(self.store.accept(t, 0, "core", until, self.clock.now), 1)
        self.assertEqual(self.store.start(t, "core", until), 1)
        self.assertEqual(self.store.finish(t, "core", "SUCCEEDED", self.clock.now), 1)
        self.assertIn(self.store.rows[t]["state"], TERMINAL)
        self.assertIsNone(self.store.rows[t]["lease_until"])

    def test_a_second_offer_matches_zero_rows(self):
        t = seed(self.sup)
        self.sup.route(t)
        self.assertEqual(self.store.offer(t, "worker-2", "worker-2", self.clock.now), 0)

    def test_a_report_from_another_executor_matches_zero_rows(self):
        t = seed(self.sup)
        self.sup.route(t)
        self.store.accept(t, 0, "core", self.clock.now + LEASE_S, self.clock.now)
        self.assertEqual(self.store.start(t, "worker-2", self.clock.now + LEASE_S), 0)

    def test_renewal_keeps_a_long_task_out_of_expiry(self):
        t = seed(self.sup)
        self.sup.route(t)
        self.store.accept(t, 0, "core", self.clock.now + LEASE_S, self.clock.now)
        self.store.start(t, "core", self.clock.now + LEASE_S)
        for _ in range(5):
            self.clock.advance(LEASE_S // 2)
            self.assertEqual(self.store.renew(t, "core", self.clock.now + LEASE_S), 1)
            self.assertEqual(self.store.expire(t, self.clock.now), 0)
        self.assertEqual(self.store.rows[t]["state"], "RUNNING")


class CrashWindows(unittest.TestCase):
    """The four seams of the failure matrix. Each is a recognisable row state,
    and the recovery is a transition rather than an inference from a filesystem."""

    def setUp(self):
        self.clock, self.store = Clock(), Store()
        self.sup = Supervisor(self.store, self.clock)

    def test_offer_before_delivery(self):
        t = seed(self.sup)
        self.sup.route(t, deliver=False)
        self.clock.advance(LEASE_S + 1)
        self.sup.restart()
        self.assertEqual(self.store.rows[t]["attempt"], 1)
        self.assertEqual(self.store.rows[t]["state"], "OFFERED")
        self.assertEqual(len(self.sup.delivered), 1)

    def test_delivery_before_accept_refuses_the_stale_accept(self):
        t = seed(self.sup)
        self.sup.route(t)
        self.clock.advance(LEASE_S + 1)
        self.sup.restart()
        self.assertEqual(
            self.store.accept(t, 0, "core", self.clock.now + LEASE_S, self.clock.now), 0,
            "an accept under an expired attempt must match zero rows")
        self.assertEqual(self.store.rows[t]["attempt"], 1)

    def test_accept_before_completion_refuses_the_dead_executors_finish(self):
        t = seed(self.sup, requested="worker-2")
        self.sup.health["worker-2"] = "HEALTHY"
        self.sup.route(t)
        self.store.accept(t, 0, "worker-2", self.clock.now + LEASE_S, self.clock.now)
        self.store.start(t, "worker-2", self.clock.now + LEASE_S)
        self.clock.advance(LEASE_S + 1)
        self.sup.restart()
        self.assertEqual(self.store.rows[t]["attempt"], 1)
        self.assertEqual(self.store.finish(t, "worker-2", "SUCCEEDED", self.clock.now), 0)

    def test_completion_before_release_settles_from_the_result_on_disk(self):
        t = seed(self.sup)
        self.sup.route(t)
        self.store.accept(t, 0, "core", self.clock.now + LEASE_S, self.clock.now)
        self.store.start(t, "core", self.clock.now + LEASE_S)
        self.sup.results.add(t)
        self.clock.advance(LEASE_S + 1)
        self.sup.restart()
        self.assertEqual(self.store.rows[t]["state"], "SUCCEEDED")
        self.assertEqual(self.sup.delivered, [], "a finished task must not be re-offered")


class BoundButUnavailableStaysPending(unittest.TestCase):
    """A bound room with no healthy worker waits. Nothing else may take the work,
    and only an explicit owner choice changes that."""

    def setUp(self):
        self.clock, self.store = Clock(), Store()
        self.sup = Supervisor(self.store, self.clock)
        self.sup.bindings["room-A"] = ["worker-2"]
        self.sup.health["worker-2"] = "UNAVAILABLE"
        self.t = seed(self.sup)

    def test_it_never_silently_runs_on_the_core(self):
        for _ in range(20):
            self.clock.advance(LEASE_S)
            self.sup.reconcile_leases()
            self.assertEqual(self.store.rows[self.t]["state"], "PENDING")
            self.assertIsNone(self.store.rows[self.t]["assigned_worker"])
        self.assertEqual(self.sup.delivered, [])

    def test_process_with_core_is_the_explicit_release(self):
        self.sup.run_on_core.add("room-A")
        self.sup.reconcile_leases()
        self.assertEqual(self.store.rows[self.t]["assigned_worker"], "core")

    def test_rebind_routes_to_the_new_worker(self):
        self.sup.bindings["room-A"] = ["worker-3"]
        self.sup.health["worker-3"] = "HEALTHY"
        self.sup.reconcile_leases()
        self.assertEqual(self.store.rows[self.t]["assigned_worker"], "worker-3")

    def test_restart_probing_admits_only_after_healthy(self):
        for state in ("WEDGED", "PROBING"):
            self.sup.health["worker-2"] = state
            self.sup.reconcile_leases()
            self.assertIsNone(self.store.rows[self.t]["assigned_worker"],
                              "%s is not eligible for ordinary work" % state)
        self.clock.advance(PROBE_TIMEOUT_S)
        self.sup.health["worker-2"] = "HEALTHY"
        self.sup.reconcile_leases()
        self.assertEqual(self.store.rows[self.t]["assigned_worker"], "worker-2")


class EveryScheduleConverges(unittest.TestCase):
    """The invariant the backstop enforces: after a pass at a time past every
    lease, no row sits in a non-PENDING non-terminal state."""

    STEPS = ("accept", "start", "finish", "crash")

    def _run(self, order, health="HEALTHY"):
        clock, store = Clock(), Store()
        sup = Supervisor(store, clock)
        sup.bindings["room-A"] = ["worker-2"]
        sup.health["worker-2"] = health
        t = seed(sup)
        sup.reconcile_leases()
        for step in order:
            if step == "crash":
                break
            until = clock.now + LEASE_S
            if step == "accept":
                store.accept(t, store.rows[t]["attempt"], "worker-2", until, clock.now)
            elif step == "start":
                store.start(t, "worker-2", until)
            elif step == "finish":
                store.finish(t, "worker-2", "SUCCEEDED", clock.now)
            clock.advance(1)
        clock.advance(LEASE_S + 1)
        sup.restart()
        return store.rows[t], clock

    def test_no_schedule_leaves_a_stuck_row(self):
        for order in itertools.permutations(self.STEPS):
            with self.subTest(order=order):
                row, clock = self._run(order)
                self.assertIn(row["state"], TERMINAL + ("PENDING", "OFFERED"))
                if row["state"] in NON_TERMINAL and row["lease_until"] is not None:
                    self.assertGreaterEqual(row["lease_until"], clock.now,
                                            "an expired lease survived a pass")

    def test_an_unavailable_binding_never_leaks_to_the_core(self):
        for order in itertools.permutations(self.STEPS):
            with self.subTest(order=order):
                row, _ = self._run(order, health="UNAVAILABLE")
                self.assertNotEqual(row["assigned_worker"], "core")

    def test_the_convergence_check_can_fail(self):
        """Control: a store whose expiry is disabled must violate the invariant."""
        clock, store = Clock(), Store()
        sup = Supervisor(store, clock)
        t = seed(sup)
        sup.route(t)
        store.expire = lambda *a, **k: 0
        clock.advance(LEASE_S + 1)
        sup.restart()
        self.assertEqual(store.rows[t]["state"], "OFFERED")
        self.assertLess(store.rows[t]["lease_until"], clock.now,
                        "without expiry the row is stuck past its lease")


if __name__ == "__main__":
    unittest.main(verbosity=1)
