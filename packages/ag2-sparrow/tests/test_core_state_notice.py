"""core_state_notice — the "why am I silent" sweep.

Covers the notice/recovery lifecycle a room observes across a core outage:
degraded → one notice per (room, reason) per cooldown, failed sends retried
without burning the notice, recovery announced exactly once, and every
no-verdict input (absent/stale/malformed file, unrecognized state, kill
switch) doing NOTHING — the module's failure mode must be silence, not spam.
"""
import json
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ag2_sparrow import core_state_notice as csn


class _Sender:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []  # (room, body)

    def __call__(self, room, body):
        if self.ok:
            self.sent.append((room, body))
        return self.ok


def _write_state(tmp, state, kind=None, mtime=None):
    p = tmp / csn.CORE_SUPERVISOR_FILE
    p.write_text(json.dumps({"state": state, "kind": kind,
                             "detail": "x", "prompt": None}))
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def _write_heartbeat(tmp, ts):
    (tmp / csn.CORE_HEARTBEAT_FILE).write_text(str(int(ts)))


def test_watcher_heartbeat_gates_freshness():
    # Review should-fix #1: freshness must come from the watcher's per-tick
    # heartbeat, not the state file's (write-on-change) mtime.
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        _write_state(tmp, "logged-out")
        # FRESH heartbeat → trust the state even with an ancient state mtime
        _write_state(tmp, "logged-out", mtime=now - 6 * 3600)
        _write_heartbeat(tmp, now - 5)
        s = _Sender()
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        assert len(s.sent) == 1, "fresh heartbeat → degraded state is a verdict"
        # STALE heartbeat → watcher presumed dead → NO verdict (the fix)
        tmp2 = pathlib.Path(tempfile.mkdtemp())
        _write_state(tmp2, "logged-out")
        _write_heartbeat(tmp2, now - csn.WATCHER_STALE_S - 1)
        s2 = _Sender()
        csn.sweep_core_state_notices(tmp2, {"!a:s"}, s2, now=now)
        assert s2.sent == [], "stale heartbeat → watcher dead → no notice"
        # a healthy sweep with a stale heartbeat also does nothing (no false recovery)
        assert csn.read_core_state(tmp2, now) is None


def test_invalid_heartbeat_is_no_verdict():
    # Review r3: an absent heartbeat (old watcher) trusts the state, but a
    # PRESENT-but-garbled one means freshness unknown → no verdict.
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        _write_state(tmp, "logged-out")
        (tmp / csn.CORE_HEARTBEAT_FILE).write_text("not-a-number")
        assert csn.read_core_state(tmp, now) is None
        s = _Sender()
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        assert s.sent == []


def test_recovery_validates_and_purges_targets():
    # r3: recovery purges a corrupt/forged active key (never sends/retries) and
    # only recovers ones passing the surface validator.
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        _write_heartbeat(tmp, now)
        # seed a degraded notice to a good room so `active` has a real entry
        _write_state(tmp, "crashed")
        good = _Sender()
        csn.sweep_core_state_notices(tmp, {"!good:s"}, good, now=now,
                                     recovery_target_ok=lambda r: r.startswith("!"))
        # forge a junk active key directly in the ledger
        import json as _json
        led = _json.loads((tmp / csn.LEDGER_FILE).read_text())
        led["active"]["evil-not-a-room"] = "crashed"
        (tmp / csn.LEDGER_FILE).write_text(_json.dumps(led))
        # go healthy → recovery: good room gets it, junk key purged (no send)
        _write_state(tmp, "idle-ready")
        rec = _Sender()
        csn.sweep_core_state_notices(tmp, set(), rec, now=now + 1,
                                     recovery_target_ok=lambda r: r.startswith("!"))
        assert [room for room, _ in rec.sent] == ["!good:s"]
        active = _json.loads((tmp / csn.LEDGER_FILE).read_text())["active"]
        assert active == {}, "both the recovered and the purged key are cleared"


def test_partial_success_committed_before_exception():
    # Review r4 #2 + r4-followup nit #7: room A succeeds, room B raises → A is
    # noticed EXACTLY ONCE (count the sends, not just the final ledger).
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        _write_state(tmp, "crashed")
        _write_heartbeat(tmp, now)
        a_sends = [0]

        def send(room, body):
            if room == "!b:s":
                raise RuntimeError("boom on B")
            a_sends[0] += 1
            return True

        for tick in range(3):
            try:
                csn.sweep_core_state_notices(tmp, {"!a:s", "!b:s"}, send,
                                             now=now + tick)
            except RuntimeError:
                pass
        assert a_sends[0] == 1, f"A sent {a_sends[0]}x, expected once"
        assert json.loads((tmp / csn.LEDGER_FILE).read_text())["active"].get("!a:s") == "crashed"


def test_recovery_persistence_failure_does_not_resurrect():
    # r4-followup #1: recovery send succeeds but its persist fails → the memory
    # snapshot (active empty) is authoritative, so the next pass must NOT re-send.
    import unittest.mock as mock
    csn._MEM_LEDGERS.clear()
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        _write_heartbeat(tmp, now)
        _write_state(tmp, "crashed")
        s = _Sender()
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)  # persists active={A}
        assert json.loads((tmp / csn.LEDGER_FILE).read_text())["active"] == {"!a:s": "crashed"}
        _write_state(tmp, "idle-ready")
        rec = _Sender()
        with mock.patch.object(csn.os, "replace", side_effect=OSError("ro")):
            for tick in range(3):
                csn.sweep_core_state_notices(tmp, set(), rec, now=now + 1 + tick)
        assert len(rec.sent) == 1, f"recovery re-sent {len(rec.sent)}x — resurrection bug"
    csn._MEM_LEDGERS.clear()


def test_unattempted_rooms_not_charged_a_failure():
    # r4-followup #2: recovery for A,B; A raises before B's turn. B was never
    # attempted, so it must NOT accrue failures / get purged.
    csn._MEM_LEDGERS.clear()
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        _write_heartbeat(tmp, now)
        # seed active for A and B via a degraded pass
        _write_state(tmp, "crashed")
        csn.sweep_core_state_notices(tmp, {"!a:s", "!b:s"}, _Sender(), now=now)
        _write_state(tmp, "idle-ready")

        def raising_send(room, body):
            if room == "!a:s":  # sorts first; aborts the batch before B's turn
                raise RuntimeError("A raises, aborting before B")
            return True

        # ONE pass: A (sorted first) raises before B is reached, so B must accrue
        # no failure and keep its debt (the bug charged every planned item) (#2).
        csn._RR_CURSORS.clear()
        try:
            csn.sweep_core_state_notices(tmp, set(), raising_send, now=now + 1)
        except RuntimeError:
            pass
        led = json.loads((tmp / csn.LEDGER_FILE).read_text())
        assert "!b:s" in led["active"], "B not attempted this pass; must keep its debt"
        assert led.get("fail", {}).get("!b:s", 0) == 0, "B must not accrue a failure"
        assert led.get("fail", {}).get("!a:s", 0) == 1, "A (attempted) is charged once"
    csn._MEM_LEDGERS.clear()
    csn._RR_CURSORS.clear()


def test_batch_cap_rotation_reaches_all_rooms():
    # r4-followup #3: 9 degraded rooms, the first 8 (sorted) always fail; the
    # reachable 9th must eventually get an attempt across rotated passes.
    csn._MEM_LEDGERS.clear()
    csn._RR_CURSORS.clear()
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        _write_heartbeat(tmp, now)
        _write_state(tmp, "crashed")
        rooms = {f"!r{i:02d}:s" for i in range(9)}
        ninth = "!r08:s"  # sorts last
        attempts = {r: 0 for r in rooms}

        def send(room, body):
            attempts[room] += 1
            return room == ninth  # only the 9th would succeed if attempted

        for tick in range(4):  # a few rotated passes
            csn.sweep_core_state_notices(tmp, rooms, send, now=now + tick)
        assert attempts[ninth] >= 1, "rotation never reached the reachable 9th room"
    csn._MEM_LEDGERS.clear()
    csn._RR_CURSORS.clear()


def test_budget_truncation_does_not_starve():
    # r4-followup-2: 3 rooms (< count cap) but the TIME budget truncates each
    # pass; rotation must advance by ACTUAL attempts so the 3rd is reached.
    import unittest.mock as mock
    csn._MEM_LEDGERS.clear()
    csn._RR_CURSORS.clear()
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        _write_heartbeat(tmp, now)
        _write_state(tmp, "crashed")
        rooms = {"!r0:s", "!r1:s", "!r2:s"}
        attempts = {r: 0 for r in rooms}
        clock = [0.0]  # fake monotonic: each send "takes" 6s of the 12s budget

        def send(room, body):
            attempts[room] += 1
            clock[0] += 6.0
            return False  # always fails (no cooldown earned)

        with mock.patch.object(csn.time, "monotonic", lambda: clock[0]):
            for tick in range(3):
                clock[0] = 0.0  # reset the per-sweep monotonic base each pass
                csn.sweep_core_state_notices(tmp, rooms, send, now=now + tick)
        assert attempts["!r2:s"] >= 1, "budget-truncated small batch starved r2"
    csn._MEM_LEDGERS.clear()
    csn._RR_CURSORS.clear()


def test_persistence_failure_keeps_process_local_cooldown():
    # Review r4 #3: if the ledger can't be written, successful-send accounting
    # survives in-process so we don't re-send every sweep.
    import unittest.mock as mock
    csn._MEM_LEDGERS.clear()
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        _write_state(tmp, "crashed")
        _write_heartbeat(tmp, now)
        s = _Sender()
        with mock.patch.object(csn.os, "replace", side_effect=OSError("read-only")):
            for tick in range(3):
                csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now + tick)
        assert len(s.sent) == 1, "one notice despite the disk write failing every sweep"
    csn._MEM_LEDGERS.clear()


def test_numeric_guards():
    # #10 cooldown + #9 heartbeat numeric validation.
    import os as _os
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        for bad in ("nan", "0", "-1"):
            _os.environ["SPARROW_CORE_NOTICE_COOLDOWN_S"] = bad
            try:
                assert csn._cooldown_s() == 1800.0, f"{bad} → default cooldown"
            finally:
                del _os.environ["SPARROW_CORE_NOTICE_COOLDOWN_S"]
        # inf / future heartbeat must NOT read fresh
        _write_state(tmp, "logged-out")
        (tmp / csn.CORE_HEARTBEAT_FILE).write_text("1e999")  # -> inf
        assert csn.read_core_state(tmp, now) is None
        (tmp / csn.CORE_HEARTBEAT_FILE).write_text(str(int(now + 10_000)))  # far future
        assert csn.read_core_state(tmp, now) is None


def test_debounce_suppresses_login_restart_flap():
    # A logout→login→restart flaps the core; a debounce must suppress the
    # premature recovery and only notice/recover once stable for debounce_s.
    csn._DEBOUNCE.clear()
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        _write_heartbeat(tmp, now)
        _write_state(tmp, "crashed")
        s = _Sender()
        # t0: degraded seen but not yet stable → no notice
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now, debounce_s=10)
        assert s.sent == [], "degraded not yet stable → no notice"
        # t+11: still degraded, now stable → one notice
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now + 11, debounce_s=10)
        assert len(s.sent) == 1, "stable degraded → notice fires"
        # brief healthy blip (t+13) → does NOT fire recovery (not stable)
        _write_state(tmp, "idle-ready")
        csn.sweep_core_state_notices(tmp, set(), s, now=now + 13, debounce_s=10)
        assert len(s.sent) == 1, "healthy blip < debounce → no premature recovery"
        # flap back to degraded (t+15) → recovery timer reset, still no recovery
        _write_state(tmp, "crashed")
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now + 15, debounce_s=10)
        # genuinely, stably healthy (t+30) → exactly one recovery
        _write_state(tmp, "idle-ready")
        csn.sweep_core_state_notices(tmp, set(), s, now=now + 20, debounce_s=10)  # healthy first seen
        assert all("back online" not in b for _, b in s.sent), "healthy not stable yet"
        csn.sweep_core_state_notices(tmp, set(), s, now=now + 31, debounce_s=10)  # stable 11s
        assert sum("back online" in b for _, b in s.sent) == 1, "exactly one recovery once stable"
    csn._DEBOUNCE.clear()


def test_generic_human_gate_still_tells_senders():
    # An unlisted blocked-human kind is not sender silence: the owner
    # escalation never reaches these rooms, so the generic line must.
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        _write_heartbeat(tmp, now)
        (tmp / csn.CORE_SUPERVISOR_FILE).write_text(json.dumps(
            {"state": "blocked-human", "kind": "unknown"}))
        s = _Sender()
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        assert len(s.sent) == 1 and "needs my owner" in s.sent[0][1]
        # A listed kind still maps to its specific reason, not the generic one.
        assert csn.degraded_reason("blocked-human", "login") == "logged-out"


def test_hung_needs_a_long_hold_before_senders_hear():
    # "hung" must persist HUNG_NOTICE_MIN_HOLD_S before senders hear — a
    # 3-minute build must not fire a false "stalled"/"back online" pair.
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        _write_heartbeat(tmp, now)
        _write_state(tmp, "hung")
        s = _Sender()
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        _write_heartbeat(tmp, now + 180)
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now + 180)
        assert s.sent == [], "a hung state held < the min hold stays quiet"
        late = now + csn.HUNG_NOTICE_MIN_HOLD_S + 1
        _write_heartbeat(tmp, late)
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=late)
        assert len(s.sent) == 1 and "stalled" in s.sent[0][1]
    csn._DEBOUNCE.clear()


def test_blocked_known_is_not_proof_of_recovery():
    # Not every blocked-known gate is auto-answered (folder-trust never is), so
    # the state can hold indefinitely — it must not discharge recovery debt.
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        (tmp / csn.LEDGER_FILE).write_text(json.dumps({
            "schema_version": 2, "active": {"!a:s": "crashed"},
            "last_sent": {"!a:s": {"crashed": now - 10}}, "fail": {}}))
        _write_heartbeat(tmp, now)
        (tmp / csn.CORE_SUPERVISOR_FILE).write_text(json.dumps(
            {"state": "blocked-known", "kind": "folder-trust"}))
        s = _Sender()
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        assert s.sent == [], "wedged at a known gate ≠ back online"
        _write_state(tmp, "idle-ready")
        csn.sweep_core_state_notices(tmp, set(), s, now=now)
        assert len(s.sent) == 1 and "back online" in s.sent[0][1]


def test_absent_heartbeat_trust_is_age_bounded():
    # A leftover state file with no heartbeat must not notice forever.
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        now = time.time()
        _write_state(tmp, "crashed", mtime=now - csn.ABSENT_HEARTBEAT_TRUST_S - 60)
        s = _Sender()
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        assert s.sent == [] and csn.read_core_state(tmp, now) is None
        _write_state(tmp, "crashed", mtime=now - 30)  # recent → still trusted
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        assert len(s.sent) == 1


def test_no_supervisor_file_does_nothing():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        s = _Sender()
        csn.sweep_core_state_notices(tmp, {"!r:s"}, s)
        assert s.sent == []
        assert not (tmp / csn.LEDGER_FILE).exists()


def test_degraded_notices_once_per_room_with_cooldown():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _write_state(tmp, "blocked-human", kind="session-limit")
        s = _Sender()
        now = time.time()
        _write_heartbeat(tmp, now)  # live watcher: the long-outage path
        csn.sweep_core_state_notices(tmp, {"!a:s", "!b:s"}, s, now=now)
        assert sorted(r for r, _ in s.sent) == ["!a:s", "!b:s"]
        assert all("usage limit" in b for _, b in s.sent)
        # same reason inside the cooldown → silent (per room)
        csn.sweep_core_state_notices(tmp, {"!a:s", "!b:s"}, s, now=now + 60)
        assert len(s.sent) == 2
        # a NEW room mid-outage still gets its notice
        csn.sweep_core_state_notices(tmp, {"!a:s", "!c:s"}, s, now=now + 61)
        assert [r for r, _ in s.sent].count("!c:s") == 1
        # past the cooldown the same room re-notices (one reminder per window),
        # with the file untouched as a write-on-change watcher leaves it
        later = now + csn._cooldown_s() + 1
        _write_heartbeat(tmp, later)  # watcher still alive, state file untouched
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=later)
        assert [r for r, _ in s.sent].count("!a:s") == 2


def test_failed_send_burns_nothing_and_retries():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _write_state(tmp, "logged-out")
        now = time.time()
        failing = _Sender(ok=False)
        csn.sweep_core_state_notices(tmp, {"!a:s"}, failing, now=now)
        assert failing.sent == []
        ok = _Sender()
        csn.sweep_core_state_notices(tmp, {"!a:s"}, ok, now=now + 1)
        assert len(ok.sent) == 1 and "logged out" in ok.sent[0][1]


def test_reason_change_renotices_inside_cooldown():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _write_state(tmp, "blocked-human", kind="session-limit")
        s = _Sender()
        now = time.time()
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        _write_state(tmp, "crashed")
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now + 5)
        assert len(s.sent) == 2
        assert "usage limit" in s.sent[0][1] and "not running" in s.sent[1][1]


def test_recovery_announced_once_and_flap_stays_bounded():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _write_state(tmp, "blocked-human", kind="session-limit")
        s = _Sender()
        now = time.time()
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        _write_state(tmp, "idle-ready")
        # recovery goes to the noticed room even if its task already resolved
        # (rooms arg empty) — the sender was told "I'm down", so tell them back
        csn.sweep_core_state_notices(tmp, set(), s, now=now + 5)
        assert len(s.sent) == 2 and "back online" in s.sent[1][1]
        # healthy again → nothing more
        csn.sweep_core_state_notices(tmp, set(), s, now=now + 6)
        assert len(s.sent) == 2
        # degraded/healthy FLAP (#1): recovery must NOT clear the cooldown —
        # same reason inside the window stays silent, no second recovery owed
        _write_state(tmp, "blocked-human", kind="session-limit")
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now + 7)
        _write_state(tmp, "idle-ready")
        csn.sweep_core_state_notices(tmp, set(), s, now=now + 8)
        assert len(s.sent) == 2
        # past the cooldown a genuine new outage notices again
        later = now + csn._cooldown_s() + 1
        _write_state(tmp, "blocked-human", kind="session-limit", mtime=later)
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=later)
        assert len(s.sent) == 3


def test_alternating_reasons_bounded_per_reason():
    # Review should-fix #1: usage-limit ↔ logged-out must not re-send on every
    # alternation — each reason keeps ITS OWN cooldown for the room.
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        s = _Sender()
        now = time.time()
        seq = [("blocked-human", "session-limit"), ("logged-out", None),
               ("blocked-human", "session-limit"), ("logged-out", None),
               ("blocked-human", "session-limit")]
        for i, (state, kind) in enumerate(seq):
            _write_state(tmp, state, kind=kind)
            csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now + i)
        assert len(s.sent) == 2  # one per reason, not one per alternation
        assert "usage limit" in s.sent[0][1] and "logged out" in s.sent[1][1]


def test_v1_ledger_resets_cleanly():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        (tmp / csn.LEDGER_FILE).write_text(json.dumps({
            "schema_version": 1,
            "noticed": {"!a:s": {"reason": "usage-limit", "ts": time.time()}}}))
        _write_state(tmp, "blocked-human", kind="session-limit")
        s = _Sender()
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s)
        # v1 state is unreadable under v2 → reset: one (duplicate) notice
        # beats wedging, and the file is rewritten as v2
        assert len(s.sent) == 1
        assert json.loads((tmp / csn.LEDGER_FILE).read_text())["schema_version"] == 2


def test_old_mtime_is_still_a_verdict():
    # Write-on-change leaves an old mtime on current content: with a live
    # watcher (fresh heartbeat) age must not gate the verdict.
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        s = _Sender()
        now = time.time()
        _write_state(tmp, "blocked-human", kind="session-limit",
                     mtime=now - 6 * 3600)
        _write_heartbeat(tmp, now)
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        assert len(s.sent) == 1 and "usage limit" in s.sent[0][1]


def test_no_verdict_inputs_do_nothing():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        s = _Sender()
        now = time.time()
        # unrecognized state: neither a notice nor proof of recovery
        _write_state(tmp, "gateway-down")
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        # malformed / non-dict
        (tmp / csn.CORE_SUPERVISOR_FILE).write_text("[]")
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        (tmp / csn.CORE_SUPERVISOR_FILE).write_text("{nope")
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        assert s.sent == []


def test_unrecognized_state_does_not_fake_recovery():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        s = _Sender()
        now = time.time()
        _write_state(tmp, "crashed")
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s, now=now)
        _write_state(tmp, "gateway-down")
        csn.sweep_core_state_notices(tmp, set(), s, now=now + 5)
        assert len(s.sent) == 1  # no recovery line
        _write_state(tmp, "running")
        csn.sweep_core_state_notices(tmp, set(), s, now=now + 6)
        assert len(s.sent) == 2 and "back online" in s.sent[1][1]


def test_kill_switch_env_disables_everything():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _write_state(tmp, "crashed")
        s = _Sender()
        os.environ["SPARROW_CORE_NOTICE"] = "0"
        try:
            csn.sweep_core_state_notices(tmp, {"!a:s"}, s)
        finally:
            del os.environ["SPARROW_CORE_NOTICE"]
        assert s.sent == []


def test_bodies_are_fixed_strings_never_file_content():
    # Rooms span tiers; the supervisor file carries core pane text. Nothing
    # from the file may reach a room body — only this module's fixed phrases.
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        marker = "SECRET-PANE-CONTENT"
        (tmp / csn.CORE_SUPERVISOR_FILE).write_text(json.dumps({
            "state": "blocked-human", "kind": "session-limit",
            "detail": marker, "prompt": marker}))
        s = _Sender()
        csn.sweep_core_state_notices(tmp, {"!a:s"}, s)
        assert len(s.sent) == 1 and marker not in s.sent[0][1]


def test_read_core_state_bounds_and_shapes():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _write_state(tmp, "x" * 1000, kind=123)
        state, kind = csn.read_core_state(tmp)
        assert len(state) == csn._FIELD_MAX and kind is None
        # Unlisted blocked-human kinds fall back to the generic sender-facing
        # reason (the owner escalation never reaches these rooms).
        assert csn.degraded_reason("blocked-human", None) == "blocked"
        assert csn.degraded_reason("blocked-human", "unknown") == "blocked"
        assert csn.degraded_reason("blocked-human", "permission") == "blocked"
        assert csn.degraded_reason("blocked-human", "login") == "logged-out"
        assert csn.degraded_reason("blocked-human", "fable-limit-unfocused") == "usage-limit"
        assert csn.degraded_reason("hung", None) == "hung"
        assert csn.degraded_reason("idle-ready", None) is None
        assert csn.degraded_reason("blocked-known", None) is None


def test_plan_commit_seam_for_async_callers():
    # The plan/commit split lets an await-based caller (discord) reuse the exact
    # ledger + cooldown logic: plan → send → commit only what succeeded.
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _write_state(tmp, "logged-out")
        now = time.time()
        plan = csn.plan_notices(tmp, {"!a:s", "!b:s"}, now=now)
        assert plan.kind == "degraded" and plan.reason == "logged-out"
        assert {r for r, _ in plan.items} == {"!a:s", "!b:s"}
        # simulate: !a delivered, !b failed → only !a is committed
        plan.commit(["!a:s"])
        # !a is now inside cooldown (no re-plan), !b is not (retry available)
        p2 = csn.plan_notices(tmp, {"!a:s", "!b:s"}, now=now + 1)
        assert {r for r, _ in p2.items} == {"!b:s"}
        p2.commit(["!b:s"])
        # recovery is owed to BOTH delivered rooms once healthy
        _write_state(tmp, "idle-ready")
        rec = csn.plan_notices(tmp, set(), now=now + 2)
        assert rec.kind == "recovery" and {r for r, _ in rec.items} == {"!a:s", "!b:s"}
        rec.commit(["!a:s", "!b:s"])
        assert csn.plan_notices(tmp, set(), now=now + 3) is None


def test_ledger_name_isolates_surfaces():
    # Two bridges share a state dir; each MUST use its own ledger file or they
    # corrupt each other and one surface's cooldown suppresses the other's.
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        _write_state(tmp, "crashed")
        now = time.time()
        # discord notices its room and commits to its own ledger
        pd = csn.plan_notices(tmp, {"123"}, now=now, ledger_name="nd.json")
        pd.commit(["123"])
        # slack, same room-id string, DIFFERENT ledger → not suppressed
        ps = csn.plan_notices(tmp, {"123"}, now=now + 1, ledger_name="ns.json")
        assert {r for r, _ in ps.items} == {"123"}
        # two distinct files exist; the gateway's default ledger is untouched
        assert (tmp / "nd.json").exists() and not (tmp / "ns.json").exists()
        assert not (tmp / csn.LEDGER_FILE).exists()


def test_suffix_names_the_surface():
    assert "gateway" in csn._degraded_body("crashed")
    assert csn._degraded_body("crashed", suffix=" _(x)_").endswith(" _(x)_")
    assert csn._recovery_body(suffix=" _(x)_").endswith(" _(x)_")


if __name__ == "__main__":
    test_watcher_heartbeat_gates_freshness()
    test_debounce_suppresses_login_restart_flap()
    test_generic_human_gate_still_tells_senders()
    test_hung_needs_a_long_hold_before_senders_hear()
    test_blocked_known_is_not_proof_of_recovery()
    test_absent_heartbeat_trust_is_age_bounded()
    test_invalid_heartbeat_is_no_verdict()
    test_recovery_validates_and_purges_targets()
    test_partial_success_committed_before_exception()
    test_recovery_persistence_failure_does_not_resurrect()
    test_unattempted_rooms_not_charged_a_failure()
    test_batch_cap_rotation_reaches_all_rooms()
    test_budget_truncation_does_not_starve()
    test_persistence_failure_keeps_process_local_cooldown()
    test_numeric_guards()
    test_no_supervisor_file_does_nothing()
    test_degraded_notices_once_per_room_with_cooldown()
    test_failed_send_burns_nothing_and_retries()
    test_reason_change_renotices_inside_cooldown()
    test_recovery_announced_once_and_flap_stays_bounded()
    test_alternating_reasons_bounded_per_reason()
    test_v1_ledger_resets_cleanly()
    test_old_mtime_is_still_a_verdict()
    test_no_verdict_inputs_do_nothing()
    test_unrecognized_state_does_not_fake_recovery()
    test_kill_switch_env_disables_everything()
    test_bodies_are_fixed_strings_never_file_content()
    test_read_core_state_bounds_and_shapes()
    test_plan_commit_seam_for_async_callers()
    test_ledger_name_isolates_surfaces()
    test_suffix_names_the_surface()
    print("ALL PASS")
