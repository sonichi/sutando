"""Transition model with ACTOR-OWNED durable state, a SEPARABLE crash seam, and a CLOCK.

Earlier versions collapsed token consumption and task claim into one statement (qingyun-wu,
33c16809), then collapsed record publication and token creation into one statement and had no
clock, so neither the issuance crash nor a timeout could be modeled (qingyun-wu, keweichen,
e8d270b1). Here every durable write is its own step on a modeled disk, `crash_*` may land between
any two, restart re-reads disk, and the sweep carries a clock. Every schedule must end in
`eligible` or `wedged`; `probation` is never terminal.
"""
import unittest

RUNNERS = 2
WINDOW = 100          # stand_in_after_s, in model seconds


class Disk:
    def __init__(self):
        # record: sweep-only. token/journal/claimed: sweep creates, worker renames, sweep removes.
        # request: kick-pool. results: the worker finishing an admitted task.
        self.record, self.probation, self.request = {}, {}, False
        self.token, self.journal, self.claimed_rec = False, None, None   # .admit / .admit.<t> / .admit.<t>.claimed
        self.token_at, self.journal_at, self.claimed_at = None, None, None
        self.claims, self.results, self.writers = set(), set(), set()


def run(order, mode="token", pending=5, runners=RUNNERS, claim_fails_once=False):
    d = Disk(); d.record["w"] = "wedged"; claimed = 0; fail = [claim_fails_once]; now = [0]
    crash_once = [mode == "crash_issuance"]

    def verdict():
        # a v1 reader sees only the scalar; a probation-aware reader consults the sibling key
        return "probation" if d.record["w"] == "wedged" and "w" in d.probation else d.record["w"]

    def sweep():
        now[0] += 10; d.writers.add(("record", "sweep"))
        if d.request and "w" not in d.probation:
            if not d.token:                                   # (a) idempotent token
                d.token, d.token_at = True, now[0]
            if crash_once[0]:                                 # crash after (a), before (b): once
                crash_once[0] = False; return
            if mode == "lost_token":                          # the old order: record first, token lost
                d.token = False; d.probation["w"] = {"since": now[0]}; d.record["w"] = "wedged"; d.request = False; return
            d.probation["w"] = {"since": now[0]}; d.record["w"] = "wedged"   # (b)
            d.request = False                                 # (c)
            return
        if d.request and "w" in d.probation:                  # crash after (b), before (c)
            d.request = False; return
        if "w" in d.probation:
            if not d.token and d.journal is None and d.claimed_rec is None:
                d.token, d.token_at = True, now[0]            # issuance crash: re-create the lost token
                return
            if d.claimed_rec is not None and d.claimed_rec in d.results:
                d.probation.pop("w"); d.claimed_rec = None; d.record["w"] = "eligible"; return
            start = d.claimed_at if d.claimed_rec is not None else d.journal_at if d.journal is not None else d.probation["w"]["since"]
            if now[0] - start > WINDOW:
                d.probation.pop("w"); d.token = False; d.journal = None; d.claimed_rec = None
                d.record["w"] = "wedged"; return
            return                                            # held, with a clock running
        if claimed and pending - claimed == 0:
            d.record["w"] = "eligible"

    def gate_step1(task):
        if not d.token: return False
        d.token = False; d.journal, d.journal_at = task, now[0]; return True

    def gate_step2():
        nonlocal claimed
        task = d.journal
        if task is None: return
        if fail[0]:
            fail[0] = False; d.journal = None; d.token = True; return          # claim failed -> return token
        d.claims.add(task); d.claimed_rec, d.claimed_at = task, now[0]; d.journal = None; claimed += 1

    def worker():
        v = verdict()
        if v == "wedged": return
        if v == "eligible":
            nonlocal claimed; claimed += min(2 * runners, pending - claimed); return
        if mode == "seam" and d.journal is not None:          # the REJECTED design: no reconciliation
            return
        if d.journal is not None and d.journal not in d.claims:
            gate_step2(); return
        if d.claimed_rec is not None: return                  # admitted task held; nothing more to admit
        if gate_step1(f"t{claimed+1}"):
            if mode == "crash_between": return
            gate_step2()

    def finish():                                             # the admitted task completes
        if d.claimed_rec is not None: d.results.add(d.claimed_rec)

    def kick(): d.request = True; d.writers.add(("request", "kick-pool"))
    def wait(): now[0] += WINDOW + 1
    def crash_worker():
        if verdict() == "probation" and d.journal is None and d.claimed_rec is None: gate_step1(f"t{claimed+1}")

    steps = {"kick": kick, "sweep": sweep, "worker": worker, "event": worker, "restart": lambda: None,
             "crash_worker": crash_worker, "finish": finish, "wait": wait}
    for s in order: steps[s]()
    return verdict(), claimed, pending - claimed, d


def record_writers(d): return {a for art, a in d.writers if art == "record"}


class TheSeamWithoutReconciliationGoesDarkForever(unittest.TestCase):
    """qingyun-wu's control (33c16809): the REJECTED design suppresses forever after the seam crash."""

    def test_crash_between_rename_and_claim_suppresses_forever(self):
        v, c, p, d = run(["kick", "sweep", "crash_worker", "restart", "worker", "worker"], "seam")
        self.assertEqual((v, c, p), ("probation", 0, 5))
        self.assertFalse(d.token); self.assertIsNotNone(d.journal)


class TheJournalReconcilesTheSeam(unittest.TestCase):
    def test_crash_between_rename_and_claim_is_recovered_on_restart(self):
        v, c, p, d = run(["kick", "sweep", "crash_worker", "restart", "worker"])
        self.assertEqual((v, c, p), ("probation", 1, 4)); self.assertIsNone(d.journal); self.assertEqual(d.claimed_rec, "t1")

    def test_claim_collision_returns_the_token(self):
        v, c, p, d = run(["kick", "sweep", "worker"], claim_fails_once=True)
        self.assertEqual((v, c, p), ("probation", 0, 5)); self.assertTrue(d.token)
        v, c, p, d2 = run(["kick", "sweep", "worker", "worker"], claim_fails_once=True)
        self.assertEqual((v, c, p), ("probation", 1, 4))

    def test_all_four_orderings_admit_exactly_one(self):
        for order in (["kick", "sweep", "worker"], ["kick", "worker", "sweep", "worker"],
                      ["kick", "sweep", "worker", "worker", "worker"], ["kick", "sweep", "worker", "event"]):
            v, c, p, d = run(order)
            self.assertEqual((v, c, p), ("probation", 1, 4), order)
            self.assertEqual(record_writers(d), {"sweep"}, order)


class EveryProbationStateHasAClock(unittest.TestCase):
    """keweichen's and qingyun-wu's controls (e8d270b1): the three states their reads left absorbing."""

    def test_issuance_crash_is_reconciled_by_the_next_sweep(self):
        # crash after the token, before the record: the request still stands, the next sweep finishes issuance.
        v, c, p, d = run(["kick", "sweep", "restart", "worker"], "crash_issuance")
        self.assertEqual((v, c, p), ("wedged", 0, 5))                   # nothing admitted through a half-issued state
        v, c, p, d = run(["kick", "sweep", "restart", "sweep", "worker"], "crash_issuance")
        self.assertEqual((v, c, p), ("probation", 1, 4)); self.assertFalse(d.request)

    def test_record_published_but_token_lost_is_reissued(self):
        # the reviewers' state: probation on the record, no token, no journal -> the next sweep re-creates the token.
        v, c, p, d = run(["kick", "sweep", "restart", "worker"], "lost_token")
        self.assertEqual((v, c, p), ("probation", 0, 5))                # suppressed, not admitted through the eligible path
        v, c, p, d = run(["kick", "sweep", "restart", "sweep", "worker"], "lost_token")
        self.assertEqual((v, c, p), ("probation", 1, 4))

    def test_token_never_consumed_ends_wedged_after_the_window(self):
        v, c, p, d = run(["kick", "sweep", "wait", "sweep"])
        self.assertEqual((v, c, p), ("wedged", 0, 5)); self.assertFalse(d.token); self.assertNotIn("w", d.probation)

    def test_claimed_and_unfinished_past_the_window_ends_wedged(self):
        v, c, p, d = run(["kick", "sweep", "worker", "wait", "sweep"])
        self.assertEqual(v, "wedged"); self.assertIsNone(d.claimed_rec); self.assertNotIn("w", d.probation)

    def test_admitted_task_finishing_ends_eligible(self):
        v, c, p, d = run(["kick", "sweep", "worker", "finish", "sweep"])
        self.assertEqual(v, "eligible"); self.assertNotIn("w", d.probation)

    def test_a_v1_reader_sees_only_wedged_while_probation_stands(self):
        v, c, p, d = run(["kick", "sweep"])
        self.assertEqual(v, "probation"); self.assertEqual(d.record["w"], "wedged")   # scalar fails closed

    def test_no_schedule_leaves_probation_as_the_last_word(self):
        for order in (["kick", "sweep", "wait", "sweep"], ["kick", "sweep", "worker", "wait", "sweep"],
                      ["kick", "sweep", "crash_worker", "restart", "wait", "sweep"],
                      ["kick", "sweep", "restart", "sweep", "wait", "sweep"]):
            v, c, p, d = run(order)
            self.assertIn(v, ("eligible", "wedged"), order)


if __name__ == "__main__":
    unittest.main()
