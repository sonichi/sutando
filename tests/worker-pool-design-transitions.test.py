"""Transition model with ACTOR-OWNED durable state, a SEPARABLE crash seam, and a CLOCK.

Earlier versions collapsed token consumption and task claim into one statement (qingyun-wu,
33c16809), then collapsed record publication and token creation into one statement and had no
clock, so neither the issuance crash nor a timeout could be modeled (qingyun-wu, keweichen,
e8d270b1), then collapsed the CLAIM and its journal promotion into one statement, so the seam
between a held claim and an unpromoted journal could not exist in the model at all (keweichen,
a2e506e1). Here every durable write is its own step on a modeled disk, `crash_*` may land between
any two, restart re-reads disk under a NEW process identity, and the sweep carries a clock. Every
schedule must end in `eligible` or `wedged`; `probation` is never terminal.

Claims carry an OWNER because the third seam is not decidable from the journal: production reads
`claim_is_ours` (the claim's `WATCHER_ID`) and `claim_is_live` (its pid), and `WATCHER_ID` is
`$$-$RANDOM`, so a worker's own pre-restart claim is neither ours nor live.
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
        self.claims, self.results, self.writers = {}, set(), set()   # claims: task -> owner id
        self.live_owners = set()


def run(order, mode="token", pending=5, runners=RUNNERS, claim_fails_once=False):
    d = Disk(); d.record["w"] = "wedged"; claimed = 0; fail = [claim_fails_once]; now = [0]
    crash_once = [mode == "crash_issuance"]
    me = ["p1"]; d.live_owners.add("p1")          # WATCHER_ID of the running worker process

    def verdict():
        # a v1 reader sees only the scalar; a probation-aware reader consults the sibling key
        return "probation" if d.record["w"] == "wedged" and "w" in d.probation else d.record["w"]

    def clock_start():
        """The one durable timestamp for the current probation shape, or None if it has none."""
        if d.claimed_rec is not None: return d.claimed_at
        if d.journal is not None: return d.journal_at   # claimed or not: the only stamp that exists here
        if d.token: return d.token_at
        return None

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
            start = clock_start()
            # A fallback start would silently supply a clock the design lacks, so a shape with
            # no clock must be expressible or the timeout test passes however the clocks read.
            if start is not None and now[0] - start > WINDOW:
                d.probation.pop("w"); d.token = False; d.journal = None; d.claimed_rec = None
                d.record["w"] = "wedged"; return
            return                                            # held, with a clock running
        if claimed and pending - claimed == 0:
            d.record["w"] = "eligible"

    def gate_step1(task):
        if not d.token: return False
        d.token = False; d.journal, d.journal_at = task, now[0]; return True

    def gate_step2a():
        """(2) acquire_task_claim -- commits and RETURNS. Its own durable write."""
        task = d.journal
        if task is None: return False
        if fail[0]:
            fail[0] = False; d.journal = None; d.token = True; return False    # claim failed -> return token
        d.claims[task] = me[0]; return True

    def gate_step2b():
        """(3) promote the journal to .claimed. A SEPARATE durable write; idempotent."""
        nonlocal claimed
        task = d.journal
        if task is None or d.claims.get(task) is None: return
        d.claimed_rec, d.claimed_at = task, now[0]; d.journal = None; claimed += 1

    def worker():
        v = verdict()
        if v == "wedged": return
        if v == "eligible":
            nonlocal claimed; claimed += min(2 * runners, pending - claimed); return
        if mode == "seam" and d.journal is not None:          # the REJECTED design: no reconciliation
            return
        if d.journal is not None:                             # reconcile by the CLAIM, not the journal
            task = d.journal; owner = d.claims.get(task)
            if owner is None:                                 # no claim: retry (2) then (3)
                if gate_step2a(): gate_step2b()
                return
            if owner == me[0]:                                # claim_is_ours: retry (3) ALONE
                gate_step2b(); return
            if owner in d.live_owners:                        # live, not ours: collision, return the attempt
                d.journal = None; d.token = True; return
            del d.claims[task]                                # stale: retire, re-link, then (3)
            if gate_step2a(): gate_step2b()
            return
        if d.claimed_rec is not None: return                  # admitted task held; nothing more to admit
        if gate_step1(f"t{claimed+1}"):
            if mode == "crash_between": return
            if gate_step2a():
                if mode == "crash_after_claim": return        # crash between (2) and (3)
                gate_step2b()

    def finish():                                             # the admitted task completes
        if d.claimed_rec is not None: d.results.add(d.claimed_rec)

    def kick(): d.request = True; d.writers.add(("request", "kick-pool"))
    def wait(): now[0] += WINDOW + 1
    def crash_worker():
        if verdict() == "probation" and d.journal is None and d.claimed_rec is None: gate_step1(f"t{claimed+1}")

    def restart():
        d.live_owners.discard(me[0]); me[0] = f"p{len(d.live_owners) + 2}"; d.live_owners.add(me[0])

    def other_live_claim():
        """A DIFFERENT, living instance takes the admitted task out from under us."""
        if d.journal is not None:
            d.claims[d.journal] = "other"; d.live_owners.add("other")

    steps = {"kick": kick, "sweep": sweep, "worker": worker, "event": worker, "restart": restart,
             "crash_worker": crash_worker, "finish": finish, "wait": wait,
             "other_live_claim": other_live_claim}
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


class TheClaimToPromotionSeamIsCrashComplete(unittest.TestCase):
    """keweichen's control (a2e506e1): a held claim beside an UNPROMOTED journal.

    The claim commits and returns before the promotion runs, so this state is reachable in
    production. It is decidable only from the claim's owner and liveness -- the journal reads
    identically in all three cases below.
    """

    def test_the_seam_is_reachable_at_all(self):
        # the state the previous model could not express: claim held, journal not promoted
        v, c, p, d = run(["kick", "sweep", "worker"], "crash_after_claim")
        self.assertEqual(d.journal, "t1"); self.assertIsNone(d.claimed_rec)
        self.assertEqual(d.claims.get("t1"), "p1"); self.assertEqual((v, c, p), ("probation", 0, 5))

    def test_restart_finds_its_own_dead_claim_and_completes_the_admission(self):
        # WATCHER_ID is per-process, so the pre-restart claim is neither ours nor live -> retire, re-link, promote
        v, c, p, d = run(["kick", "sweep", "worker", "restart", "worker"], "crash_after_claim")
        self.assertEqual(d.claimed_rec, "t1"); self.assertIsNone(d.journal)
        self.assertEqual((v, c, p), ("probation", 1, 4))
        self.assertEqual(d.claims.get("t1"), "p2", "the surviving claim must be the LIVE process's")
        v, c, p, d = run(["kick", "sweep", "worker", "restart", "worker", "finish", "sweep"], "crash_after_claim")
        self.assertEqual(v, "eligible")

    def test_a_failed_promotion_inside_a_living_process_retries_promotion_alone(self):
        # claim_is_ours: the admission is real and already paid for; a blind retry of (2) would
        # read its OWN live claim as a collision and strand the task with no admission record
        v, c, p, d = run(["kick", "sweep", "worker", "worker"], "crash_after_claim")
        self.assertEqual(d.claimed_rec, "t1"); self.assertIsNone(d.journal)
        self.assertEqual((v, c, p), ("probation", 1, 4))
        self.assertFalse(d.token, "promotion must not hand the token back")
        self.assertEqual(d.claims.get("t1"), "p1", "the claim is kept, never retired and retaken")

    def test_a_live_other_owner_returns_the_one_attempt(self):
        v, c, p, d = run(["kick", "sweep", "crash_worker", "other_live_claim", "worker"])
        self.assertIsNone(d.journal); self.assertTrue(d.token, "the attempt is returned, not lost")
        self.assertEqual((v, c, p), ("probation", 0, 5))

    def test_the_seam_carries_a_clock_and_ends_wedged(self):
        # the journal's mtime is the ONLY durable timestamp in this state, so the journal clock
        # must run while the journal stands -- claimed or not
        v, c, p, d = run(["kick", "sweep", "worker", "wait", "sweep"], "crash_after_claim")
        self.assertEqual(v, "wedged"); self.assertNotIn("w", d.probation)
        self.assertIsNone(d.journal); self.assertFalse(d.token)

    def test_no_seam_schedule_leaves_probation_as_the_last_word(self):
        for order in (["kick", "sweep", "worker", "wait", "sweep"],
                      ["kick", "sweep", "worker", "restart", "worker", "wait", "sweep"],
                      ["kick", "sweep", "worker", "worker", "wait", "sweep"],
                      ["kick", "sweep", "worker", "restart", "worker", "finish", "sweep"]):
            v, c, p, d = run(order, "crash_after_claim")
            self.assertIn(v, ("eligible", "wedged"), order)

    def test_the_seam_never_issues_a_second_attempt(self):
        for order in (["kick", "sweep", "worker", "restart", "worker", "worker", "worker"],
                      ["kick", "sweep", "worker", "worker", "worker", "worker"]):
            v, c, p, d = run(order, "crash_after_claim")
            self.assertEqual(c, 1, f"{order}: exactly one task may be admitted per probation")


if __name__ == "__main__":
    unittest.main()
