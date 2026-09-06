"""A no-write transition model over the design's own wedge/reset rules.

The reviewer's control showed that a reset implemented as DELETION is ordering-dependent
and, in one ordering, admits 2*runners rather than one. This models BOTH the old
(delete) and new (durable probation, atomic admit) rules so the old one FAILS the
pins and the new one passes -- a model that only ever passed would pin nothing.
"""
import unittest

RUNNERS = 2


def run(order, reset, pending=5, runners=RUNNERS):
    """Return (verdict, claimed, pending) after the named ordering.

    reset='delete'    : kick deletes the record entry (the old design)
    reset='probation' : kick requests; sweep publishes probation(admit=1); gate consumes
    """
    rec = "wedged"          # published verdict for the instance
    admit = 0
    claimed = 0
    probation_requested = False

    def sweep():
        nonlocal rec, admit, probation_requested
        if reset == "delete":
            # inputs unchanged (oldest task unclaimed, no claim held) -> republish
            if claimed == 0:
                rec = "wedged"
            else:
                rec = "eligible"
        else:
            if probation_requested and rec != "probation":
                rec, admit, probation_requested = "probation", 1, False
            elif rec == "probation":
                pass                       # HELD: not recomputed while probation stands
            elif claimed and pending_left() == 0:
                rec = "eligible"

    def pending_left():
        return pending - claimed

    def worker():
        nonlocal claimed, admit
        if rec == "wedged":
            return                         # self-gate suppresses
        if reset == "delete":
            # record absent/eligible: reconciliation admits up to 2*runners
            cap = 2 * runners
            claimed += min(cap, pending_left())
        else:
            # gate: claim ONLY IF admit>0, decrement-and-claim atomically
            if rec == "probation" and admit > 0:
                admit -= 1
                claimed += 1
            elif rec == "eligible":
                claimed += min(2 * runners, pending_left())

    def kick():
        nonlocal rec, probation_requested
        if reset == "delete":
            rec = "absent"                 # deletion == absent == eligible per rule 1
        else:
            probation_requested = True     # writes NOTHING to the record

    def event():
        # an arriving event hits the same self-gate as reconciliation
        worker()

    steps = {"kick": kick, "sweep": sweep, "worker": worker, "event": event}
    for s in order:
        steps[s]()
    return rec, claimed, pending_left()


class TheOldDeleteResetFailsTheReviewersControl(unittest.TestCase):
    """The reviewer's two measured outcomes, reproduced -- the CONTROL."""

    def test_kick_sweep_worker_leaves_the_room_dark(self):
        self.assertEqual(run(["kick", "sweep", "worker"], "delete"), ("wedged", 0, 5))

    def test_kick_worker_sweep_admits_four_not_one(self):
        self.assertEqual(run(["kick", "worker", "sweep"], "delete"), ("eligible", 4, 1))


class TheProbationResetHoldsUnderAllFourOrderings(unittest.TestCase):
    def test_kick_sweep_worker_admits_exactly_one(self):
        self.assertEqual(run(["kick", "sweep", "worker"], "probation"), ("probation", 1, 4))

    def test_kick_worker_sweep_admits_exactly_one(self):
        # worker before sweep: no probation published yet, gate still sees wedged -> 0;
        # then sweep publishes probation; a following worker admits 1.
        self.assertEqual(run(["kick", "worker", "sweep", "worker"], "probation"), ("probation", 1, 4))

    def test_multi_task_backlog_decrements_once(self):
        self.assertEqual(run(["kick", "sweep", "worker", "worker", "worker"], "probation"), ("probation", 1, 4))

    def test_event_during_probation_cannot_admit_a_second(self):
        self.assertEqual(run(["kick", "sweep", "worker", "event"], "probation"), ("probation", 1, 4))

    def test_an_intervening_sweep_does_not_republish_wedged(self):
        self.assertEqual(run(["kick", "sweep", "sweep", "sweep", "worker"], "probation"), ("probation", 1, 4))


if __name__ == "__main__":
    unittest.main()
