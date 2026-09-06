"""A transition model over the design's wedge/reset rules, with ACTOR-OWNED durable state.

The first version kept `rec` and `admit` as shared in-process variables: it proved the
count arithmetic and could not see which production actor persists the decrement — so
it passed while the design had the worker mutating the core-only record. This version
models the three durable artifacts separately, each writable by exactly one actor, and
asserts the writer set on every run. Restart is modeled as re-reading disk.
"""
import unittest

RUNNERS = 2


class Disk:
    def __init__(self):
        # record: sweep-only. tokens: sweep creates, worker renames. request: kick-pool.
        self.record, self.tokens, self.request = {}, set(), False
        self.writers = set()


def run(order, reset, pending=5, runners=RUNNERS, restart_after=None):
    d = Disk()
    d.record["w"] = "wedged"
    claimed = 0
    mem = {"admit_seen": None}    # a worker's in-process view; must NOT be load-bearing

    def sweep():
        d.writers.add(("record", "sweep"))
        if reset == "delete":
            d.record["w"] = "wedged" if claimed == 0 else "eligible"
        else:
            if d.request and d.record["w"] != "probation":
                d.record["w"] = "probation"; d.request = False
                d.tokens.add("w"); d.writers.add(("token-create", "sweep"))
            elif d.record["w"] == "probation":
                pass                                   # held
            elif claimed and pending - claimed == 0:
                d.record["w"] = "eligible"

    def worker():
        nonlocal claimed
        v = d.record["w"]                              # re-read from disk every time
        if v == "wedged":
            return
        if reset == "delete":
            claimed += min(2 * runners, pending - claimed)
        elif reset == "counter":
            # the REJECTED design: worker decrements admit inside the record
            a = d.record.get("admit", 0)
            if v == "probation" and a > 0:
                d.record["admit"] = a - 1; d.writers.add(("record", "worker")); claimed += 1
        else:
            if v == "probation" and "w" in d.tokens:
                d.tokens.discard("w"); d.writers.add(("token-consume", "worker")); claimed += 1
            elif v == "eligible":
                claimed += min(2 * runners, pending - claimed)

    def kick():
        if reset == "delete":
            d.record["w"] = "absent"; d.writers.add(("record", "kick-pool"))
        else:
            d.request = True; d.writers.add(("request", "kick-pool"))
            if reset == "counter":
                d.record["admit"] = 1; d.writers.add(("record", "sweep"))  # sweep would publish it

    def restart():
        mem["admit_seen"] = None                       # process memory gone; disk persists

    steps = {"kick": kick, "sweep": sweep, "worker": worker, "event": worker, "restart": restart}
    for s in order:
        steps[s]()
    return d.record["w"], claimed, pending - claimed, d


RECORD_WRITERS_ALLOWED = {"sweep"}


def record_writers(d):
    return {actor for art, actor in d.writers if art == "record"}


class TheOldDeleteResetFailsTheReviewersControl(unittest.TestCase):
    def test_kick_sweep_worker_leaves_the_room_dark(self):
        self.assertEqual(run(["kick", "sweep", "worker"], "delete")[:3], ("wedged", 0, 5))

    def test_kick_worker_sweep_admits_four_not_one(self):
        self.assertEqual(run(["kick", "worker", "sweep"], "delete")[:3], ("eligible", 4, 1))

    def test_delete_makes_kickpool_a_record_writer(self):
        self.assertIn("kick-pool", record_writers(run(["kick"], "delete")[3]))


class TheCounterInTheRecordIsAlsoAWorkerWriter(unittest.TestCase):
    """qingyun-wu's P1 on 2d7b01ad: consuming admit inside pool-status.json makes the worker
    a writer of the core-only record. This is the CONTROL for the writer assertion."""

    def test_counter_design_has_a_worker_record_writer(self):
        d = run(["kick", "sweep", "worker"], "counter")[3]
        self.assertIn("worker", record_writers(d))


class TheTokenResetKeepsTheRecordCoreOnly(unittest.TestCase):
    def assertCoreOnly(self, d):
        self.assertEqual(record_writers(d), RECORD_WRITERS_ALLOWED, f"record writers: {record_writers(d)}")

    def test_kick_sweep_worker_admits_exactly_one(self):
        v, c, p, d = run(["kick", "sweep", "worker"], "token"); self.assertEqual((v, c, p), ("probation", 1, 4)); self.assertCoreOnly(d)

    def test_kick_worker_sweep_worker_admits_exactly_one(self):
        v, c, p, d = run(["kick", "worker", "sweep", "worker"], "token"); self.assertEqual((v, c, p), ("probation", 1, 4)); self.assertCoreOnly(d)

    def test_multi_task_backlog_admits_once(self):
        v, c, p, d = run(["kick", "sweep", "worker", "worker", "worker"], "token"); self.assertEqual((v, c, p), ("probation", 1, 4)); self.assertCoreOnly(d)

    def test_event_during_probation_cannot_admit_a_second(self):
        v, c, p, d = run(["kick", "sweep", "worker", "event"], "token"); self.assertEqual((v, c, p), ("probation", 1, 4)); self.assertCoreOnly(d)

    def test_intervening_sweeps_hold_probation(self):
        v, c, p, d = run(["kick", "sweep", "sweep", "sweep", "worker"], "token"); self.assertEqual((v, c, p), ("probation", 1, 4)); self.assertCoreOnly(d)

    def test_restart_and_reread_cannot_admit_again(self):
        # consumed state is on disk (token renamed), not in process memory
        v, c, p, d = run(["kick", "sweep", "worker", "restart", "worker", "event"], "token")
        self.assertEqual((v, c, p), ("probation", 1, 4)); self.assertCoreOnly(d)

    def test_two_concurrent_workers_share_one_token(self):
        v, c, p, d = run(["kick", "sweep", "worker", "worker"], "token")
        self.assertEqual(c, 1); self.assertCoreOnly(d)


if __name__ == "__main__":
    unittest.main()
