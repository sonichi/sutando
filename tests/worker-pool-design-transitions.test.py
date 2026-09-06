"""Transition model with ACTOR-OWNED durable state and a SEPARABLE crash seam.

Earlier versions collapsed token consumption and task claim into one Python statement, so they
could not see the crash between them (qingyun-wu, 33c16809). Here the two are distinct durable
steps on a modeled disk; `crash` may land between them; restart re-reads disk. The writer set of
the record is asserted on every run.
"""
import unittest

RUNNERS = 2


class Disk:
    def __init__(self):
        # record: sweep-only. token/journal: sweep creates, worker renames. request: kick-pool.
        self.record, self.token, self.journal, self.request = {}, False, None, False
        self.claims, self.writers = set(), set()


def run(order, mode, pending=5, runners=RUNNERS, claim_fails_once=False):
    d = Disk(); d.record["w"] = "wedged"; claimed = 0; fail = [claim_fails_once]

    def sweep():
        d.writers.add(("record", "sweep"))
        if d.request and d.record["w"] != "probation":
            d.record["w"] = "probation"; d.request = False; d.token = True
        elif d.record["w"] == "probation":
            pass                                            # held
        elif claimed and pending - claimed == 0:
            d.record["w"] = "eligible"

    def gate_step1(task):
        if not d.token: return False
        d.token = False; d.journal = task; return True     # rename .admit -> .admit.<task>

    def gate_step2():
        nonlocal claimed
        task = d.journal
        if task is None: return
        if fail[0]:
            fail[0] = False; d.journal = None; d.token = True   # claim failed -> return token
            return
        d.claims.add(task); d.journal = None; claimed += 1  # hard-link claim held

    def worker():
        v = d.record["w"]
        if v == "wedged": return
        if v == "eligible":
            nonlocal claimed; claimed += min(2 * runners, pending - claimed); return
        # the REJECTED design has no reconciliation: a journal with no claim means "token gone" -> suppress
        if mode == "seam" and d.journal is not None:
            return
        # probation: reconcile FIRST — a journal with no held claim is a retry, not a suppress
        if d.journal is not None and d.journal not in d.claims:
            gate_step2(); return
        if gate_step1(f"t{claimed+1}"):
            if mode == "crash_between": return              # crash lands here; step 2 never runs
            gate_step2()

    def kick():
        d.request = True; d.writers.add(("request", "kick-pool"))

    def restart():
        pass                                                # process memory gone; disk persists

    steps = {"kick": kick, "sweep": sweep, "worker": worker, "event": worker, "restart": restart,
             "crash": lambda: None}
    for s in order:
        if s == "crash_worker":
            # perform step 1 then crash before step 2
            v = d.record["w"]
            if v == "probation" and d.journal is None: gate_step1(f"t{claimed+1}")
            continue
        steps[s]()
    return d.record["w"], claimed, pending - claimed, d


def record_writers(d): return {a for art, a in d.writers if art == "record"}


class TheSeamWithoutReconciliationGoesDarkForever(unittest.TestCase):
    """qingyun-wu's control: crash after token rename, before claim -> probation, no token,
    task pending, restarted gate suppresses. This is the REJECTED design."""

    def test_crash_between_rename_and_claim_suppresses_forever(self):
        v, c, p, d = run(["kick", "sweep", "crash_worker", "restart", "worker", "worker"], "seam")
        self.assertEqual((v, c, p), ("probation", 0, 5))
        self.assertFalse(d.token); self.assertIsNotNone(d.journal)   # exactly their durable state


class TheJournalReconcilesTheSeam(unittest.TestCase):
    def test_crash_between_rename_and_claim_is_recovered_on_restart(self):
        v, c, p, d = run(["kick", "sweep", "crash_worker", "restart", "worker"], "token")
        self.assertEqual((v, c, p), ("probation", 1, 4)); self.assertIsNone(d.journal)

    def test_claim_collision_returns_the_token(self):
        v, c, p, d = run(["kick", "sweep", "worker"], "token", claim_fails_once=True)
        self.assertEqual((v, c, p), ("probation", 0, 5)); self.assertTrue(d.token)
        v, c, p, d2 = run(["kick", "sweep", "worker", "worker"], "token", claim_fails_once=True)
        self.assertEqual((v, c, p), ("probation", 1, 4))                # second attempt succeeds

    def test_no_token_no_journal_still_suppresses(self):
        v, c, p, d = run(["kick", "sweep", "worker", "restart", "worker", "event"], "token")
        self.assertEqual((v, c, p), ("probation", 1, 4))

    def test_all_four_orderings_admit_exactly_one(self):
        for order in (["kick", "sweep", "worker"], ["kick", "worker", "sweep", "worker"],
                      ["kick", "sweep", "worker", "worker", "worker"], ["kick", "sweep", "worker", "event"]):
            v, c, p, d = run(order, "token")
            self.assertEqual((v, c, p), ("probation", 1, 4), order)
            self.assertEqual(record_writers(d), {"sweep"}, order)


if __name__ == "__main__":
    unittest.main()
