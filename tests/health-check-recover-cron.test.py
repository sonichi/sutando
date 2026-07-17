#!/usr/bin/env python3
"""
Tests for the cron-layer death recovery (`recover_cron_if_dead`) in
src/health-check.py.

Motivated by the 2026-07-17 incident: the core session was ALIVE (23 days,
fresh heartbeat) but its IN-SESSION cron layer was dead — session crons are
registered per-session via CronCreate and auto-expire after 7 days, so the
long-lived core silently outlived its own crons. Scheduled work (the 6:04am
report, the morning briefing, the */5 main loop) stopped firing while every
liveness probe read healthy. "Core was not dead. Cron was dead."

Detection: fresh core heartbeat + core-status.json `ts` (stamped by every
main-loop pass) frozen beyond CRON_STALE_SEC. Recovery: type
`/schedule-crons` into the live core's tmux pane — a NUDGE, never a restart.

Covered invariants:
  a) fresh cron stamp                 → no action, no nudge, no DM
  b) stale stamp + alive core         → observed → confirmed → NUDGED once,
                                        DM wording says the CRON layer died,
                                        never that the core died
  c) core DOWN (no heartbeat)         → no action — that is the dead-core
                                        relaunch branch (PR #2160), not this
  d) cooldown after a nudge           → no second nudge inside the window
  e) core just booted                 → no action (hasn't had a main-loop
                                        period to stamp yet)
  f) stamp advances mid-confirm       → resets to observed, never nudges
  g) give-up cap (3/hr)               → DMs "gave up" once, stops nudging
  h) no stamp ever written            → no action (new install ≠ death)
  i) nudge launch fails               → no cooldown/history burned, retries
  j) concurrent invocation (lock)     → second caller no-ops with "locked"
  k) END-TO-END failure-mode exercise → REAL files in a temp workspace: fresh
     `.alive` mtime + stale core-status.json ts → nudged via the default
     detection fns; freshening the stamp file flips it back to no-action

Run: python3 tests/health-check-recover-cron.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

# Pin thresholds so the test is independent of any SUTANDO_* env override
# present in the runner's environment.
hc.CRON_STALE_SEC = 1800
hc.RECOVER_CONFIRM_SEC = 120
hc.RECOVER_COOLDOWN_SEC = 1800
hc.RECOVER_MAX_PER_HOUR = 3


class Harness:
    """Drives recover_cron_if_dead with injected, recording collaborators."""

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.sent: list[str] = []
        self.nudges = 0
        self.nudge_ok = True
        self.send_ok = True

    def sender(self, text):
        self.sent.append(text)
        return self.send_ok

    def nudge(self):
        self.nudges += 1
        return self.nudge_ok

    def run(self, now, alive=True, status_ts=None, booted=False):
        return hc.recover_cron_if_dead(
            state_file=self.state_file,
            now=now,
            alive_fn=lambda: alive,
            status_ts_fn=lambda: status_ts,
            just_booted_fn=lambda: booted,
            nudge_fn=self.nudge,
            sender=self.sender,
        )


def case_a_fresh_stamp_no_action() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "cron.json")
        # stamp 60s old — well inside CRON_STALE_SEC
        r = h.run(now=1_000_000, alive=True, status_ts=1_000_000 - 60)
        if r is not None:
            fails.append(f"a) fresh stamp acted: {r}")
        if h.nudges or h.sent:
            fails.append("a) fresh stamp triggered nudge/DM")
    return fails


def case_b_stale_stamp_alive_core_nudges() -> list[str]:
    """The incident shape: core alive, stamp frozen for an hour. First pass
    observes; a pass past the confirm window nudges ONCE and DMs with wording
    that blames the cron layer, not the core."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "cron.json")
        frozen = 1_000_000 - 3600
        r0 = h.run(now=1_000_000, alive=True, status_ts=frozen)
        if not r0 or r0.get("action") != "observed":
            fails.append(f"b) first stale pass should observe, got {r0}")
        if h.nudges:
            fails.append("b) nudged on first observation (no confirm window)")
        r1 = h.run(now=1_000_060, alive=True, status_ts=frozen)  # +60s < CONFIRM
        if not r1 or r1.get("action") != "confirming":
            fails.append(f"b) within confirm window should be 'confirming', got {r1}")
        r2 = h.run(now=1_000_200, alive=True, status_ts=frozen)  # +200s > CONFIRM
        if not r2 or r2.get("action") != "nudged":
            fails.append(f"b) confirmed cron-death should nudge, got {r2}")
        if h.nudges != 1:
            fails.append(f"b) expected exactly one nudge, got {h.nudges}")
        if len(h.sent) != 1:
            fails.append(f"b) nudge should DM owner once, sent {len(h.sent)}")
        if h.sent:
            msg = h.sent[0].lower()
            if "cron layer" not in msg:
                fails.append(f"b) DM must say the CRON layer died, got: {h.sent[0]}")
            if "core is down" in msg or "core died" in msg:
                fails.append(f"b) DM must NOT claim the core died, got: {h.sent[0]}")
        if r2 and r2.get("dm_sent") is not True:
            fails.append(f"b) successful DM should record dm_sent=True, got {r2.get('dm_sent')}")
    return fails


def case_c_dead_core_not_this_path() -> list[str]:
    """A core with NO fresh heartbeat is the dead-core relaunch's branch
    (PR #2160) — this path must not act, however stale the stamp."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "cron.json")
        r = h.run(now=1_000_000, alive=False, status_ts=1_000_000 - 86400)
        if r is not None or h.nudges or h.sent:
            fails.append(f"c) acted on a dead core: {r}, nudges={h.nudges}")
    return fails


def case_d_cooldown_blocks_second_nudge() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "cron.json")
        frozen = 1_000_000 - 3600
        h.run(now=1_000_000, status_ts=frozen)
        h.run(now=1_000_200, status_ts=frozen)               # nudge #1
        h.run(now=1_000_300, status_ts=frozen)               # re-observe (post-nudge reset)
        r = h.run(now=1_000_500, status_ts=frozen)           # confirmed but within cooldown
        if not r or r.get("action") != "cooldown":
            fails.append(f"d) inside cooldown should report 'cooldown', got {r}")
        if h.nudges != 1:
            fails.append(f"d) cooldown should leave a single nudge, got {h.nudges}")
    return fails


def case_e_just_booted_no_action() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "cron.json")
        r = h.run(now=1_000_000, status_ts=1_000_000 - 7200, booted=True)
        if r is not None or h.nudges:
            fails.append(f"e) acted on a just-booted core: {r}, nudges={h.nudges}")
    return fails


def case_f_advancing_stamp_resets() -> list[str]:
    """Stamp advanced between passes (still older than the threshold, e.g. a
    slow drain) → something is stamping again → reset to observed, no nudge."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "cron.json")
        h.run(now=1_000_000, status_ts=1_000_000 - 3600)
        r = h.run(now=1_000_200, status_ts=1_000_200 - 3000)  # advanced by 800
        if not r or r.get("action") != "observed":
            fails.append(f"f) advancing stamp should reset to 'observed', got {r}")
        if h.nudges:
            fails.append("f) nudged a cron layer that is stamping again")
    return fails


def case_g_give_up_cap() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "cron.json"
        h = Harness(sf)
        now = 2_000_000
        frozen = now - 7200
        # Pre-seed 3 nudges within the trailing hour, cooldown passed, and a
        # confirmed observation on the SAME frozen stamp — next action = give up.
        sf.write_text(json.dumps({
            "cron_first_seen": now - 500,
            "cron_status_ts": frozen,
            "last_nudge": now - hc.RECOVER_COOLDOWN_SEC - 10,
            "nudge_history": [now - 3000, now - 2000, now - hc.RECOVER_COOLDOWN_SEC - 10],
        }))
        r = h.run(now=now, status_ts=frozen)
        if not r or r.get("action") != "gave_up":
            fails.append(f"g) 4th nudge in an hour should give up, got {r}")
        if h.nudges:
            fails.append("g) gave-up state still nudged")
        if len(h.sent) != 1 or "gave up" not in h.sent[0].lower():
            fails.append(f"g) give-up should DM once with a 'gave up' message, sent {h.sent}")
        h.run(now=now + 60, status_ts=frozen)
        if len(h.sent) != 1:
            fails.append(f"g) give-up DM not deduped, sent {len(h.sent)}")
    return fails


def case_h_no_stamp_no_action() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "cron.json")
        r = h.run(now=1_000_000, status_ts=None)
        if r is not None or h.nudges:
            fails.append(f"h) acted with no stamp ever written: {r}")
    return fails


def case_i_failed_nudge_does_not_burn_state() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "cron.json"
        h = Harness(sf)
        h.nudge_ok = False
        frozen = 1_000_000 - 3600
        h.run(now=1_000_000, status_ts=frozen)
        r = h.run(now=1_000_200, status_ts=frozen)            # confirmed → nudge FAILS
        if not r or r.get("action") != "nudge_failed":
            fails.append(f"i) failed nudge should report 'nudge_failed', got {r}")
        st = json.loads(sf.read_text())
        if st.get("last_nudge"):
            fails.append("i) failed nudge recorded a cooldown timestamp")
        if st.get("nudge_history"):
            fails.append("i) failed nudge recorded history (would count toward give-up)")
        if not st.get("cron_first_seen"):
            fails.append("i) failed nudge cleared the confirmation, would re-delay retry")
        h.nudge_ok = True
        r2 = h.run(now=1_000_400, status_ts=frozen)
        if not r2 or r2.get("action") != "nudged":
            fails.append(f"i) retry after failed nudge did not nudge, got {r2}")
    return fails


def case_j_lock_prevents_concurrent_nudge() -> list[str]:
    if hc.fcntl is None:
        return []  # no POSIX locking on this platform; lock degrades to no-op
    import fcntl
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "cron.json"
        lock_path = sf.with_name(sf.name + ".lock")
        holder = open(lock_path, "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            h = Harness(sf)
            r = h.run(now=1_000_000, status_ts=1_000_000 - 3600)
            if r != {"action": "locked"}:
                fails.append(f"j) concurrent call should be 'locked', got {r}")
            if h.nudges:
                fails.append("j) concurrent call nudged despite held lock")
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
    return fails


def case_k_end_to_end_failure_mode() -> list[str]:
    """Exercise the ACTUAL failure mode against real files, using the DEFAULT
    detection collaborators (_any_core_alive, _core_status_ts,
    _core_started_within): a temp workspace holding a FRESH `.alive` heartbeat
    (mtime = now, started_at = 23 days ago — the incident's long-lived core)
    and a core-status.json whose ts froze an hour ago. Only the nudge + DM are
    injected. Must nudge. Then freshen the stamp → must go quiet."""
    fails = []
    saved_ws = hc.WORKSPACE_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            hc.WORKSPACE_DIR = ws
            now = time.time()
            cores = ws / "state" / "cores"
            cores.mkdir(parents=True)
            alive = cores / "testhost.alive"
            alive.write_text(json.dumps({
                "host": "testhost", "pid": 12345,
                "started_at": now - 23 * 86400,  # the 23-day core
                "last_beat_at": now, "status": "ok",
                "socket": "/tmp/sutando-test.sock", "schema_version": 1,
            }))
            os.utime(alive, (now, now))  # fresh heartbeat mtime → core ALIVE
            status = ws / "state" / "core-status.json"
            status.write_text(json.dumps({
                "status": "idle", "ts": now - 3600,  # stamp frozen an hour ago
            }))

            nudges = []
            sent = []
            kw = dict(
                state_file=ws / "state" / "cron-recovery.json",
                nudge_fn=lambda: nudges.append(1) or True,
                sender=lambda t: sent.append(t) or True,
            )
            r0 = hc.recover_cron_if_dead(now=now, **kw)
            if not r0 or r0.get("action") != "observed":
                fails.append(f"k) real stale stamp + fresh .alive should observe, got {r0}")
            r1 = hc.recover_cron_if_dead(now=now + hc.RECOVER_CONFIRM_SEC + 10, **kw)
            if not r1 or r1.get("action") != "nudged":
                fails.append(f"k) confirmed real cron-death should nudge, got {r1}")
            if len(nudges) != 1:
                fails.append(f"k) expected one nudge, got {len(nudges)}")

            # The socket resolver must surface the heartbeat's runtime-authored
            # socket (what the real nudge would use).
            sock = hc._live_core_socket(ws)
            if sock != "/tmp/sutando-test.sock":
                fails.append(f"k) _live_core_socket should read the heartbeat socket, got {sock}")

            # Freshen the stamp (cron fired again) → detection must go quiet.
            status.write_text(json.dumps({"status": "idle", "ts": now + 200}))
            r2 = hc.recover_cron_if_dead(now=now + 300, **kw)
            if r2 is not None:
                fails.append(f"k) fresh stamp should be quiet, got {r2}")
            if len(nudges) != 1:
                fails.append(f"k) fresh stamp still nudged, total {len(nudges)}")
    finally:
        hc.WORKSPACE_DIR = saved_ws
    return fails


def main() -> int:
    cases = [
        ("a", case_a_fresh_stamp_no_action),
        ("b", case_b_stale_stamp_alive_core_nudges),
        ("c", case_c_dead_core_not_this_path),
        ("d", case_d_cooldown_blocks_second_nudge),
        ("e", case_e_just_booted_no_action),
        ("f", case_f_advancing_stamp_resets),
        ("g", case_g_give_up_cap),
        ("h", case_h_no_stamp_no_action),
        ("i", case_i_failed_nudge_does_not_burn_state),
        ("j", case_j_lock_prevents_concurrent_nudge),
        ("k", case_k_end_to_end_failure_mode),
    ]
    all_failures = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            all_failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    print("\nAll cron-liveness recovery invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
