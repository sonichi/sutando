#!/usr/bin/env python3
"""Model of the supervisor-owned task journal: one atomically replaced state
record per task, a generation check on every executor event, a durable receipt
inbox replayed on restart, and an advisory single-instance lock.
"""
import itertools
import unittest

TERMINAL = ("SUCCEEDED", "FAILED")
NON_TERMINAL = ("PENDING", "OFFERED", "ACCEPTED", "RUNNING")
EVENT_STATES = {"accept": ("OFFERED", "ACCEPTED"), "start": ("ACCEPTED", "RUNNING"),
                "complete": ("RUNNING", "SUCCEEDED"), "fail": ("RUNNING", "FAILED")}
FIELDS = ("version", "task_id", "state", "room_id", "requested_worker",
          "executor_id", "assignment_id", "lease_generation", "lease_until",
          "offer_expires_at", "updated_at")
LEASE_S = 60
OFFER_S = 60
PROBE_TIMEOUT_S = 30


class Journal:
    """`task-state/<task-id>/state.json`, replaced whole: a write stages a temp
    record and swaps it in, so a crash before the swap leaves the old record."""

    def __init__(self):
        self.records, self.staged = {}, None

    def read(self, task_id):
        rec = self.records.get(task_id)
        return dict(rec) if rec is not None else None

    def create(self, task_id, room_id, requested_worker, now):
        if task_id in self.records:
            return 0
        self.records[task_id] = dict(
            version=1, task_id=task_id, state="PENDING", room_id=room_id,
            requested_worker=requested_worker, executor_id=None, assignment_id=None,
            lease_generation=0, lease_until=None, offer_expires_at=None,
            updated_at=now)
        return 1

    def stage(self, task_id, now, **fields):
        """The temp file: written and fsynced, not yet os.replace()d."""
        old = self.records[task_id]
        self.staged = (task_id, dict(old, version=old["version"] + 1,
                                     updated_at=now, **fields))
        return self.staged[1]

    def commit(self):
        if self.staged is None:
            return 0
        self.records[self.staged[0]], self.staged = self.staged[1], None
        return 1

    def replace(self, task_id, now, **fields):
        self.stage(task_id, now, **fields)
        return self.commit()


class SupervisorLock:
    """`run/pool-supervisor.lock`, held exclusively for the process's lifetime."""

    def __init__(self):
        self.holder = None

    def acquire(self, who):
        if self.holder is not None:
            return False
        self.holder = who
        return True


class Supervisor:
    """The only writer of the journal. Executors report events; the supervisor
    runs the generation check and replaces the record."""

    def __init__(self, journal, lock=None, name="supervisor-1", now=1000):
        self.journal, self.name, self.now = journal, name, now
        self.started = (lock or SupervisorLock()).acquire(name)
        self.bindings, self.health = {}, {"core": "HEALTHY"}
        self.receipts, self.stale = {}, []
        self.run_on_core, self.delivered, self.assignments = set(), [], 0
        self.outbox, self.results_delivered = {}, []

    def advance(self, secs):
        self.now += secs
        return self.now

    def target_for(self, rec):
        rw = rec["requested_worker"]
        if rw:
            return rw if self.health.get(rw) == "HEALTHY" else None
        bound = self.bindings.get(rec["room_id"])
        if not bound:
            return "core"
        healthy = sorted(w for w in bound if self.health.get(w) == "HEALTHY")
        if healthy:
            return healthy[0]
        return "core" if rec["room_id"] in self.run_on_core else None

    def offer(self, task_id, deliver=True):
        rec = self.journal.read(task_id)
        if rec is None or rec["state"] != "PENDING":
            return None
        target = self.target_for(rec)
        if target is None:
            return None
        self.assignments += 1
        assignment = "assign-%d" % self.assignments
        self.journal.replace(
            task_id, self.now, state="OFFERED", executor_id=target,
            assignment_id=assignment, offer_expires_at=self.now + OFFER_S,
            lease_until=None, lease_generation=rec["lease_generation"] + 1)
        if deliver:
            self.delivered.append((task_id, assignment, target))
        return target

    def _checked(self, ev):
        """Assignment, generation and executor must all be the record's current
        values, or the event did not happen."""
        rec = self.journal.read(ev["task_id"])
        if rec is None or any(rec[k] != ev[k] for k in
                              ("executor_id", "assignment_id", "lease_generation")):
            return None
        return rec

    def apply_event(self, ev):
        rec = self._checked(ev)
        if rec is None:
            if ev["type"] == "complete":
                self.stale.append((ev["task_id"], ev["lease_generation"],
                                   ev["executor_id"]))
            return False
        frm, to = EVENT_STATES[ev["type"]]
        if rec["state"] != frm:
            return False
        fields = dict(state=to, lease_until=self.now + LEASE_S)
        if to == "ACCEPTED":
            fields["offer_expires_at"] = None
        if to in TERMINAL:
            # Delivery intent is durable BEFORE the lease identity is cleared, so a
            # crash in the seam re-drives from the outbox, never dropping a reply.
            if to == "SUCCEEDED":
                self.outbox[ev["task_id"]] = dict(
                    generation=rec["lease_generation"], delivered=False)
            fields.update(executor_id=None, assignment_id=None,
                          lease_until=None, offer_expires_at=None)
        self.journal.replace(ev["task_id"], self.now, **fields)
        return True

    def renew(self, ev):
        rec = self._checked(ev)
        if rec is None or rec["state"] not in ("ACCEPTED", "RUNNING"):
            return False
        return bool(self.journal.replace(ev["task_id"], self.now,
                                         lease_until=self.now + LEASE_S))

    def expire(self, task_id):
        rec = self.journal.read(task_id)
        if rec is None or rec["state"] in TERMINAL + ("PENDING",):
            return 0
        # State-specific deadline: an OFFERED record carries offer_expires_at and no
        # lease_until, so keying on lease_until alone strands an unaccepted offer.
        deadline = (rec["offer_expires_at"] if rec["state"] == "OFFERED"
                    else rec["lease_until"])
        if deadline is None or deadline >= self.now:
            return 0
        return self.journal.replace(
            task_id, self.now, state="PENDING", executor_id=None,
            assignment_id=None, lease_until=None, offer_expires_at=None,
            lease_generation=rec["lease_generation"] + 1)

    def consume_receipts(self):
        """A receipt is an inbox message, deleted once consumed — applied when it
        passes the check, archived as stale when it does not."""
        for executor in list(self.receipts):
            inbox, self.receipts[executor] = self.receipts[executor], []
            for ev in inbox:
                self.apply_event(ev)

    def drive_outbox(self):
        """Re-drive any delivery intent without a delivered sentinel, keyed on the
        task id, so a cleared lease identity never strands a completed reply."""
        for task_id, entry in self.outbox.items():
            if not entry["delivered"]:
                entry["delivered"] = True
                self.results_delivered.append(task_id)

    def reconcile_leases(self, deliver=True):
        """Order is normative: consume receipts, then expire, then re-route.
        Consuming first is what stops a finished task being re-offered."""
        self.consume_receipts()
        self.drive_outbox()
        for task_id in list(self.journal.records):
            self.expire(task_id)
        for task_id, rec in list(self.journal.records.items()):
            if rec["state"] == "PENDING":
                self.offer(task_id, deliver=deliver)

    def restart(self):
        self.delivered = []
        self.reconcile_leases()


def report(sup, who, kind, task_id):
    """The event an executor puts on the socket: it echoes the assignment and
    the generation it was offered under, and writes nothing itself."""
    rec = sup.journal.read(task_id)
    return dict(type=kind, task_id=task_id, executor_id=who,
                assignment_id=rec["assignment_id"],
                lease_generation=rec["lease_generation"])


def complete(sup, who, task_id, socket_up=True):
    """Result first, then the durable receipt, then the socket attempt."""
    ev = report(sup, who, "complete", task_id)
    sup.receipts.setdefault(who, []).append(ev)
    if socket_up:
        sup.consume_receipts()
        sup.drive_outbox()
    return ev


def fresh(**health):
    journal = Journal()
    sup = Supervisor(journal)
    sup.health.update(health)
    return journal, sup


def seed(sup, task_id="task-1", room="room-A", requested=None):
    sup.journal.create(task_id, room, requested, sup.now)
    return task_id




class GuardedTransitions(unittest.TestCase):
    def setUp(self):
        self.journal, self.sup = fresh()
        self.t = seed(self.sup)

    def test_happy_path_reaches_a_terminal_state(self):
        self.sup.offer(self.t)
        self.assertEqual(self.journal.read(self.t)["state"], "OFFERED")
        for kind in ("accept", "start", "complete"):
            ev = report(self.sup, "core", kind, self.t)
            self.assertTrue(self.sup.apply_event(ev), kind)
        rec = self.journal.read(self.t)
        self.assertIn(rec["state"], TERMINAL)
        self.assertIsNone(rec["lease_until"])
        self.assertEqual(rec["version"], 5, "one version per replacement")

    def test_a_second_offer_changes_nothing(self):
        self.sup.offer(self.t)
        before = self.journal.read(self.t)
        self.assertIsNone(self.sup.offer(self.t))
        self.assertEqual(self.journal.read(self.t), before)

    def test_only_the_current_assignment_holder_moves_the_record(self):
        self.sup.offer(self.t)
        self.sup.apply_event(report(self.sup, "core", "accept", self.t))
        version = self.journal.read(self.t)["version"]
        wrong_gen = report(self.sup, "core", "start", self.t)
        wrong_gen["lease_generation"] += 7
        for ev in (report(self.sup, "worker-2", "start", self.t), wrong_gen):
            self.assertFalse(self.sup.apply_event(ev))
        self.assertEqual(self.journal.read(self.t)["version"], version)

    def test_renewal_keeps_a_long_task_out_of_expiry(self):
        self.sup.offer(self.t)
        self.sup.apply_event(report(self.sup, "core", "accept", self.t))
        self.sup.apply_event(report(self.sup, "core", "start", self.t))
        for _ in range(5):
            self.sup.advance(LEASE_S // 2)
            self.assertTrue(self.sup.renew(report(self.sup, "core", "start", self.t)))
            self.assertEqual(self.sup.expire(self.t), 0)
        self.assertEqual(self.journal.read(self.t)["state"], "RUNNING")


class AtomicReplacement(unittest.TestCase):
    def test_a_crash_between_the_temp_and_the_replace_leaves_the_old_record(self):
        journal, sup = fresh()
        t = seed(sup)
        sup.offer(t)
        before = journal.read(t)
        journal.stage(t, sup.now, state="ACCEPTED")
        self.assertEqual(journal.read(t), before, "the swap has not happened yet")
        journal.staged = None
        self.assertEqual(journal.read(t), before)
        self.assertEqual(sorted(journal.read(t)), sorted(FIELDS),
                         "a reader sees a complete record, never a torn one")
        journal.stage(t, sup.now, state="ACCEPTED")
        journal.commit()
        self.assertEqual(journal.read(t)["state"], "ACCEPTED")
        self.assertEqual(sorted(journal.read(t)), sorted(FIELDS))


class StaleGenerationIsRefused(unittest.TestCase):
    def test_a_late_completion_never_overwrites_the_current_generation(self):
        journal, sup = fresh(**{"worker-2": "HEALTHY", "worker-3": "HEALTHY"})
        t = seed(sup, requested="worker-2")
        sup.offer(t)
        sup.apply_event(report(sup, "worker-2", "accept", t))
        sup.apply_event(report(sup, "worker-2", "start", t))
        late = report(sup, "worker-2", "complete", t)
        sup.advance(LEASE_S + 1)
        journal.replace(t, sup.now, requested_worker="worker-3")
        sup.reconcile_leases()
        current = journal.read(t)
        self.assertEqual(current["executor_id"], "worker-3")
        self.assertFalse(sup.apply_event(late))
        self.assertEqual(journal.read(t), current, "the record is untouched")
        self.assertEqual(sup.stale, [(t, late["lease_generation"], "worker-2")])


class ReceiptInboxIsReplayedOnRestart(unittest.TestCase):
    def setUp(self):
        self.journal, self.sup = fresh()
        self.t = seed(self.sup)
        self.sup.offer(self.t)
        self.sup.apply_event(report(self.sup, "core", "accept", self.t))
        self.sup.apply_event(report(self.sup, "core", "start", self.t))

    def test_an_unreachable_socket_leaves_the_receipt_for_the_restart(self):
        complete(self.sup, "core", self.t, socket_up=False)
        self.assertEqual(self.journal.read(self.t)["state"], "RUNNING")
        self.assertEqual(len(self.sup.receipts["core"]), 1)
        self.sup.advance(LEASE_S + 1)
        self.sup.restart()
        self.assertEqual(self.journal.read(self.t)["state"], "SUCCEEDED")
        self.assertEqual(self.sup.receipts["core"], [],
                         "a consumed receipt must be deleted from the inbox")
        self.assertEqual(self.sup.delivered, [])

    def test_a_reachable_socket_needs_no_replay(self):
        complete(self.sup, "core", self.t, socket_up=True)
        self.assertEqual(self.journal.read(self.t)["state"], "SUCCEEDED")
        self.assertEqual(self.sup.receipts["core"], [])


class SingleInstance(unittest.TestCase):
    def test_a_second_supervisor_fails_to_start(self):
        journal, lock = Journal(), SupervisorLock()
        first = Supervisor(journal, lock=lock, name="supervisor-1")
        second = Supervisor(journal, lock=lock, name="supervisor-2")
        self.assertTrue(first.started)
        self.assertFalse(second.started, "the advisory lock is exclusive")


class CrashWindows(unittest.TestCase):
    """The four seams of the failure matrix. Each is a recognisable record state
    plus a receipt outcome, and the recovery is a transition."""

    def setUp(self):
        self.journal, self.sup = fresh(**{"worker-2": "HEALTHY"})

    def test_offer_before_delivery(self):
        t = seed(self.sup)
        self.sup.offer(t, deliver=False)
        offered = self.journal.read(t)["lease_generation"]
        self.sup.advance(LEASE_S + 1)
        self.sup.restart()
        rec = self.journal.read(t)
        self.assertEqual(rec["state"], "OFFERED")
        self.assertGreater(rec["lease_generation"], offered)
        self.assertEqual(len(self.sup.delivered), 1, "nothing accepted it")

    def test_delivery_before_accept_refuses_the_stale_accept(self):
        t = seed(self.sup)
        self.sup.offer(t)
        late = report(self.sup, "core", "accept", t)
        self.sup.advance(LEASE_S + 1)
        self.sup.restart()
        self.assertFalse(self.sup.apply_event(late),
                         "an accept under an expired generation must be refused")

    def test_accept_before_completion_refuses_the_dead_executors_finish(self):
        t = seed(self.sup, requested="worker-2")
        self.sup.offer(t)
        self.sup.apply_event(report(self.sup, "worker-2", "accept", t))
        self.sup.apply_event(report(self.sup, "worker-2", "start", t))
        late = report(self.sup, "worker-2", "complete", t)
        self.sup.advance(LEASE_S + 1)
        self.sup.restart()
        self.assertFalse(self.sup.apply_event(late))
        self.assertNotIn(self.journal.read(t)["state"], TERMINAL)

    def test_completion_before_release_settles_from_the_receipt(self):
        t = seed(self.sup)
        self.sup.offer(t)
        self.sup.apply_event(report(self.sup, "core", "accept", t))
        self.sup.apply_event(report(self.sup, "core", "start", t))
        complete(self.sup, "core", t, socket_up=False)
        self.sup.advance(LEASE_S + 1)
        self.sup.restart()
        self.assertEqual(self.journal.read(t)["state"], "SUCCEEDED")
        self.assertEqual(self.sup.delivered, [],
                         "a finished task must not be re-offered")

    def test_an_unaccepted_offer_expires_on_offer_expires_at(self):
        t = seed(self.sup)
        self.sup.offer(t, deliver=False)
        rec = self.journal.read(t)
        self.assertIsNotNone(rec["offer_expires_at"])
        self.assertIsNone(rec["lease_until"], "an OFFERED record holds no lease")
        self.assertEqual(self.sup.expire(t), 0, "not yet past offer_expires_at")
        self.sup.advance(OFFER_S + 1)
        self.assertEqual(self.sup.expire(t), 1, "expires on offer_expires_at")
        self.assertEqual(self.journal.read(t)["state"], "PENDING")

    def test_a_completed_reply_survives_the_identity_clear(self):
        t = seed(self.sup, requested="worker-2")
        self.sup.offer(t)
        self.sup.apply_event(report(self.sup, "worker-2", "accept", t))
        self.sup.apply_event(report(self.sup, "worker-2", "start", t))
        complete(self.sup, "worker-2", t)
        self.assertIn(t, self.sup.results_delivered)
        self.assertIsNone(self.journal.read(t)["executor_id"])
        stale = dict(type="complete", task_id=t, executor_id="worker-2",
                     assignment_id="assign-1", lease_generation=99)
        self.sup.receipts.setdefault("worker-2", []).append(stale)
        self.sup.restart()
        self.assertIn(t, self.sup.results_delivered,
                      "delivery intent is durable independent of the lease")


class BoundButUnavailableStaysPending(unittest.TestCase):
    """A bound room with no healthy worker waits. Nothing else may take the work,
    and only an explicit owner choice changes that."""

    def setUp(self):
        self.journal, self.sup = fresh(**{"worker-2": "UNAVAILABLE"})
        self.sup.bindings["room-A"] = ["worker-2"]
        self.t = seed(self.sup)

    def test_it_never_silently_runs_on_the_core(self):
        for _ in range(20):
            self.sup.advance(LEASE_S)
            self.sup.reconcile_leases()
            rec = self.journal.read(self.t)
            self.assertEqual(rec["state"], "PENDING")
            self.assertIsNone(rec["executor_id"])
        self.assertEqual(self.sup.delivered, [])

    def test_process_with_core_is_the_explicit_release(self):
        self.sup.run_on_core.add("room-A")
        self.sup.reconcile_leases()
        self.assertEqual(self.journal.read(self.t)["executor_id"], "core")

    def test_rebind_routes_to_the_new_worker(self):
        self.sup.bindings["room-A"] = ["worker-3"]
        self.sup.health["worker-3"] = "HEALTHY"
        self.sup.reconcile_leases()
        self.assertEqual(self.journal.read(self.t)["executor_id"], "worker-3")

    def test_restart_probing_admits_only_after_healthy(self):
        for state in ("WEDGED", "PROBING"):
            self.sup.health["worker-2"] = state
            self.sup.reconcile_leases()
            self.assertIsNone(self.journal.read(self.t)["executor_id"],
                              "%s is not eligible for ordinary work" % state)
        self.sup.advance(PROBE_TIMEOUT_S)
        self.sup.health["worker-2"] = "HEALTHY"
        self.sup.reconcile_leases()
        self.assertEqual(self.journal.read(self.t)["executor_id"], "worker-2")


class EveryScheduleConverges(unittest.TestCase):
    """The invariant the backstop enforces: after a pass at a time past every
    lease, no record sits in a non-PENDING non-terminal state."""

    STEPS = ("accept", "start", "complete", "crash")

    def _run(self, order, health="HEALTHY"):
        journal, sup = fresh(**{"worker-2": health})
        sup.bindings["room-A"] = ["worker-2"]
        t = seed(sup)
        sup.reconcile_leases()
        for step in order:
            if step == "crash":
                break
            if journal.read(t)["assignment_id"] is not None:
                sup.apply_event(report(sup, "worker-2", step, t))
            sup.advance(1)
        sup.advance(LEASE_S + 1)
        sup.restart()
        return journal.read(t), sup

    def test_no_schedule_leaves_a_stuck_record(self):
        for order in itertools.permutations(self.STEPS):
            with self.subTest(order=order):
                rec, sup = self._run(order)
                self.assertIn(rec["state"], TERMINAL + ("PENDING", "OFFERED"))
                if rec["state"] in NON_TERMINAL and rec["state"] != "PENDING":
                    deadline = (rec["offer_expires_at"] if rec["state"] == "OFFERED"
                                else rec["lease_until"])
                    if deadline is not None:
                        self.assertGreaterEqual(deadline, sup.now,
                                                "an expired deadline survived a pass")

    def test_an_unavailable_binding_never_leaks_to_the_core(self):
        for order in itertools.permutations(self.STEPS):
            with self.subTest(order=order):
                rec, _ = self._run(order, health="UNAVAILABLE")
                self.assertNotEqual(rec["executor_id"], "core")

    def test_the_convergence_check_can_fail(self):
        """Control: a supervisor whose expiry is disabled violates the invariant."""
        journal, sup = fresh()
        t = seed(sup)
        sup.offer(t)
        sup.expire = lambda *a, **k: 0
        sup.advance(LEASE_S + 1)
        sup.restart()
        rec = journal.read(t)
        self.assertEqual(rec["state"], "OFFERED")
        self.assertLess(rec["offer_expires_at"], sup.now,
                        "without expiry the record is stuck past its offer deadline")


if __name__ == "__main__":
    unittest.main(verbosity=1)
