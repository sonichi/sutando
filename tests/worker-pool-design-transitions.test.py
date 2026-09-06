"""Transition model with ACTOR-OWNED durable state, a SEPARABLE crash seam, and a CLOCK.

Each durable write must be its own step, because collapsing any two of them makes the seam
between them inexpressible: token consumption with the task claim, record publication with token
creation (which also removes the clock, so neither the issuance crash nor a timeout can be
modeled), and the claim with its journal promotion. Here every durable write is its own step on a
modeled disk, `crash_*` may land between
any two, restart re-reads disk under a NEW process identity, and the sweep carries a clock. Every
schedule must end in `eligible` or `wedged`; `probation` is never terminal.

Claims carry an OWNER because the third seam is not decidable from the journal: production reads
`claim_is_ours` (the claim's `WATCHER_ID`) and `claim_is_live` (its pid), and `WATCHER_ID` is
`$$-$RANDOM`, so a worker's own pre-restart claim is neither ours nor live.
"""
import unittest

RUNNERS = 2
WINDOW = 100          # stand_in_after_s, in model seconds

# STALE < WINDOW mirrors v1's 180 < 300: the snapshot expires first, and that gap is the bug.
STALE = 60


def artifact_path(instance, phase, task=None):
    """`<instance>.admit/{token | held/<task_id> | claimed/<task_id>}`.

    A task id is one path component already (`tasks/task-<id>.txt`), so it cannot contain `/`:
    the id is the WHOLE final segment and the phase is a segment of its own. Injective.
    """
    if phase == "token":
        return f"{instance}.admit/token"
    return f"{instance}.admit/{phase}/{task}"


def legacy_artifact_path(instance, phase, task):
    """The REJECTED flat form: phase appended to the name. Kept only as the control's other half."""
    return f"{instance}.admit.{task}" + (".claimed" if phase == "claimed" else "")


class Disk:
    def __init__(self):
        # record: sweep-only. token/journal/claimed: sweep creates, worker renames, sweep removes.
        # request: kick-pool. results: the worker finishing an admitted task.
        self.record, self.probation, self.request = {}, {}, False
        self.admit_dir = False        # <instance>.admit/ exists -- may hold NO file yet
        self.token, self.journal, self.claimed_rec = False, None, None   # token / held/<t> / claimed/<t>
        self.token_at, self.journal_at, self.claimed_at = None, None, None
        self.computed_at = None      # when the sweep last published; the snapshot's own clock
        self.claims, self.results, self.writers = {}, set(), set()   # claims: task -> owner id
        self.live_owners = set()


def run(order, mode="token", pending=5, runners=RUNNERS, claim_fails_once=False,
        durable_gate=True, flat_issuance=False, unconditional_gate=True,
        dir_is_allowance=False):
    """Three separable pre-fix knobs, so a control isolates ONE defect at a time.

    `durable_gate=False`  -- no directory gate at all (the pre-#3860 reader).
    `unconditional_gate=False` -- the gate runs only PAST snapshot expiry, so a fresh or
        republished snapshot releases the worker over a standing artifact.
    `flat_issuance=True`  -- issuance step (a) tests the TOKEN NAME, which a worker's rename
        makes absent. Step (a) only: the recovery branch already tested the whole family.
    """
    d = Disk(); d.record["w"] = "wedged"; claimed = 0; fail = [claim_fails_once]; now = [0]
    crash_once = [mode == "crash_issuance"]
    crash_mkdir = [mode == "crash_mkdir"]
    me = ["p1"]; d.live_owners.add("p1")          # WATCHER_ID of the running worker process

    def allowance():
        """A FILE under <instance>.admit/ -- token, held/<t> or claimed/<t>.

        Directory existence is NOT this question: mkdir and the token write are two writes, so an
        empty directory means issuance stopped between them, never that an allowance was minted.
        """
        return d.token or d.journal is not None or d.claimed_rec is not None

    def verdict():
        # The directory gate is UNCONDITIONAL -- before every record read, not only past expiry.
        # Gating it on staleness left a fresh-but-unpublished snapshot releasing the worker.
        stale = d.computed_at is not None and now[0] - d.computed_at > STALE
        if durable_gate and allowance() and (unconditional_gate or stale):
            return "probation"
        # Past stale_after_s every cell reads ABSENT, which means eligible.
        if stale:
            return "eligible"
        # a v1 reader sees only the scalar; a probation-aware reader consults the sibling key
        return "probation" if d.record["w"] == "wedged" and "w" in d.probation else d.record["w"]

    def clock_start():
        """The one durable timestamp for the current probation shape, or None if it has none."""
        if d.claimed_rec is not None: return d.claimed_at
        if d.journal is not None: return d.journal_at   # claimed or not: the only stamp that exists here
        if d.token: return d.token_at
        return None

    def sweep():
        now[0] += 10; d.writers.add(("record", "sweep")); d.computed_at = now[0]
        if d.request and "w" not in d.probation:
            # (a) mkdir, then the token write: TWO writes, so a crash lands between them.
            # EEXIST alone proves only the directory; recovery asks for a FILE.
            d.admit_dir = True
            if crash_mkdir[0]:                                # crash after mkdir, before token
                crash_mkdir[0] = False; return
            if dir_is_allowance and d.admit_dir and not allowance():
                pass                                          # pre-fix: EEXIST read as a mint
            elif not (d.token if flat_issuance else allowance()):
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
            # `dir_is_allowance=True` is the pre-fix reading: EEXIST proves a mint, so an
            # empty directory is never finished and the verdict has no clock to end on.
            if not allowance() and not (dir_is_allowance and d.admit_dir):
                d.admit_dir = True                            # an empty directory is unfinished issuance
                d.token, d.token_at = True, now[0]            # finish it exactly once
                return
            if d.claimed_rec is not None and d.claimed_rec in d.results:
                # rmtree(<instance>.admit): the whole directory, not just the phase that ended it
                d.probation.pop("w"); d.token = False; d.journal = None; d.claimed_rec = None
                d.admit_dir = False; d.record["w"] = "eligible"; return
            start = clock_start()
            # A fallback start would silently supply a clock the design lacks, so a shape with
            # no clock must be expressible or the timeout test passes however the clocks read.
            if start is not None and now[0] - start > WINDOW:
                d.probation.pop("w"); d.token = False; d.journal = None; d.claimed_rec = None
                d.admit_dir = False; d.record["w"] = "wedged"; return
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
    def drift(): now[0] += STALE + 2      # past snapshot expiry, INSIDE probation's own window
    def crash_worker():
        if verdict() == "probation" and d.journal is None and d.claimed_rec is None: gate_step1(f"t{claimed+1}")

    def restart():
        d.live_owners.discard(me[0]); me[0] = f"p{len(d.live_owners) + 2}"; d.live_owners.add(me[0])

    def other_live_claim():
        """A DIFFERENT, living instance takes the admitted task out from under us."""
        if d.journal is not None:
            d.claims[d.journal] = "other"; d.live_owners.add("other")

    steps = {"kick": kick, "sweep": sweep, "worker": worker, "event": worker, "restart": restart,
             "crash_worker": crash_worker, "finish": finish, "wait": wait, "drift": drift,
             "other_live_claim": other_live_claim}
    for s in order: steps[s]()
    return verdict(), claimed, pending - claimed, d


def record_writers(d): return {a for art, a in d.writers if art == "record"}


class TheSeamWithoutReconciliationGoesDarkForever(unittest.TestCase):
    """The REJECTED design, kept as a control: it suppresses forever after the seam crash."""

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
    """The three states an earlier reading left absorbing, each pinned with its clock."""

    def test_issuance_crash_is_reconciled_by_the_next_sweep(self):
        """Crash after the token, before the record: the request stands, the next sweep finishes it.

        The token IS the allowance, so the unconditional gate admits one off it even unpublished.
        This expected `wedged, 0, 5` while the gate ran only past snapshot expiry.
        """
        v, c, p, d = run(["kick", "sweep", "restart", "worker"], "crash_issuance")
        self.assertEqual((v, c, p), ("probation", 1, 4))
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
    """A held claim beside an UNPROMOTED journal.

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


class ProbationOutlivesTheEligibilitySnapshot(unittest.TestCase):
    """Probation must outlive the eligibility snapshot.

    v1 fixes `stale_after_s` at 180 while `stand_in_after_s` defaults to 300, so a stopped sweep
    erases probation from the record 120 s BEFORE probation's own window closes. ABSENT means
    eligible-if-the-beat-is-fresh, so the worker took the ordinary path and the one-token bound
    was gone. The model could not express it: `verdict()` had no snapshot clock at all.
    """

    ORDER = ["kick", "sweep", "worker", "drift", "worker", "event"]

    def test_the_pre_fix_reader_releases_the_worker_past_snapshot_expiry(self):
        # The defect itself, so the class is not just asserting the fix agrees with itself.
        v, c, p, d = run(self.ORDER, durable_gate=False)
        self.assertEqual(v, "eligible", "the record reads ABSENT once the snapshot expires")
        self.assertGreater(c, 1, "the ordinary path admits beyond the one-token bound")

    def test_the_durable_gate_holds_the_bound_across_expiry(self):
        v, c, p, d = run(self.ORDER)
        self.assertEqual(v, "probation")
        self.assertEqual(c, 1, "one token, one task, whatever the snapshot says")

    def test_it_holds_for_an_UNCONSUMED_token_too(self):
        # The other half of the boundary: expiry before the worker ever reaches its gate.
        v, c, p, d = run(["kick", "sweep", "drift", "worker", "event"])
        self.assertEqual(v, "probation")
        self.assertEqual(c, 1)
        v, c, p, d = run(["kick", "sweep", "drift", "worker", "event"], durable_gate=False)
        self.assertGreater(c, 1)

    def test_a_returning_sweep_still_terminates_it(self):
        # The cost of the fix is a dead publisher holding the worker; a LIVE one must still end it.
        v, c, p, d = run(["kick", "sweep", "worker", "drift", "finish", "sweep"])
        self.assertEqual(v, "eligible")
        v, c, p, d = run(["kick", "sweep", "worker", "drift", "wait", "sweep"])
        self.assertEqual(v, "wedged")

    def test_no_schedule_crossing_expiry_leaves_probation_as_the_last_word(self):
        for order in (["kick", "sweep", "worker", "drift", "wait", "sweep"],
                      ["kick", "sweep", "drift", "worker", "wait", "sweep"],
                      ["kick", "sweep", "worker", "drift", "restart", "worker", "wait", "sweep"]):
            v, c, p, d = run(order)
            self.assertIn(v, ("eligible", "wedged"), order)


class IssuanceRecoveryMintsAtMostOneAllowance(unittest.TestCase):
    """Issuance recovery must mint at most one allowance.

    `O_EXCL` guards a NAME, never that name's renamed successor. Under the flat form the worker's
    first rename consumed the very name issuance tested, so a sweep restarting into a crashed
    issuance found it absent and minted a second allowance beside an already-claimed task. Both
    reviewers reproduced it in the then-committed model without modifying it.
    """

    ORDER = ["kick", "sweep", "drift", "worker", "sweep"]

    def test_the_flat_form_mints_a_second_allowance(self):
        # The defect itself, so this class is not just asserting the fix agrees with itself.
        # `flat_issuance` restores the pre-fix test: mint whenever the TOKEN NAME is absent.
        v, c, p, d = run(self.ORDER, "crash_issuance", flat_issuance=True)
        self.assertEqual(d.claimed_rec, "t1", "the worker has already spent the allowance")
        self.assertTrue(d.token, "and a second one was minted beside it")

    def test_the_directory_test_covers_every_phase(self):
        v, c, p, d = run(self.ORDER, "crash_issuance")
        self.assertEqual(d.claimed_rec, "t1")
        self.assertFalse(d.token, "EEXIST in the claimed phase: recovery publishes and mints nothing")
        self.assertEqual(c, 1, "one task, whatever the crash schedule")

    def test_it_holds_at_the_journal_seam_too(self):
        # crash after the token, worker consumes as far as the JOURNAL only, then the sweep restarts
        for order in (["kick", "sweep", "crash_worker", "sweep"],
                      ["kick", "sweep", "drift", "crash_worker", "sweep"],
                      ["kick", "sweep", "worker", "restart", "sweep"]):
            v, c, p, d = run(order, "crash_issuance")
            self.assertFalse(d.token and (d.journal is not None or d.claimed_rec is not None),
                             f"{order}: a token beside a spent allowance")

    def test_fresh_and_expired_snapshots_both_hold_the_bound(self):
        # while the allowance stands, one task -- whichever side of snapshot expiry the worker runs on
        for order in (["kick", "sweep", "worker", "sweep", "worker", "event"],
                      ["kick", "sweep", "drift", "worker", "sweep", "worker", "event"]):
            v, c, p, d = run(order, "crash_issuance")
            self.assertEqual(c, 1, f"{order}: more than one task admitted under one allowance")
            self.assertEqual(v, "probation", order)

    def test_ending_probation_leaves_no_artifact_behind(self):
        """Reopening the ordinary path once probation ENDS is correct; a leftover allowance is not.

        Before the fix this schedule ended `verdict=eligible claimed=5 token=True`. Each knob
        below reproduces one half of that row against the current model. The flat mint itself is
        asserted by the first two tests in this class, where it
        stands beside an already-claimed task; it is not re-asserted at the END of this schedule,
        because ending probation now removes the whole directory and would mask it.
        """
        ORDER = ["kick", "sweep", "drift", "worker", "sweep", "finish", "sweep", "worker"]

        # (i) the conditional gate: a republished snapshot releases the worker over an artifact
        v, c, p, d = run(ORDER, "crash_issuance", unconditional_gate=False)
        self.assertEqual((v, c), ("eligible", 5), "the gate must be conditional to release here")

        # (ii) both pre-fix halves together: the leftover is minted and then spent
        v, c, p, d = run(ORDER, "crash_issuance", flat_issuance=True, unconditional_gate=False)
        self.assertEqual((v, c), ("eligible", 5))

        # both fixed: exactly one admission WHILE probation stood, and the directory goes with it.
        # The ordinary path reopening afterwards is correct -- probation ended on its own terms.
        v, c, p, d = run(ORDER[:-1], "crash_issuance")
        self.assertEqual((v, c), ("eligible", 1), "one task under the allowance, then released")
        self.assertFalse(d.token or d.journal is not None or d.claimed_rec is not None,
                         "no artifact may outlive the probation it gates")

    def test_no_issuance_crash_schedule_leaves_probation_as_the_last_word(self):
        for order in (["kick", "sweep", "restart", "sweep", "wait", "sweep"],
                      ["kick", "sweep", "drift", "worker", "sweep", "wait", "sweep"],
                      ["kick", "sweep", "worker", "sweep", "finish", "sweep"]):
            v, c, p, d = run(order, "crash_issuance")
            self.assertIn(v, ("eligible", "wedged"), order)


class TheArtifactPathIsInjective(unittest.TestCase):
    """`<instance>.admit.<task_id>[.claimed]` is not a task identity.

    Gateway ids may contain dots and state-looking tails -- the design says so itself under
    "Order of claiming", and the gateway bridge accepts both ids below. The model's own
    `journal` and `claimed_rec` are separate fields, so the collision is invisible to it; it is
    only visible in the PATH, which is why this control compares paths and not model state.
    """

    # both legal, and adjacent: one is the other plus the phase suffix the flat form appended
    IDS = ("task-a", "task-a.claimed")

    def test_the_flat_form_collides_on_two_legal_adjacent_ids(self):
        promoted = legacy_artifact_path("worker-2", "claimed", self.IDS[0])
        unpromoted = legacy_artifact_path("worker-2", "held", self.IDS[1])
        self.assertEqual(promoted, unpromoted,
                         "the control must exhibit the collision, or it proves nothing")

    def test_the_segmented_form_separates_them(self):
        promoted = artifact_path("worker-2", "claimed", self.IDS[0])
        unpromoted = artifact_path("worker-2", "held", self.IDS[1])
        self.assertNotEqual(promoted, unpromoted)
        self.assertEqual(promoted, "worker-2.admit/claimed/task-a")
        self.assertEqual(unpromoted, "worker-2.admit/held/task-a.claimed")

    def test_the_id_is_the_whole_final_segment_for_every_shape(self):
        for task in ("t1", "task-a", "task-a.claimed", "task.held.claimed", "a.b.c.claimed"):
            for phase in ("held", "claimed"):
                path = artifact_path("worker-2", phase, task)
                self.assertEqual(path.rsplit("/", 1)[1], task, (phase, task))
                self.assertEqual(path.split("/")[1], phase, (phase, task))

    def test_no_task_id_can_forge_another_phase(self):
        # a slash is the only thing that could, and a task id is already one path component
        seen = {}
        for task in ("t", "t.claimed", "claimed", "token", "held/x"):
            for phase in ("held", "claimed"):
                seen.setdefault(artifact_path("worker-2", phase, task), []).append((phase, task))
        forged = {k: v for k, v in seen.items() if len(v) > 1}
        self.assertEqual(forged, {}, f"two (phase, task) pairs share a path: {forged}")
        self.assertNotIn(artifact_path("worker-2", "token"), seen,
                         "an unconsumed token must not be reachable by any task id")


class AnEmptyAllowanceDirectoryIsUnfinishedIssuance(unittest.TestCase):
    """`mkdir` and the token write are TWO writes, so a crash leaves an empty directory.

    Reading `EEXIST` as proof of a minted allowance publishes probation over a directory holding
    no token, no journal and no claimed record: nothing to consume, and no timestamp for any of
    the three clocks, so the verdict can never end. An existence test standing in for a state
    test, which is the same shape as the flat form's defect one layer down.
    """

    ORDER = ["kick", "sweep", "restart", "sweep", "worker"]

    def test_the_existence_reading_wedges_the_room_forever(self):
        # The defect itself, so this class is not asserting the fix agrees with itself.
        v, c, p, d = run(self.ORDER, "crash_mkdir", dir_is_allowance=True)
        self.assertTrue(d.admit_dir, "the directory is there")
        self.assertFalse(d.token or d.journal is not None or d.claimed_rec is not None,
                         "and it holds nothing")
        self.assertEqual((v, c), ("probation", 0), "published over an empty directory")
        # And it is ABSORBING, which is the harm: no clock can start, so no sweep ends it.
        for tail in (["wait", "sweep"], ["sweep", "sweep", "wait", "sweep"]):
            v2, c2, _p, _d = run(self.ORDER + tail, "crash_mkdir", dir_is_allowance=True)
            self.assertEqual((v2, c2), ("probation", 0), tail)

    def test_recovery_finishes_issuance_and_the_worker_admits_one(self):
        v, c, p, d = run(self.ORDER, "crash_mkdir")
        self.assertEqual((v, c, p), ("probation", 1, 4))
        self.assertEqual(d.claimed_rec, "t1")

    def test_it_finishes_exactly_once_however_many_sweeps_run(self):
        for extra in range(1, 4):
            order = ["kick", "sweep", "restart"] + ["sweep"] * extra + ["worker", "worker", "event"]
            v, c, p, d = run(order, "crash_mkdir")
            self.assertEqual(c, 1, f"{extra} recovery sweeps admitted {c}")

    def test_every_crash_mkdir_schedule_still_terminates(self):
        for order in (["kick", "sweep", "restart", "sweep", "wait", "sweep"],
                      ["kick", "sweep", "restart", "sweep", "worker", "wait", "sweep"],
                      ["kick", "sweep", "restart", "sweep", "worker", "finish", "sweep"],
                      ["kick", "sweep", "drift", "sweep", "worker", "wait", "sweep"]):
            v, c, p, d = run(order, "crash_mkdir")
            self.assertIn(v, ("eligible", "wedged"), order)
            self.assertFalse(d.admit_dir, f"{order}: the directory outlived its probation")

    def test_an_empty_directory_is_not_an_allowance_for_the_GATE_either(self):
        # After the crash the directory stands with no file: the worker must NOT be held by it,
        # because holding on a phase-less directory is the wedge this class exists to remove.
        v, c, p, d = run(["kick", "sweep", "worker"], "crash_mkdir")
        self.assertTrue(d.admit_dir)
        self.assertEqual(c, 0, "nothing to consume yet")
        self.assertEqual(v, "wedged", "the record still reads wedged; probation was never published")


if __name__ == "__main__":
    unittest.main()
