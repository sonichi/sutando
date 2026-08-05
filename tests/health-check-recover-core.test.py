#!/usr/bin/env python3
"""
Tests for the core wedge auto-recovery (`recover_core_if_wedged`) in
src/health-check.py.

Motivated by the 2026-06-02 incident: the core crossed into 1M extended
context, hit the interactive `/usage-credits` gate (which cannot be
pre-authorized for an unattended agent), and looped silently — alive (heartbeat
ticking) but draining nothing. --notify-slack makes that visible; this makes it
self-healing by restarting the core via src/agent/start-cli.sh --restart, with
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
  j) dead core (no heartbeat, not booted) → relaunched (session-died gap)
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


def case_j_dead_core_relaunches() -> list[str]:
    """A fully-dead core (no heartbeat, not just-booted) IS relaunched — the
    'session died and took its in-session crons/dailies' case (owner-requested
    2026-07-17). Observed on the first pass, restarted after the confirm window.
    A dead core that JUST booted is NOT touched (coming up, not gone)."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        r0 = h.run(now=1_000_000, alive=False, age=5000)     # dead → observe
        if not r0 or r0.get("action") != "observed":
            fails.append(f"j) first pass on a dead core should observe, got {r0}")
        r1 = h.run(now=1_000_200, alive=False, age=5000)     # +200s > CONFIRM → relaunch
        if not r1 or r1.get("action") != "restarted":
            fails.append(f"j) confirmed-dead core should relaunch, got {r1}")
        if h.restart_calls != [False]:
            fails.append(f"j) dead relaunch should call restart once (1M), got {h.restart_calls}")
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        r = h.run(now=1_000_000, alive=False, age=5000, booted=True)  # dead but just booted
        if r is not None or h.restart_calls:
            fails.append(f"j) a just-booted core must NOT be relaunched: {r}, {h.restart_calls}")
    return fails


def case_j2_dead_core_before_after_output() -> list[str]:
    """BEFORE/AFTER health-check output for the dead-core relaunch (CR #2160):
    run the SAME recover-core check in two states and capture the action + the
    DM the owner would see.
      BEFORE (healthy, non-wedged core): no action, no restart, no DM.
      AFTER  (a confirmed-dead core):    'restarted' + the skull 'core is down /
                                          Auto-relaunching' DM, restart called once.
    """
    fails = []
    # BEFORE — a healthy core with only fresh work: nothing happens (no output).
    with tempfile.TemporaryDirectory() as td:
        hb = Harness(Path(td) / "rec.json")
        rb = hb.run(now=1_000_000, alive=True, age=10)      # alive + fresh task → healthy
        print("  BEFORE (healthy core): action=%r restart_calls=%r DM=%r"
              % ((rb or {}).get("action"), hb.restart_calls, hb.sent))
        if hb.restart_calls or hb.sent:
            fails.append(f"j2) healthy core should produce no restart/DM, got {hb.restart_calls}/{hb.sent}")
    # AFTER — a confirmed-dead core: relaunch once + the skull DM.
    with tempfile.TemporaryDirectory() as td:
        ha = Harness(Path(td) / "rec.json")
        ha.run(now=1_000_000, alive=False, age=5000)         # first pass → observe
        ra = ha.run(now=1_000_200, alive=False, age=5000)    # +200s > CONFIRM → relaunch
        print("  AFTER  (dead core):    action=%r restart_calls=%r DM=%r"
              % ((ra or {}).get("action"), ha.restart_calls, ha.sent))
        if (ra or {}).get("action") != "restarted" or ha.restart_calls != [False]:
            fails.append(f"j2) dead core should relaunch once, got {ra}/{ha.restart_calls}")
        dm = " ".join(ha.sent).lower()
        if not ("down" in dm and "relaunch" in dm):
            fails.append(f"j2) dead relaunch should DM 'core is down / relaunching', got {ha.sent}")
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


def case_o_launch_env_path() -> list[str]:
    """Regression for the 2026-07-10 launchd restart bug: under launchd's minimal
    PATH, start-cli.sh --restart failed rc=127 (node/claude not found).
    _resolve_launch_env must PREPEND homebrew, ~/.local/bin, and (when present)
    the bundled runtime so the tools resolve."""
    import os
    fails = []
    saved = os.environ.get("PATH")
    try:
        os.environ["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"  # launchd minimal
        env = hc._resolve_launch_env()
        parts = env["PATH"].split(":")
        for needed in ("/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin")):
            if needed not in parts:
                fails.append(f"o) resolved PATH missing {needed}")
        if "/opt/homebrew/bin" in parts and "/usr/bin" in parts:
            if parts.index("/opt/homebrew/bin") > parts.index("/usr/bin"):
                fails.append("o) healed dirs must be PREPENDED (before the minimal PATH)")
    finally:
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved
    return fails


def case_p_wedged_to_dead_transition_reobserves() -> list[str]:
    """A wedged→dead transition on the SAME oldest task must RE-OBSERVE (start a
    fresh confirm window for the dead condition) — NOT inherit the wedge's
    already-elapsed window and relaunch on the first dead pass. Regression for
    the mode-unaware confirm-window reuse found reviewing #2160: dead and wedged
    share wedge_first_seen/wedge_task, and without a mode check a core that is
    wedged-and-confirming, then dies with the same oldest task, would restart
    immediately though the DEAD condition was never confirmed across a pass."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        h.run(now=1_000_000, alive=True, age=900, key="t1")            # wedged → observe
        r_conf = h.run(now=1_000_100, alive=True, age=1000, key="t1")  # +100 < CONFIRM(120) → confirming
        if not r_conf or r_conf.get("action") != "confirming":
            fails.append(f"p) wedge should be confirming at +100s, got {r_conf}")
        # Core DIES, SAME oldest task, at +200s (> CONFIRM measured from the wedge observe).
        # Pre-fix this restarts (inherits the wedge window); post-fix it re-observes.
        r_flip = h.run(now=1_000_200, alive=False, age=1000, key="t1")
        if not r_flip or r_flip.get("action") != "observed":
            fails.append(f"p) wedged→dead flip must RE-OBSERVE (fresh window), got {r_flip}")
        if h.restart_calls:
            fails.append(f"p) must NOT relaunch on the first dead pass after a wedge, got {h.restart_calls}")
        # Dead now persists and confirms on ITS OWN window → relaunch.
        r_dead = h.run(now=1_000_400, alive=False, age=1000, key="t1")  # +200s from the dead observe
        if not r_dead or r_dead.get("action") != "restarted":
            fails.append(f"p) confirmed-dead (own window) should relaunch, got {r_dead}")
        if h.restart_calls != [False]:
            fails.append(f"p) dead relaunch should call restart once (1M), got {h.restart_calls}")
    return fails


def case_q_preupgrade_state_dead_reobserves() -> list[str]:
    """Persisted-state migration regression (Qingyun review, #2160): a PRE-UPGRADE
    state file has wedge_first_seen/wedge_task but NO wedge_mode. The previous head
    only ever observed WEDGES, so that absent mode is an implied wedge window. If
    the core is DEAD on the first post-upgrade pass with the same oldest task and
    the old window has already elapsed, the death must be RE-OBSERVED on its own
    window — NOT restarted immediately by inheriting the elapsed wedge window.
    Direct repro of the seed Qingyun reported returning action='restarted'."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "rec.json"
        h = Harness(sf)
        # Pre-upgrade format: no wedge_mode. Window opened at 1_000_000; elapsed by
        # 1_000_200 (> CONFIRM 120). Core is DEAD, same oldest task "t1".
        sf.write_text(json.dumps({
            "wedge_first_seen": 1_000_000,
            "wedge_task": "t1",
            "wedge_status_ts": None,
        }))
        r = h.run(now=1_000_200, alive=False, age=1000, key="t1")
        if not r or r.get("action") != "observed":
            fails.append(f"q) pre-upgrade state + dead must RE-OBSERVE, got {r}")
        if h.restart_calls:
            fails.append(f"q) must NOT restart on the first post-upgrade dead pass, got {h.restart_calls}")
        st = json.loads(sf.read_text())
        if st.get("wedge_mode") != "dead":
            fails.append(f"q) re-observe should backfill wedge_mode='dead', got {st.get('wedge_mode')}")
        if st.get("wedge_first_seen") != 1_000_200:
            fails.append(f"q) re-observe should reset the window to now, got {st.get('wedge_first_seen')}")
        # Death now persists and confirms on ITS OWN window → relaunch.
        r2 = h.run(now=1_000_400, alive=False, age=1000, key="t1")   # +200s from the dead observe
        if not r2 or r2.get("action") != "restarted":
            fails.append(f"q) confirmed-dead (own window) should relaunch, got {r2}")
    return fails


def case_r_local_liveness_ignores_peer_heartbeats():
    """qingyun-wu's P1 (#2160): a PEER heartbeat must not suppress a local relaunch.

    The dead-core actuator defaulted to `_any_core_alive`, whose contract is
    explicitly "any host" and which globs every `state/cores/*.alive`. The
    workspace is SHARED, so on a multi-core setup one fresh peer record made a
    dead local host look alive forever and the relaunch never fired.

    Four states, because two of them are what stop the fix becoming "never
    believe a heartbeat": a fresh LOCAL beat must still read alive, and a stale
    one must not.
    """
    import os
    import pathlib
    import tempfile
    import time as _t
    fails = []
    ws = pathlib.Path(tempfile.mkdtemp())
    (ws / "state" / "cores").mkdir(parents=True)
    (ws / "state" / "cores" / "peer-host.alive").write_text("{}")

    if not hc._any_core_alive(ws):
        fails.append("r) _any_core_alive should still see the peer — that contract is fleet-wide")
    if hc._local_core_alive(ws):
        fails.append("r) a PEER heartbeat made the local core look alive (the P1)")

    sys.path.insert(0, str(pathlib.Path(hc.__file__).resolve().parent))
    from util_paths import _host_label
    local = ws / "state" / "cores" / f"{_host_label()}.alive"
    local.write_text("{}")
    if not hc._local_core_alive(ws):
        fails.append("r) a fresh LOCAL heartbeat must read alive")

    stale = _t.time() - 600
    os.utime(local, (stale, stale))
    if hc._local_core_alive(ws):
        fails.append("r) a STALE local heartbeat must not read alive")

    # and the actuator must actually relaunch when only a peer is beating
    (ws / "state" / "cores" / "peer-host.alive").write_text("{}")
    if hc._local_core_alive(ws):
        fails.append("r) local liveness still true after refreshing only the peer")
    return fails


def case_s_actuator_DEFAULT_alive_fn_is_local_not_fleet():
    """Pin the WIRING, not just the function.

    Case r proves `_local_core_alive` is correct. It does NOT prove the actuator
    USES it: `Harness` always injects `alive_fn`, so the default is never
    exercised. I verified that by reverting `alive_fn = alive_fn or
    _local_core_alive` back to `_any_core_alive` — case r still passed. A test
    that cannot fail in the broken state proves nothing about it.

    So this one calls the actuator with NO injected alive_fn, against a
    workspace where only a PEER is beating. With the fleet-wide default the core
    reads alive and nothing is observed; with the local default it observes.
    """
    import os
    import pathlib
    import tempfile
    fails = []
    ws = pathlib.Path(tempfile.mkdtemp())
    (ws / "state" / "cores").mkdir(parents=True)
    (ws / "state" / "cores" / "peer-host.alive").write_text("{}")   # PEER only

    orig = hc.WORKSPACE_DIR
    hc.WORKSPACE_DIR = ws
    try:
        r = hc.recover_core_if_wedged(
            state_file=ws / "rec.json",
            now=1_000_000,
            # alive_fn deliberately NOT injected — this is the point of the case
            oldest_task_fn=lambda: ("t1", 5000),
            status_ts_fn=lambda: None,
            just_booted_fn=lambda: False,
            restart_fn=lambda standard_context: True,
            sender=lambda text: True,
        )
    finally:
        hc.WORKSPACE_DIR = orig

    # Assert on wedge_mode, NOT on action. Both modes return "observed" — the
    # fleet-wide default reaches it via the WEDGE path (alive but stuck) and the
    # local default via the DEAD path. An action-only assertion passes under
    # both, which is exactly how my first draft of this case failed to pin
    # anything; the revert control caught it.
    import json as _json
    st = _json.loads((ws / "rec.json").read_text()) if (ws / "rec.json").exists() else {}
    if st.get("wedge_mode") != "dead":
        fails.append(
            "s) with only a PEER heartbeat the local core is DEAD, but the "
            f"actuator recorded wedge_mode={st.get('wedge_mode')!r} — the "
            "default alive_fn is fleet-wide, not local")
    if r is None or r.get("action") != "observed":
        fails.append(f"s) expected an observation, got {r}")
    return fails


def case_t_unresolvable_host_label_reads_NOT_alive():
    """The fail-safe direction, which is the whole reason this branch exists.

    `_local_core_alive` resolves this host's name through
    `util_paths._host_label()`. If that cannot be resolved, the function must
    return False — NOT alive — so an unidentifiable host produces an extra
    observation rather than a SUPPRESSED relaunch. Failing the other way would
    silently disable core recovery on exactly the hosts whose identity is
    already broken.

    Reachable in practice: `util_paths` is imported inside the function, so an
    import error, a renamed helper, or a raising label resolver all land here.
    """
    import pathlib
    import sys as _sys
    import tempfile
    fails = []
    ws = pathlib.Path(tempfile.mkdtemp())
    (ws / "state" / "cores").mkdir(parents=True)

    _sys.path.insert(0, str(pathlib.Path(hc.__file__).resolve().parent))
    import util_paths
    original = util_paths._host_label

    # a fresh heartbeat for THIS host exists — so a False result can only come
    # from the label failing, never from a missing file
    (ws / "state" / "cores" / f"{original()}.alive").write_text("{}")
    if not hc._local_core_alive(ws):
        fails.append("t) precondition: a fresh local heartbeat should read alive")

    def _boom():
        raise RuntimeError("cannot resolve host label")

    util_paths._host_label = _boom
    try:
        if hc._local_core_alive(ws):
            fails.append(
                "t) an unresolvable host label read ALIVE — that suppresses the "
                "relaunch on a host whose identity is already broken")
    finally:
        util_paths._host_label = original
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
        ("j", case_j_dead_core_relaunches),
        ("r", case_r_local_liveness_ignores_peer_heartbeats),
        ("s", case_s_actuator_DEFAULT_alive_fn_is_local_not_fleet),
        ("t", case_t_unresolvable_host_label_reads_NOT_alive),
        ("j2", case_j2_dead_core_before_after_output),
        ("k", case_k_draining_backlog_never_restarts),
        ("l", case_l_progress_resets_long_task),
        ("m", case_m_lock_prevents_concurrent_restart),
        ("n", case_n_failed_dm_still_restarts_and_records),
        ("o", case_o_launch_env_path),
        ("p", case_p_wedged_to_dead_transition_reobserves),
        ("q", case_q_preupgrade_state_dead_reobserves),
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
