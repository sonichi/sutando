#!/usr/bin/env python3
"""
Tests for the core wedge auto-recovery (`recover_core_if_wedged`) in
src/health-check.py.

Motivated by the 2026-06-02 incident: the core crossed into 1M extended
context, hit the interactive `/usage-credits` gate (which cannot be
pre-authorized for an unattended agent), and looped silently — alive (heartbeat
ticking) but draining nothing. --notify-slack makes that visible; this makes it
self-healing by restarting the core via scripts/start-cli.sh --restart, with
1M preserved on the first attempt and a graceful 200K fallback if it recurs.

Because auto-restarting a 24/7 agent is consequential, the guards are the whole
point. These cover:
  a) healthy / no queued work        → no action, no restart, no DM
  b) wedged but core just booted     → no action (catching up, not stuck)
  c) wedged, first observation       → "observed", no restart (confirm window)
  d) wedged, within confirm window   → "confirming", no restart
  e) wedged + confirmed              → restart in 1M mode (keeps 1M), DM sent
  f) within cooldown after a restart → no second restart
  g) recurs after cooldown           → escalates to standard 200K context
  h) give-up cap (3/hr)              → DMs "gave up", stops restarting
  i) restart launch fails            → no cooldown/history burned, retries
  j) core down (not alive)           → no action even with an old task
  k) draining backlog (oldest task   → never restarts (queue is healthy, just
     differs each pass)                 busy) — review blocker 3
  l) core makes progress             → resets, never restarts a long live task;
     (core-status.json advances)        a FROZEN status with same task does fire
  m) concurrent invocation (lock     → second caller no-ops with "locked"
     held)                              — review suggestion
  n) restart DM fails                → still restarts, records dm_sent=False
                                        — review blocker 2

Run: python3 tests/health-check-recover-core.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

# Pin thresholds so the test is independent of any SUTANDO_RECOVER_* env override
# present in the runner's environment.
hc.RECOVER_WEDGE_SEC = 600
hc.RECOVER_CONFIRM_SEC = 120
hc.RECOVER_COOLDOWN_SEC = 1800
hc.RECOVER_MAX_PER_HOUR = 3


class Harness:
    """Drives recover_core_if_wedged with injected, recording collaborators."""

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.sent: list[str] = []
        self.restart_calls: list[bool] = []
        self.restart_ok = True
        self.send_ok = True

    def sender(self, text):
        self.sent.append(text)
        return self.send_ok

    def restart(self, standard_context):
        self.restart_calls.append(standard_context)
        return self.restart_ok

    def run(self, now, alive=True, age=900, key="t1", status_ts=None, booted=False):
        oldest = (key, age) if age is not None else None
        return hc.recover_core_if_wedged(
            state_file=self.state_file,
            now=now,
            alive_fn=lambda: alive,
            oldest_task_fn=lambda: oldest,
            status_ts_fn=lambda: status_ts,
            just_booted_fn=lambda: booted,
            restart_fn=self.restart,
            sender=self.sender,
        )


def case_a_healthy_no_action() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        r = h.run(now=1_000_000, alive=True, age=None)  # alive, empty queue
        if r is not None:
            fails.append(f"a) healthy (no queue) acted: {r}")
        if h.restart_calls or h.sent:
            fails.append("a) healthy triggered restart/DM")
    return fails


def case_b_just_booted_never_restarts() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        h.run(now=1_000_000, age=5000, booted=True)
        h.run(now=1_000_500, age=5000, booted=True)
        if h.restart_calls:
            fails.append("b) restarted a just-booted core")
    return fails


def case_c_first_observation_no_restart() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        r = h.run(now=1_000_000, age=900)
        if not r or r.get("action") != "observed":
            fails.append(f"c) first wedge should be 'observed', got {r}")
        if h.restart_calls or h.sent:
            fails.append("c) first observation restarted/DM'd prematurely")
    return fails


def case_d_within_confirm_window_no_restart() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        h.run(now=1_000_000, age=900)
        r = h.run(now=1_000_060, age=960)            # +60s < CONFIRM(120)
        if not r or r.get("action") != "confirming":
            fails.append(f"d) within confirm window should be 'confirming', got {r}")
        if h.restart_calls:
            fails.append("d) restarted within confirm window")
    return fails


def case_e_confirmed_restart_keeps_1m() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        h.run(now=1_000_000, age=900)
        r = h.run(now=1_000_200, age=900)            # +200s > CONFIRM → restart
        if not r or r.get("action") != "restarted":
            fails.append(f"e) confirmed wedge should restart, got {r}")
        if r and r.get("mode") != "1m":
            fails.append(f"e) first restart must keep 1M, mode={r.get('mode')}")
        if h.restart_calls != [False]:
            fails.append(f"e) first restart should pass standard_context=False, got {h.restart_calls}")
        if len(h.sent) != 1:
            fails.append(f"e) restart should DM owner once, sent {len(h.sent)}")
        if r and r.get("dm_sent") is not True:
            fails.append(f"e) successful DM should record dm_sent=True, got {r.get('dm_sent')}")
    return fails


def case_f_cooldown_blocks_second_restart() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        h.run(now=1_000_000, age=900)
        h.run(now=1_000_200, age=900)                # restart #1
        h.run(now=1_000_300, age=1000)               # observe (post-restart reset)
        r = h.run(now=1_000_500, age=1200)           # confirmed but within cooldown
        if r and r.get("action") == "restarted":
            fails.append("f) restarted again within cooldown window")
        if h.restart_calls != [False]:
            fails.append(f"f) cooldown should leave a single restart, got {h.restart_calls}")
    return fails


def case_g_recurrence_escalates_to_standard() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        h.run(now=1_000_000, age=900)
        h.run(now=1_000_200, age=900)                # restart #1 (1m)
        t2 = 1_000_200 + hc.RECOVER_COOLDOWN_SEC + 50
        h.run(now=t2, age=1500)                       # re-observe
        r = h.run(now=t2 + 200, age=1500)            # restart #2
        if not r or r.get("action") != "restarted":
            fails.append(f"g) recurrence should restart again, got {r}")
        if r and r.get("mode") != "standard":
            fails.append(f"g) 2nd restart must degrade to standard, mode={r.get('mode')}")
        if h.restart_calls != [False, True]:
            fails.append(f"g) escalation should be [1m=False, standard=True], got {h.restart_calls}")
    return fails


def case_h_give_up_cap() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "rec.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        h = Harness(sf)
        now = 2_000_000
        # Pre-seed 3 restarts within the trailing hour, cooldown already passed,
        # and a confirmed wedge observation on the SAME task the harness reports
        # ("t1") — the next action must be give-up.
        sf.write_text(json.dumps({
            "wedge_first_seen": now - 500,
            "wedge_task": "t1",
            "wedge_status_ts": None,
            "last_restart": now - hc.RECOVER_COOLDOWN_SEC - 10,
            "restart_history": [now - 3000, now - 2000, now - hc.RECOVER_COOLDOWN_SEC - 10],
            "last_restart_mode": "standard",
        }))
        r = h.run(now=now, age=1800)
        if not r or r.get("action") != "gave_up":
            fails.append(f"h) 4th restart in an hour should give up, got {r}")
        if h.restart_calls:
            fails.append("h) gave-up state still restarted")
        if len(h.sent) != 1 or "gave up" not in h.sent[0].lower():
            fails.append(f"h) give-up should DM once with a 'gave up' message, sent {h.sent}")
        # Dedup: a second pass in the same give-up episode must not re-DM.
        h.run(now=now + 60, age=1900)
        if len(h.sent) != 1:
            fails.append(f"h) give-up DM not deduped, sent {len(h.sent)}")
    return fails


def case_i_failed_restart_does_not_burn_state() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "rec.json"
        h = Harness(sf)
        h.restart_ok = False
        h.run(now=1_000_000, age=900)
        r = h.run(now=1_000_200, age=900)            # confirmed → restart attempt FAILS
        if not r or r.get("action") != "restart_failed":
            fails.append(f"i) failed restart should report 'restart_failed', got {r}")
        st = json.loads(sf.read_text())
        if st.get("last_restart"):
            fails.append("i) failed restart recorded a cooldown timestamp")
        if st.get("restart_history"):
            fails.append("i) failed restart recorded history (would count toward give-up)")
        if not st.get("wedge_first_seen"):
            fails.append("i) failed restart cleared the confirmation, would re-delay retry")
        h.restart_ok = True
        r2 = h.run(now=1_000_400, age=950)
        if not r2 or r2.get("action") != "restarted":
            fails.append(f"i) retry after failed restart did not restart, got {r2}")
    return fails


def case_j_core_down_no_action() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        r = h.run(now=1_000_000, alive=False, age=5000)
        if r is not None or h.restart_calls:
            fails.append(f"j) acted on a dead core: {r}, restarts={h.restart_calls}")
    return fails


def case_k_draining_backlog_never_restarts() -> list[str]:
    """A busy-but-healthy core surfaces a DIFFERENT oldest task each pass as it
    drains the queue. The identity check must reset the window every time, so
    the confirm window never completes and no restart fires (review blocker 3)."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        actions = [
            h.run(now=1_000_000, age=900, key="taskA"),
            h.run(now=1_000_200, age=900, key="taskB"),
            h.run(now=1_000_400, age=900, key="taskC"),
            h.run(now=1_000_600, age=900, key="taskD"),
        ]
        if h.restart_calls:
            fails.append(f"k) restarted a draining (healthy) backlog: {h.restart_calls}")
        if any(a is None or a.get("action") != "observed" for a in actions):
            fails.append(f"k) draining backlog should stay 'observed', got {[a and a.get('action') for a in actions]}")
    return fails


def case_l_progress_resets_long_task() -> list[str]:
    """Same oldest task across passes, but core-status.json advances → the core
    is making progress on a long task, not wedged → reset, no restart. A FROZEN
    status (same task, status unchanged) DOES restart — proving it's the
    progress signal, not mere status presence, that protects the task."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        h.run(now=1_000_000, age=900, key="t1", status_ts=1000)
        r = h.run(now=1_000_200, age=960, key="t1", status_ts=1100)  # advanced
        if not r or r.get("action") != "observed":
            fails.append(f"l) advancing status should reset to 'observed', got {r}")
        if h.restart_calls:
            fails.append("l) restarted a core that is making progress")
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        h.run(now=2_000_000, age=900, key="t1", status_ts=1000)
        r = h.run(now=2_000_200, age=960, key="t1", status_ts=1000)  # frozen → wedged
        if not r or r.get("action") != "restarted":
            fails.append(f"l) frozen status with same stuck task should restart, got {r}")
    return fails


def case_m_lock_prevents_concurrent_restart() -> list[str]:
    """A second concurrent invocation, while another holds the recovery lock,
    must no-op with 'locked' (review suggestion — no double-restart)."""
    if hc.fcntl is None:
        return []  # no POSIX locking on this platform; lock degrades to no-op
    import fcntl
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "rec.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        lock_path = sf.with_name(sf.name + ".lock")
        holder = open(lock_path, "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            h = Harness(sf)
            r = h.run(now=1_000_000, age=900)
            if r != {"action": "locked"}:
                fails.append(f"m) concurrent call should be 'locked', got {r}")
            if h.restart_calls:
                fails.append("m) concurrent call restarted despite held lock")
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
    return fails


def case_n_failed_dm_still_restarts_and_records() -> list[str]:
    """If the wedge-restart DM fails, recovery still restarts (recovery >
    notification) but records dm_sent=False so the restart isn't invisible
    (review blocker 2)."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "rec.json"
        h = Harness(sf)
        h.send_ok = False
        h.run(now=1_000_000, age=900)
        r = h.run(now=1_000_200, age=900)
        if not r or r.get("action") != "restarted":
            fails.append(f"n) should still restart when DM fails, got {r}")
        if r and r.get("dm_sent") is not False:
            fails.append(f"n) failed DM should record dm_sent=False, got {r.get('dm_sent')}")
        if h.restart_calls != [False]:
            fails.append(f"n) should have restarted once, got {h.restart_calls}")
        st = json.loads(sf.read_text())
        if st.get("last_restart_dm_sent") is not False:
            fails.append(f"n) state should record last_restart_dm_sent=False, got {st.get('last_restart_dm_sent')}")
    return fails


def main() -> int:
    cases = [
        ("a", case_a_healthy_no_action),
        ("b", case_b_just_booted_never_restarts),
        ("c", case_c_first_observation_no_restart),
        ("d", case_d_within_confirm_window_no_restart),
        ("e", case_e_confirmed_restart_keeps_1m),
        ("f", case_f_cooldown_blocks_second_restart),
        ("g", case_g_recurrence_escalates_to_standard),
        ("h", case_h_give_up_cap),
        ("i", case_i_failed_restart_does_not_burn_state),
        ("j", case_j_core_down_no_action),
        ("k", case_k_draining_backlog_never_restarts),
        ("l", case_l_progress_resets_long_task),
        ("m", case_m_lock_prevents_concurrent_restart),
        ("n", case_n_failed_dm_still_restarts_and_records),
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
    print("\nAll core-recovery invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
