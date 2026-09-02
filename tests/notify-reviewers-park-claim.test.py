#!/usr/bin/env python3
"""Two concurrent callers must not both claim one reviewer's park.

Sequential cases cannot see this: check-then-append passes every one of them and
still lets two processes read "not parked", both append, and both post.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py"
MSG = "re-review https://github.com/sonichi/sutando/pull/3509"

# The child calls the PRODUCTION claim, not a re-implementation of it: a copy
# would pass while the shipped writer raced.
CHILD = f"""
import importlib.util, json, os, sys, time
spec = importlib.util.spec_from_file_location("nr", {str(SCRIPT)!r})
nr = importlib.util.module_from_spec(spec)
sys.path.insert(0, {str(REPO / "src")!r})
spec.loader.exec_module(nr)
gate = sys.argv[1]
while not os.path.exists(gate):
    time.sleep(0.002)
try:
    got = nr.claim_park({MSG!r}, "k", "k")
except OSError as e:
    got = "error:" + str(e)
print(json.dumps({{"claim": got}}))
"""


def _load():
    spec = importlib.util.spec_from_file_location("nr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "src"))
    spec.loader.exec_module(mod)
    return mod


class OneClaimWins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = pathlib.Path(self.tmp) / "asks.jsonl"
        self.gate = pathlib.Path(self.tmp) / "go"
        self.prev = os.environ.get("SUTANDO_REVIEW_ASKS_LEDGER")
        os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(self.led)

    def tearDown(self):
        if self.prev is None:
            os.environ.pop("SUTANDO_REVIEW_ASKS_LEDGER", None)
        else:
            os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = self.prev

    def _race(self, n=2):
        src = pathlib.Path(self.tmp) / "child.py"
        src.write_text(CHILD)
        procs = [subprocess.Popen([sys.executable, str(src), str(self.gate)],
                                  stdout=subprocess.PIPE, text=True,
                                  env=dict(os.environ)) for _ in range(n)]
        time.sleep(0.25)          # let every child reach the gate first
        self.gate.write_text("go")
        return [json.loads(p.communicate()[0] or '{"claim": null}')["claim"]
                for p in procs]

    def test_exactly_one_of_two_concurrent_callers_claims_the_park(self):
        claims = self._race()
        won = [c for c in claims if isinstance(c, int) and c]
        lost = [c for c in claims if c is None]
        self.assertEqual(len(won), 1, f"both callers claimed: {claims}")
        self.assertEqual(len(lost), 1, f"nobody was refused: {claims}")

    def test_the_race_leaves_exactly_one_pending_row(self):
        self._race()
        rows = [json.loads(l) for l in self.led.read_text().splitlines()]
        pending = [r for r in rows if r["outcome"] == "pending"]
        self.assertEqual(len(pending), 1, f"two reservations for one actor: {rows}")

    def test_the_control_a_serial_second_call_is_also_refused(self):
        # Proves the assertion above is about the CLAIM and not about the two
        # children failing to start: the same refusal holds without any race.
        nr = _load()
        first = nr.claim_park(MSG, "k", "k")
        second = nr.claim_park(MSG, "k", "k")
        self.assertTrue(first)
        self.assertIsNone(second)

    def test_a_recased_pr_url_is_the_same_pull_request(self):
        # GitHub owner/name is case-insensitive, so a re-cased URL named the
        # same PR while the park treated it as a second one.
        nr = _load()
        up = MSG.replace("sonichi/sutando", "Sonichi/Sutando")
        self.assertNotEqual(up, MSG, "fixture did not actually re-case the URL")
        self.assertTrue(nr.claim_park(MSG, "k", "k"))
        nr.record_asks(MSG, "k", outcome="unknown", actor="k")
        self.assertIsNone(nr.claim_park(up, "k", "k"),
                          "a re-cased URL bypassed the park")

    def test_removing_an_alias_does_not_release_the_endpoints_park(self):
        # The roster spelling is mutable and the endpoint is not; keying on the
        # spelling let a renamed alias re-ask the same recipient.
        nr = _load()
        ep = "@stand:ag2.space"
        self.assertTrue(nr.claim_park(MSG, "k", "alpha", endpoint=ep))
        nr.record_asks(MSG, "k", outcome="unknown", actor="alpha", endpoint=ep)
        self.assertIsNone(nr.claim_park(MSG, "k", "beta", endpoint=ep),
                          "an alias rename bypassed the park")

    def test_the_discord_route_carries_a_durable_endpoint(self):
        # The fix keyed the park on `stand`, which a Discord target does not
        # have — so the ordinary Discord route was left entirely unkeyed.
        nr = _load()
        roster = {"kewei": {"stand_name": "kewei-red", "discord_id": "153795",
                            "home_channel": "1535008"}}
        targets, _ = nr.resolve(["kewei"], roster)
        self.assertEqual(targets[0]["transport"], "discord")
        self.assertIsNone(targets[0]["stand"], "fixture stopped being Discord-only")
        ep = targets[0].get("endpoint")
        self.assertTrue(ep, "a Discord target carries no durable endpoint")
        self.assertTrue(nr.claim_park(MSG, "kewei", "alpha", endpoint=ep))
        nr.record_asks(MSG, "kewei", outcome="unknown", actor="alpha", endpoint=ep)
        self.assertIsNone(nr.claim_park(MSG, "kewei", "beta", endpoint=ep),
                          "an alias rename bypassed the park on Discord")

    def test_a_settlement_lands_in_its_reservations_stream(self):
        # Reservation keyed by endpoint and settlement keyed by the actor left
        # two streams, so the reservation never got superseded.
        nr = _load()
        ep = "discord:153795"
        nr.claim_park(MSG, "kewei", "alpha", endpoint=ep)
        nr.record_asks(MSG, "kewei", outcome="failed", actor="alpha", endpoint=ep)
        rows = [json.loads(l) for l in self.led.read_text().splitlines()]
        self.assertEqual({r.get("endpoint") for r in rows}, {ep},
                         f"reservation and settlement split streams: {rows}")

    def test_the_negative_control_a_different_endpoint_still_gets_asked(self):
        # Without this, keying on the endpoint could park EVERYONE and every
        # case above would still pass while suppressing legitimate asks.
        nr = _load()
        self.assertTrue(nr.claim_park(MSG, "k", "alpha",
                                      endpoint="@a:ag2.space"))
        nr.record_asks(MSG, "k", outcome="unknown", actor="alpha",
                       endpoint="@a:ag2.space")
        self.assertTrue(nr.claim_park(MSG, "k", "beta",
                                      endpoint="@b:ag2.space"),
                        "a different recipient was wrongly parked")

    def test_the_negative_control_a_released_claim_is_reclaimable(self):
        # Without this, a claim that refused EVERYTHING would pass every case
        # above while suppressing legitimate retries.
        nr = _load()
        self.assertTrue(nr.claim_park(MSG, "k", "k"))
        nr.record_asks(MSG, "k", outcome="failed", actor="k")
        self.assertTrue(nr.claim_park(MSG, "k", "k"),
                        "a released reservation must be claimable again")


class CompactionIsIndistinguishableFromTheFullHistory(unittest.TestCase):
    """The ledger's disk bound. Every stream keeps its earliest real ask and its
    latest outcome; anything else is unreadable by either projection."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = pathlib.Path(self.tmp) / "asks.jsonl"
        self.prev = os.environ.get("SUTANDO_REVIEW_ASKS_LEDGER")
        os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(self.led)
        self.nr = _load()

    def tearDown(self):
        if self.prev is None:
            os.environ.pop("SUTANDO_REVIEW_ASKS_LEDGER", None)
        else:
            os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = self.prev

    def _history(self):
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        td = __import__("datetime").timedelta
        rows = []
        for i in range(300):
            rows.append({"repo": "o/r", "pr": 7, "reviewer": "A", "actor": "A",
                         "ts": (now - td(seconds=3000 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "channel": "room",
                         "outcome": "confirmed" if i == 0 else ("pending" if i % 2 else "failed")})
        for i in range(300):
            rows.append({"repo": "o/r", "pr": 8, "reviewer": "B", "actor": "B",
                         "ts": (now - td(seconds=2000 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "channel": "room", "outcome": "unknown" if i == 0 else "pending"})
        self.led.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_both_projections_are_byte_identical_after_compaction(self):
        self._history()
        before = (self.nr._latest_outcomes(self.led), self.nr._first_ask(self.led))
        rows_before = sum(1 for _ in open(self.led))
        self.nr.compact(self.led)
        self.assertEqual(self.nr._latest_outcomes(self.led), before[0])
        self.assertEqual(self.nr._first_ask(self.led), before[1])
        self.assertLess(sum(1 for _ in open(self.led)), rows_before // 100)

    def test_compaction_does_not_revive_a_retired_component(self):
        """@keweichen round 4. The reducer must REPLACE membership as the
        uncompacted reader does — a union stamps a retired lifecycle's component
        onto the live row, and compaction alone then flips an unrelated claim
        from admitted (1) to parked (None). Driven through the PRODUCTION writer."""
        msg = "re-review https://github.com/o/r/pull/7"
        def seed():
            self.nr.record_asks(msg, "r1", outcome="pending", actor="X",
                                endpoint="@x:1", membership=["actor:A", "actor:B"])
            self.nr.record_asks(msg, "r1", outcome="failed", actor="X", endpoint="@x:1")
            self.nr.record_asks(msg, "r1", outcome="pending", actor="X",
                                endpoint="@x:1", membership=["actor:B"])
            self.nr.record_asks(msg, "r1", outcome="unknown", actor="X", endpoint="@x:1")
        seed()
        self.assertEqual(self.nr.claim_park(msg, "A2", "A2", membership=["actor:A"]), 1,
                         "control: uncompacted, the retired component admits A")
        self.led.unlink(); seed(); self.nr.compact(self.led)
        self.assertEqual(self.nr.claim_park(msg, "A2", "A2", membership=["actor:A"]), 1,
                         "compaction alone must not park a retired component")
        # The unrelated-person control keweichen asked for, and the LIVE side:
        self.assertIsNone(self.nr.claim_park(msg, "B2", "B2", membership=["actor:B"]),
                          "the live component must still park after compaction")

    def test_one_malformed_row_does_not_abort_retry_admission(self):
        """@keweichen round 4 P2. _membership_overlap reparsed raw JSON, so a
        `repo: []` row crashed the whole batch with an unhashable-key TypeError.
        It now projects through _row(), the schema's one owner."""
        msg = "re-review https://github.com/o/r/pull/7"
        self.nr.record_asks(msg, "r1", outcome="unknown", actor="X",
                            endpoint="@x:1", membership=["actor:B"])
        with open(self.led, "a") as fh:
            fh.write(json.dumps({"repo": [], "pr": 7, "reviewer": "z",
                                 "ts": "2026-09-02T02:00:00Z", "outcome": "unknown"}) + "\n")
        got = self.nr.claim_park(msg, "C2", "C2", membership=["actor:C"])
        self.assertEqual(got, 1, "a malformed row must be skipped, not abort the claim")

    def test_membership_survives_compaction(self):
        """@keweichen's P1. `_rewrite` builds retained rows from a closed field
        set; `_membership_overlap` rereads `membership` from the raw row. Drop it
        in compaction and retry admission silently never matches again."""
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        td = __import__("datetime").timedelta
        msg = "see https://github.com/o/r/pull/7"
        rows = [{"repo": "o/r", "pr": 7, "reviewer": "A", "actor": "A",
                 "channel": "room", "outcome": "unknown", "membership": ["actor:A", "actor:B"],
                 "ts": (now - td(seconds=9000)).strftime("%Y-%m-%dT%H:%M:%SZ")}]
        # A SECOND stream carries the bulk, so the park's own stream is not
        # collapsed into it — same-stream padding hides the field under `last`.
        for i in range(2100):
            rows.append({"repo": "o/r", "pr": 9, "reviewer": "Z", "actor": "Z",
                         "channel": "room", "outcome": "pending",
                         "ts": (now - td(seconds=8000 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")})
        self.led.write_text("".join(json.dumps(r) + "\n" for r in rows))
        before = self.nr._membership_overlap(self.led, msg, {"actor:A"})
        self.assertIsNotNone(before, "fixture never established an overlap to lose")
        self.nr._maybe_compact(self.led)
        self.assertIsNotNone(
            self.nr._membership_overlap(self.led, msg, {"actor:A"}),
            "compaction dropped the membership retry admission depends on")

    def test_an_unsafe_unresolved_state_survives_compaction(self):
        # The property that matters: compaction must never clear a park.
        msg = "see https://github.com/o/r/pull/7"
        self.led.write_text(json.dumps(
            {"repo": "o/r", "pr": 7, "reviewer": "A", "actor": "A",
             "ts": "2026-08-29T11:00:00Z", "channel": "room",
             "outcome": "unknown"}) + "\n")
        self.assertTrue(self.nr.unknown_parked(msg, "A", "A"))
        self.nr.compact(self.led)
        self.assertTrue(self.nr.unknown_parked(msg, "A", "A"), "compaction cleared a park")

    def test_compaction_is_idempotent(self):
        self._history()
        self.nr.compact(self.led)
        once = (self.nr._latest_outcomes(self.led), self.nr._first_ask(self.led))
        self.nr.compact(self.led)
        self.assertEqual((self.nr._latest_outcomes(self.led), self.nr._first_ask(self.led)), once)

    def test_the_public_writer_takes_the_ledger_lock(self):
        # record_asks appended WITHOUT the lock, so a durable write vanished
        # inside a compactor's replace. The property is mutual exclusion.
        gate = pathlib.Path(self.tmp) / "held"
        src = pathlib.Path(self.tmp) / "holder.py"
        src.write_text(HOLDER.format(script=str(SCRIPT), led=str(self.led),
                                     gate=str(gate), repo=str(REPO)))
        holder = subprocess.Popen([sys.executable, str(src)], env=dict(os.environ))
        for _ in range(500):
            if gate.exists():
                break
            time.sleep(0.01)
        self.assertTrue(gate.exists(), "the holder never took the lock")
        t0 = time.monotonic()
        self.nr.record_asks("see https://github.com/o/r/pull/9", "W",
                            outcome="unknown", actor="W")
        waited = time.monotonic() - t0
        holder.wait(timeout=10)
        self.assertGreater(waited, 0.35,
                           f"the append did not wait for the lock ({waited:.3f}s)")

    def test_settled_streams_are_evicted_before_the_ceiling_but_active_ones_never(self):
        rows = [{"repo": "o/r", "pr": i, "reviewer": f"s{i}",
                 "ts": f"2026-08-01T{(i // 60) % 24:02d}:{i % 60:02d}:00Z",
                 "channel": "room", "outcome": "confirmed"} for i in range(4500)]
        rows += [{"repo": "o/r", "pr": 90000 + i, "reviewer": f"a{i}",
                  "ts": "2026-08-29T11:00:00Z", "channel": "room",
                  "outcome": "unknown"} for i in range(10)]
        self.led.write_text("".join(json.dumps(r) + "\n" for r in rows))
        with self.nr._ledger_lock(self.led):
            self.nr._maybe_compact(self.led)
        st = self.nr._streams(self.led)
        active = [k for k, v in st.items() if v["last"][0] in ("unknown", "pending")]
        self.assertLessEqual(sum(1 for _ in open(self.led)), self.nr._MAX_ROWS)
        self.assertEqual(len(active), 10, "an active park was evicted")

    def test_an_all_active_ledger_keeps_everything_and_says_so(self):
        # Fail-closed: retention gives way to safety state, loudly.
        self.led.write_text("".join(json.dumps(
            {"repo": "o/r", "pr": i, "reviewer": f"a{i}", "ts": "2026-08-29T11:00:00Z",
             "channel": "room", "outcome": "unknown"}) + "\n" for i in range(5000)))
        import contextlib as _c
        import io as _io
        err = _io.StringIO()
        with _c.redirect_stderr(err), self.nr._ledger_lock(self.led):
            self.nr._maybe_compact(self.led)
        self.assertEqual(sum(1 for _ in open(self.led)), 5000)
        self.assertIn("never evicted", err.getvalue())

    def test_an_accepted_pr_key_survives_compaction_unchanged(self):
        self.led.write_text(json.dumps(
            {"repo": "o/r", "pr": "007", "reviewer": "k",
             "ts": "2026-08-29T11:00:00Z", "channel": "room",
             "outcome": "confirmed"}) + "\n")
        before = self.nr._latest_outcomes(self.led)
        self.nr.compact(self.led)
        self.assertEqual(self.nr._latest_outcomes(self.led), before)

    def test_a_compaction_failure_does_not_undo_a_durable_append(self):
        # Compaction is maintenance AFTER the write. Letting it raise converted
        # a successful reservation into a failed claim and parked an unsent ask.
        orig = self.nr._maybe_compact
        self.nr._maybe_compact = lambda led: (_ for _ in ()).throw(PermissionError("ro"))
        import contextlib as _c
        import io as _io
        err = _io.StringIO()
        try:
            with _c.redirect_stderr(err):
                n = self.nr.record_asks("see https://github.com/o/r/pull/9", "A",
                                        outcome="unknown", actor="A")
        finally:
            self.nr._maybe_compact = orig
        self.assertEqual(n, 1, "the append was reported as a failure")
        self.assertIn("compaction failed", err.getvalue())
        self.assertIn("the append stands", err.getvalue())
        rows = [json.loads(l) for l in self.led.read_text().splitlines()]
        self.assertTrue(any(str(r.get("pr")) == "9" for r in rows))

    def test_the_eviction_ordering_is_total_over_every_accepted_row(self):
        # `repo` may legitimately be None on a legacy row; a tuple sort then
        # compares None with a string when the timestamps tie.
        self.led.write_text("".join(json.dumps(r) + "\n" for r in (
            {"repo": None, "pr": 7, "reviewer": "k", "ts": "2026-08-29T11:00:00Z",
             "channel": "room", "outcome": "confirmed"},
            {"repo": "o/r", "pr": 8, "reviewer": "j", "ts": "2026-08-29T11:00:00Z",
             "channel": "room", "outcome": "confirmed"})))
        self.nr.compact(self.led)           # must not raise TypeError
        self.assertEqual(len(self.nr._streams(self.led)), 2)

    def test_compaction_preserves_the_ledger_mode(self):
        # A fresh inode takes the process umask, widening 0600 to 0644.
        import stat as _stat
        self._history()
        os.chmod(self.led, 0o600)
        self.nr.compact(self.led)
        self.assertEqual(_stat.S_IMODE(self.led.stat().st_mode), 0o600)

    def test_a_second_thread_cannot_bypass_the_ledger_lock(self):
        # The depth counter was process-global, so while one thread held the
        # lock another read it as held and skipped flock entirely.
        import threading
        import time as _t
        self.led.touch()
        order = []

        def holder():
            with self.nr._ledger_lock(self.led):
                order.append("holder-in")
                _t.sleep(0.4)
                order.append("holder-out")

        def other():
            _t.sleep(0.1)
            with self.nr._ledger_lock(self.led):
                order.append("other-in")

        ts = [threading.Thread(target=holder), threading.Thread(target=other)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(order, ["holder-in", "holder-out", "other-in"],
                         f"a second thread bypassed the lock: {order}")

    def test_the_writer_refuses_a_row_its_own_reader_would_drop(self):
        msg = "see https://github.com/o/r/pull/7"
        for label, kw in (("reviewer", {"reviewer": []}),
                          ("actor", {"actor": ["A"]}),
                          ("outcome", {"outcome": ["unknown"]})):
            with self.assertRaises(ValueError, msg=f"{label} was accepted"):
                self.nr.record_asks(msg, kw.get("reviewer", "A"),
                                    outcome=kw.get("outcome", "confirmed"),
                                    actor=kw.get("actor"))

    def test_a_fresh_ledger_is_created_private(self):
        # Under umask 022 a new file is 0644, and this one records who was
        # asked about what.
        import stat as _stat
        fresh = pathlib.Path(self.tmp) / "fresh.jsonl"
        os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(fresh)
        self.nr.record_asks("see https://github.com/o/r/pull/7", "A",
                            outcome="confirmed", actor="A")
        self.assertEqual(_stat.S_IMODE(fresh.stat().st_mode), 0o600)
        self.nr.compact(fresh)
        self.assertEqual(_stat.S_IMODE(fresh.stat().st_mode), 0o600)

    def test_the_writer_refuses_an_outcome_the_reader_would_ignore(self):
        # Appending a row no reader accepts is a silent write; the writer holds
        # the same closed set the reader reads.
        with self.assertRaises(ValueError):
            self.nr.record_asks("see https://github.com/o/r/pull/7", "A", outcome="typo")
        self.assertFalse(self.led.exists() and self.led.read_text().strip())


HOLDER = """
import importlib.util, os, sys, time
spec = importlib.util.spec_from_file_location("nr", {script!r})
nr = importlib.util.module_from_spec(spec)
sys.path.insert(0, os.path.join({repo!r}, "src"))
spec.loader.exec_module(nr)
import pathlib, time as _t
led = pathlib.Path({led!r})
led.parent.mkdir(parents=True, exist_ok=True)
led.touch(exist_ok=True)
with nr._ledger_lock(led):
    pathlib.Path({gate!r}).write_text("held")
    _t.sleep(0.6)
"""

# Same holder, but it marks its RELEASE too, so a waiter can prove it waited
# by what it reads rather than by how long it took.
HOLDER_MARKED = """
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("nr", {script!r})
nr = importlib.util.module_from_spec(spec)
sys.path.insert(0, os.path.join({repo!r}, "src"))
spec.loader.exec_module(nr)
import pathlib, time as _t
led = pathlib.Path({led!r})
led.parent.mkdir(parents=True, exist_ok=True)
led.touch(exist_ok=True)
with nr._ledger_lock(led):
    pathlib.Path({gate!r}).write_text("held")
    _t.sleep(0.6)
    pathlib.Path({gate!r}).write_text("released")
"""


class HoldingOneLedgerIsNotReentrancyOnAnother(unittest.TestCase):
    """Requested by @kewei-red-ag2space; harness rebuilt on their second review.

    The re-entrancy key resolves the ledger path, and nothing pinned the
    ADJACENT axis: holding ledger A must not make ledger B look re-entrant. An
    any-ledger key passes every single-ledger test in this file, and an
    any-ledger key is what broke the writer-lock contract the first time.

    The first version of this test used a real foreign holder and a sleep. It
    could reject correct code and accept the mutant, because observing "held"
    says nothing about how much of the window remains. This one stubs `flock`
    and asserts the OPERATION SEQUENCE, so no wall clock is involved at all.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        d = pathlib.Path(self._td.name)
        self.a, self.b = d / "a.jsonl", d / "b.jsonl"
        for f in (self.a, self.b):
            f.touch()
        self.nr = _load()

    def tearDown(self):
        self._td.cleanup()

    def _ops(self, first, second):
        """Real `_ledger_lock`, recording `flock` instead of performing it."""
        seen = []
        real = self.nr.fcntl

        class _Rec:
            LOCK_EX, LOCK_UN = real.LOCK_EX, real.LOCK_UN

            @staticmethod
            def flock(fd, op):
                seen.append("EX" if op == real.LOCK_EX else "UN")

        self.nr.fcntl = _Rec
        try:
            with self.nr._ledger_lock(first), self.nr._ledger_lock(second):
                pass
        finally:
            self.nr.fcntl = real
        return seen

    def test_a_second_ledger_takes_its_own_lock(self):
        # Two distinct ledgers = two real acquisitions. An any-ledger key
        # records one, because it treats B as already held.
        self.assertEqual(self._ops(self.a, self.b), ["EX", "EX", "UN", "UN"])

    def test_the_same_ledger_is_re_entrant_and_locks_once(self):
        # Ships as a pair: removing re-entrancy satisfies the case above and
        # locks twice here — against a real flock, the writer deadlocking.
        self.assertEqual(self._ops(self.a, self.a), ["EX", "UN"])


class RollbackAndIdentityControls(unittest.TestCase):
    """@keweichen round 5: the two durable-state blockers, pinned with both
    revisions' production code where the claim is cross-revision."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = pathlib.Path(self.tmp) / "led.jsonl"
        os.environ["SUTANDO_REVIEW_ASKS_LEDGER"] = str(self.led)
        self.nr = _load()

    def tearDown(self):
        os.environ.pop("SUTANDO_REVIEW_ASKS_LEDGER", None)

    def _old_reader(self):
        """origin/main's module, its own bytes, loaded fresh — the rollback.

        CI runs suites from a git-less COPY of the tree, where no revision but
        the one on disk exists; there the cross-revision half is unreachable
        and is SKIPPED with its reason, not faked. It runs on any checkout
        with history (dev worktrees, the repo itself)."""
        import subprocess
        got = subprocess.run(
            ["git", "-C", str(REPO), "show",
             "origin/main:skills/collaboration-intelligence/scripts/notify_reviewers.py"],
            capture_output=True, text=True)
        if got.returncode != 0:
            self.skipTest("no origin/main ref here (git-less CI copy) — "
                          "cross-revision control needs a real checkout")
        src = got.stdout
        # The module resolves its repo root as parents[3] at import time, so a
        # flat temp path raises IndexError before any assertion can run.
        f = (pathlib.Path(self.tmp) / "rollback" / "skills"
             / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(src)
        spec = importlib.util.spec_from_file_location("nr_main", f)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_head_written_rows_keep_the_request_spelling(self):
        up = "please re-review https://github.com/Sonichi/Sutando/pull/9"
        self.nr.record_asks(up, "r1", outcome="unknown", actor="X", endpoint="@x:1")
        rows = [json.loads(l) for l in self.led.read_text().splitlines()]
        self.assertEqual(rows[-1]["repo"], "Sonichi/Sutando",
                         "the persisted spelling must be the request's own")

    def test_the_pre_canonicalization_reader_recognizes_head_rows(self):
        """The round-5 P1 control: HEAD's production writer, origin/main's
        production reader (`_stale_repeat_ask`, exact-spelling match) — the
        rollback. Both the uppercase case and the lowercase control must
        report the ask as already made (stale=True), aged past its window."""
        for url_repo in ("Sonichi/Sutando", "sonichi/sutando"):
            msg = f"re-review https://github.com/{url_repo}/pull/9"
            self.led.write_text("")
            self.nr.record_asks(msg, "r1", outcome="unknown",
                                actor="X", endpoint="@x:1")
            rows = [json.loads(l) for l in self.led.read_text().splitlines()]
            rows[-1]["ts"] = "2026-09-02T01:00:00Z"      # aged 91+ minutes
            self.led.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            old = self._old_reader()
            old.ledger_path = lambda: self.led           # main has no env override
            stale, _why = old._stale_repeat_ask(msg, [{"name": "r1"}], {})
            self.assertTrue(stale,
                f"origin/main reader must still recognize {url_repo} after rollback")

    def test_compaction_preserves_the_request_spelling_for_the_old_reader(self):
        """Round-6 P1: the append kept the spelling but _rewrite emitted the
        canonical stream key, so ROUTINE COMPACTION undid the rollback fix.
        Drive the production writer across the automatic compaction trigger,
        then read the aged ask with origin/main's own production reader —
        both casings must stay recognized (repeat refused)."""
        for url_repo in ("Sonichi/Sutando", "sonichi/sutando"):
            msg = f"re-review https://github.com/{url_repo}/pull/9"
            self.led.write_text("")
            self.nr.record_asks(msg, "r1", outcome="unknown",
                                actor="X", endpoint="@x:1")
            # Filler streams push past _COMPACT_ABOVE so compact() takes the
            # PRODUCTION path, not a hand-invoked partial one.
            for i in range(self.nr._COMPACT_ABOVE + 1):
                self.nr.record_asks(
                    f"see https://github.com/o/filler/pull/{10_000 + i}",
                    "f", outcome="failed", actor="F", endpoint="@f:1")
            self.nr.compact(self.led)
            rows = [json.loads(l) for l in self.led.read_text().splitlines()]
            kept = [r for r in rows if str(r.get("pr")) == "9"]
            self.assertEqual([r["repo"] for r in kept], [url_repo],
                             f"compaction must re-emit the request spelling, got {kept}")
            for r in rows:                       # age every stamp past the window
                r["ts"] = "2026-09-02T01:00:00Z"
            self.led.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            old = self._old_reader()
            old.ledger_path = lambda: self.led
            stale, _why = old._stale_repeat_ask(msg, [{"name": "r1"}], {})
            self.assertTrue(stale,
                f"origin/main reader must recognize {url_repo} AFTER compaction")

    def test_both_revisions_dedup_a_recased_url(self):
        self.nr.record_asks("x https://github.com/Sonichi/Sutando/pull/9", "r1",
                            outcome="unknown", actor="X", endpoint="@x:1")
        self.assertTrue(self.nr.unknown_parked(
            "x https://github.com/SONICHI/sutando/pull/9",
            reviewer="r1", actor="X", endpoint="@x:1"),
            "HEAD must dedup the same PR under any casing")

    def test_a_list_reviewer_beside_a_valid_endpoint_is_refused(self):
        """Round-5 P2: `who` won on endpoint, so the list reviewer was never
        looked at — accepted, then re-emitted, then a rollback TypeError."""
        row = {"repo": "o/r", "pr": 7, "reviewer": ["a", "b"], "actor": "X",
               "endpoint": "@x:1", "outcome": "unknown",
               "ts": "2026-09-02T02:00:00Z", "membership": ["actor:B"]}
        self.assertIsNone(self.nr._row(row),
                          "a present malformed identity field must drop the row")

    def test_overlap_reemits_only_string_validated_identity(self):
        """A legacy malformed row on disk must neither crash admission nor have
        its raw reviewer written into a NEW row by the park."""
        msg = "re-review https://github.com/o/r/pull/7"
        with open(self.led, "a") as fh:
            fh.write(json.dumps({"repo": "o/r", "pr": 7, "reviewer": ["a", "b"],
                                 "actor": "X", "endpoint": "@x:1",
                                 "outcome": "unknown", "membership": ["actor:C"],
                                 "ts": "2026-09-02T02:00:00Z"}) + "\n")
        got = self.nr.claim_park(msg, "C2", "C2", membership=["actor:C"])
        for line in self.led.read_text().splitlines():
            d = json.loads(line)
            r = d.get("reviewer")
            if line == self.led.read_text().splitlines()[0]:
                continue                      # the planted legacy row itself
            self.assertTrue(r is None or isinstance(r, str),
                            f"the park re-emitted a raw identity: {d}")

    def test_overlap_reads_through_streams_not_a_second_parser(self):
        """The single-reader claim, held structurally: the function's source
        must fold via _streams and carry no raw json.loads of its own."""
        import inspect
        src = inspect.getsource(self.nr._membership_overlap)
        self.assertIn("_streams(", src)
        self.assertNotIn("json.loads", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
