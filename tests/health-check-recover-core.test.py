#!/usr/bin/env python3
"""Tests for the core wedge auto-recovery (`recover_core_if_wedged`) in"""

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

    def restart(self):
        self.restart_calls.append(True)
        return self.restart_ok

    def run(self, now, alive=True, age=900, key="t1", status_ts=None, booted=False, stopped=False):
        oldest = (key, age) if age is not None else None
        return hc.recover_core_if_wedged(
            state_file=self.state_file,
            now=now,
            alive_fn=lambda: alive,
            oldest_task_fn=lambda: oldest,
            status_ts_fn=lambda: status_ts,
            just_booted_fn=lambda: booted,
            restart_fn=self.restart,
            stopped_fn=lambda: stopped,
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
        if r and "mode" in r:
            fails.append(f"e) the retired escalation must not report a mode, got {r.get('mode')}")
        if h.restart_calls != [True]:
            fails.append(f"e) restart_fn must be called with NO argument, got {h.restart_calls}")
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
        if h.restart_calls != [True]:
            fails.append(f"f) cooldown should leave a single restart, got {h.restart_calls}")
    return fails


def case_g_recurrence_does_not_downgrade_the_model() -> list[str]:
    """A repeat wedge restarts on the configured model — no downgrade, and the
    DM must not promise one."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        h.run(now=1_000_000, age=900)
        h.run(now=1_000_200, age=900)                # restart #1
        t2 = 1_000_200 + hc.RECOVER_COOLDOWN_SEC + 50
        h.run(now=t2, age=1500)                       # re-observe
        r = h.run(now=t2 + 200, age=1500)            # restart #2
        if not r or r.get("action") != "restarted":
            fails.append(f"g) recurrence should restart again, got {r}")
        if r and "mode" in r:
            fails.append(f"g) a repeat wedge must not report a downgrade mode, got {r.get('mode')}")
        if h.restart_calls != [True, True]:
            fails.append(f"g) both restarts must be plain no-arg calls, got {h.restart_calls}")
        if any("200K" in s or "didn't hold" in s for s in h.sent):
            fails.append(f"g) the DM must not promise a context downgrade: {h.sent}")
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
    """A fully-dead core IS relaunched: observed on the first pass, restarted
    after the confirm window. A dead core that JUST booted is NOT touched."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        r0 = h.run(now=1_000_000, alive=False, age=5000)     # dead → observe
        if not r0 or r0.get("action") != "observed":
            fails.append(f"j) first pass on a dead core should observe, got {r0}")
        r1 = h.run(now=1_000_200, alive=False, age=5000)     # +200s > CONFIRM → relaunch
        if not r1 or r1.get("action") != "restarted":
            fails.append(f"j) confirmed-dead core should relaunch, got {r1}")
        if h.restart_calls != [True]:
            fails.append(f"j) dead relaunch should call restart once, got {h.restart_calls}")
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        r = h.run(now=1_000_000, alive=False, age=5000, booted=True)  # dead but just booted
        if r is not None or h.restart_calls:
            fails.append(f"j) a just-booted core must NOT be relaunched: {r}, {h.restart_calls}")
    return fails


def case_k2_deliberate_stop_never_relaunches() -> list[str]:
    """A graceful-stop tombstone gates the relaunch: a SIGTERM'd core reads
    dead but must NOT be restarted (relaunch would undo an intentional stop —
    john-the-dev, #2160). Control: same timeline without the tombstone still
    relaunches, so the gate can actually fail."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        h = Harness(Path(td) / "rec.json")
        h.run(now=1_000_000, alive=False, age=5000, stopped=True)
        r = h.run(now=1_000_200, alive=False, age=5000, stopped=True)
        if not r or r.get("action") != "deliberate-stop":
            fails.append(f"k) tombstoned dead core should report deliberate-stop, got {r}")
        if h.restart_calls:
            fails.append(f"k) tombstoned dead core must not restart, got {h.restart_calls}")
        if h.sent:
            fails.append(f"k) deliberate stop must not page, got {h.sent}")
    with tempfile.TemporaryDirectory() as td:     # control: gate absent -> case j behavior
        h = Harness(Path(td) / "rec.json")
        h.run(now=1_000_000, alive=False, age=5000, stopped=False)
        r = h.run(now=1_000_200, alive=False, age=5000, stopped=False)
        if not r or r.get("action") != "restarted" or h.restart_calls != [True]:
            fails.append(f"k) control without tombstone should still relaunch, got {r}, {h.restart_calls}")
    return fails


def case_k3_stopped_helper_reads_tombstone() -> list[str]:
    """_local_core_stopped itself: tombstone present -> True, absent -> False,
    and an unresolvable host label -> False (never suppress on uncertainty)."""
    import sys as _sys
    fails = []
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        _sys.path.insert(0, str(Path(hc.__file__).resolve().parent))
        import util_paths
        label = util_paths._host_label()
        if hc._local_core_stopped(ws):
            fails.append("k3) no tombstone should read False")
        cores = ws / "state" / "cores"
        cores.mkdir(parents=True)
        (cores / f"{label}.stopped").write_text("123.0")
        if not hc._local_core_stopped(ws):
            fails.append("k3) tombstone present should read True")
        real = util_paths._host_label
        try:
            util_paths._host_label = lambda: (_ for _ in ()).throw(RuntimeError("no label"))
            if hc._local_core_stopped(ws):
                fails.append("k3) unresolvable host label must read False, not suppress")
        finally:
            util_paths._host_label = real
    return fails


def case_j2_dead_core_before_after_output() -> list[str]:
    """BEFORE/AFTER health-check output for the dead-core relaunch:"""
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
        if (ra or {}).get("action") != "restarted" or ha.restart_calls != [True]:
            fails.append(f"j2) dead core should relaunch once, got {ra}/{ha.restart_calls}")
        dm = " ".join(ha.sent).lower()
        if not ("down" in dm and "relaunch" in dm):
            fails.append(f"j2) dead relaunch should DM 'core is down / relaunching', got {ha.sent}")
    return fails


def case_k_draining_backlog_never_restarts() -> list[str]:
    """A busy-but-healthy core surfaces a DIFFERENT oldest task each pass as it"""
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
    """Same oldest task across passes, but core-status.json advances → the core"""
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
    """If the wedge-restart DM fails, recovery still restarts (recovery >"""
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
        if h.restart_calls != [True]:
            fails.append(f"n) should have restarted once, got {h.restart_calls}")
        st = json.loads(sf.read_text())
        if st.get("last_restart_dm_sent") is not False:
            fails.append(f"n) state should record last_restart_dm_sent=False, got {st.get('last_restart_dm_sent')}")
    return fails


def case_o_launch_env_path() -> list[str]:
    """Regression for the 2026-07-10 launchd restart bug: under launchd's minimal"""
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
    """A wedged->dead transition on the SAME oldest task must start a fresh confirm
    window for the dead condition, not inherit the wedge's already-elapsed one."""
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
        if h.restart_calls != [True]:
            fails.append(f"p) dead relaunch should call restart once, got {h.restart_calls}")
    return fails


def case_q_preupgrade_state_dead_reobserves() -> list[str]:
    """A state file with wedge_first_seen but NO wedge_mode is an implied WEDGE window:
    a dead core on the first pass after upgrade must re-observe on its own window."""
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
    """A PEER heartbeat must not suppress a local relaunch: the workspace is shared, so
    an any-host liveness read makes a dead local core look alive. Fresh LOCAL still does."""
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
    """The default alive_fn is LOCAL, not fleet-wide: a live peer must never make
    this host look alive."""
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
            restart_fn=lambda: True,
            sender=lambda text: True,
        )
    finally:
        hc.WORKSPACE_DIR = orig

    # Assert on wedge_mode, NOT action: both modes return "observed", so an
    # action-only assertion passes under either and pins nothing.
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
    """An unresolvable host label reads NOT alive, so a label failure cannot be
    mistaken for a healthy core."""
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
        got = hc._local_core_alive(ws)
        # `is None`, NOT falsiness: a truthiness check cannot separate None
        # from False, so a three-state contract needs an identity check.
        if got is not None:
            fails.append(
                "t) an unresolvable host label returned %r — it must be None "
                "(UNKNOWN). False means DEFINITIVELY DEAD, and the actuator "
                "turns that into a restart of a healthy core." % (got,))
    finally:
        util_paths._host_label = original

    # The other two states, so None is not simply what this function always
    # returns — a control that cannot go positive proves nothing.
    if hc._local_core_alive(ws) is not True:
        fails.append("t) a fresh local heartbeat must read exactly True")
    (ws / "state" / "cores" / f"{original()}.alive").unlink()
    if hc._local_core_alive(ws) is not False:
        fails.append("t) an ABSENT heartbeat must read exactly False (dead), not None")
    return fails


def case_x_uncertainty_between_two_deaths_invalidates_the_window():
    """An UNKNOWN reading between two deaths invalidates the confirmation window: a probe
    too flaky for two consecutive deaths means unknown, and a destructive actuator must not fire."""
    import pathlib
    import tempfile
    fails = []
    ws = pathlib.Path(tempfile.mkdtemp())
    restarts = []
    common = dict(
        state_file=ws / "rec.json",
        oldest_task_fn=lambda: ("t1", 5000),
        status_ts_fn=lambda: None,
        just_booted_fn=lambda: False,
        restart_fn=lambda: restarts.append(True) or True,
        sender=lambda text: True,
    )
    seq = iter([False, None, False])          # dead, UNKNOWN, dead
    acts = [hc.recover_core_if_wedged(now=t, alive_fn=lambda: next(seq), **common)
            for t in (1000000, 1000200, 1000400)]

    if restarts:
        fails.append(
            "x) dead -> UNKNOWN -> dead RESTARTED — an uncertain pass must invalidate "
            f"the confirmation window, not be skipped over (actions={[a and a.get('action') for a in acts]})")
    if (acts[2] or {}).get("action") == "restarted":
        fails.append("x) the third pass restarted on a window opened before the uncertainty")
    if (acts[1] or {}).get("action") != "probe-failed":
        fails.append(f"x) the UNKNOWN pass should surface as probe-failed, got {acts[1]}")

    # PAIRED HALF: recovery must still work once the probe is clean again.
    seq2 = iter([False, False])
    acts2 = [hc.recover_core_if_wedged(now=t, alive_fn=lambda: next(seq2), **common)
             for t in (1000600, 1000800)]
    if not restarts:
        fails.append(
            "x) two clean consecutive DEAD observations after the uncertainty did NOT "
            f"restart — the reset disabled recovery instead of deferring it "
            f"(actions={[a and a.get('action') for a in acts2]})")
    return fails


def case_w_every_three_state_branch_of_both_helpers():
    """Every three-state branch of both helpers, so a change that collapses UNKNOWN
    into DEAD or ALIVE cannot pass by covering only the two it kept."""
    import json as _json
    import pathlib
    import sys as _sys
    import tempfile
    import time as _t
    from unittest import mock

    fails = []
    ws = pathlib.Path(tempfile.mkdtemp())
    (ws / "state" / "cores").mkdir(parents=True)
    _sys.path.insert(0, str(pathlib.Path(hc.__file__).resolve().parent))
    import util_paths
    label = util_paths._host_label()
    alive = ws / "state" / "cores" / f"{label}.alive"

    # An IO error on a file that EXISTS must not read as dead. Patched rather
    # than chmod-ed: a chmod test silently passes when running as root.
    alive.write_text("{}")
    real_stat = pathlib.Path.stat

    def _boom_stat(self, *a, **k):
        if self.name == alive.name:
            raise PermissionError("simulated unreadable heartbeat")
        return real_stat(self, *a, **k)

    with mock.patch.object(pathlib.Path, "stat", _boom_stat):
        got = hc._local_core_alive(ws)
    if got is not None:
        fails.append(f"w) an unreadable-but-PRESENT heartbeat returned {got!r}; must be None")

    # --- _local_core_started_within, every branch ---
    # stale mtime -> False (readable evidence of "not just booted")
    alive.write_text(_json.dumps({"started_at": _t.time()}))
    import os as _os
    old = _t.time() - 500
    _os.utime(alive, (old, old))
    if hc._local_core_started_within(600, workspace=ws) is not False:
        fails.append("w) a STALE heartbeat must read exactly False, not None")

    # fresh + valid + recent started_at -> True (the happy path, never executed before)
    now = _t.time()
    alive.write_text(_json.dumps({"started_at": now - 10}))
    if hc._local_core_started_within(600, workspace=ws, now=now) is not True:
        fails.append("w) a fresh core booted 10s ago must read exactly True")

    # fresh + valid + OLD started_at -> False
    alive.write_text(_json.dumps({"started_at": now - 5000}))
    if hc._local_core_started_within(600, workspace=ws, now=now) is not False:
        fails.append("w) a core booted 5000s ago must read exactly False")

    # undecodable payload -> UNKNOWN (the except (OSError, ValueError) clause)
    alive.write_text("{ not json")
    if hc._local_core_started_within(600, workspace=ws, now=now) is not None:
        fails.append("w) an UNDECODABLE heartbeat must read None, not False")

    # started_at missing / non-numeric -> UNKNOWN
    for payload in ({}, {"started_at": "soon"}):
        alive.write_text(_json.dumps(payload))
        if hc._local_core_started_within(600, workspace=ws, now=now) is not None:
            fails.append(f"w) started_at={payload!r} must read None — the boot time is unknown")

    # absent file -> False (nothing booted here), distinct from unreadable
    alive.unlink()
    if hc._local_core_started_within(600, workspace=ws, now=now) is not False:
        fails.append("w) an ABSENT heartbeat must read False (nothing booted), not None")

    # now=None default path
    alive.write_text(_json.dumps({"started_at": _t.time()}))
    if hc._local_core_started_within(600, workspace=ws) is not True:
        fails.append("w) the now=None default must resolve to wall-clock and read True")
    return fails


def case_v_an_unknown_probe_must_not_restart_a_healthy_core():
    """An unknown probe result must not restart a healthy core — the actuator is
    destructive, so absence of evidence is not evidence of death."""
    import pathlib
    import sys as _sys
    import tempfile
    fails = []
    ws = pathlib.Path(tempfile.mkdtemp())
    (ws / "state" / "cores").mkdir(parents=True)
    orig_ws = hc.WORKSPACE_DIR
    hc.WORKSPACE_DIR = ws

    _sys.path.insert(0, str(pathlib.Path(hc.__file__).resolve().parent))
    import util_paths
    original = util_paths._host_label
    util_paths._host_label = lambda: (_ for _ in ()).throw(RuntimeError("unresolvable"))

    restarts = []
    try:
        common = dict(
            state_file=ws / "rec.json",
            oldest_task_fn=lambda: ("task-stuck", 5000),
            status_ts_fn=lambda: None,
            restart_fn=lambda: restarts.append(True) or True,
            sender=lambda text: True,
        )   # alive_fn / just_booted_fn DELIBERATELY not injected — the defaults
            # are the thing under test.
        r1 = hc.recover_core_if_wedged(now=1000000, **common)
        r2 = hc.recover_core_if_wedged(now=1000200, **common)
    finally:
        util_paths._host_label = original
        hc.WORKSPACE_DIR = orig_ws

    if restarts:
        fails.append(
            "v) a failed liveness probe RESTARTED the core — unknown state must "
            "suppress the destructive action, not trigger it")
    for lbl, r in (("pass1", r1), ("pass2", r2)):
        if (r or {}).get("action") != "probe-failed":
            fails.append(
                f"v) {lbl} returned {r} — an unknown probe must surface itself as "
                "'probe-failed', not pass silently as an observation")
    return fails


def case_u_peer_boot_does_not_suppress_local_recovery():
    """A peer's recent boot must not suppress LOCAL recovery; the boot grace is
    per-host, and reading it fleet-wide silently disables recovery on this one."""
    import json as _json
    import pathlib
    import tempfile
    fails = []
    ws = pathlib.Path(tempfile.mkdtemp())
    (ws / "state" / "cores").mkdir(parents=True)
    import os as _os
    _peer = ws / "state" / "cores" / "peer-host.alive"
    _peer.write_text(_json.dumps({"started_at": 1_000_000}))
    # heartbeat_is_fresh is two-sided, so a real wall-clock mtime reads as
    # future-dated against now=1_000_100 and the peer is skipped.
    _os.utime(_peer, (1_000_100, 1_000_100))

    if not hc._core_started_within(300, workspace=ws, now=1_000_100):
        fails.append("u) precondition: the fleet guard should see the peer's boot")
    if hc._local_core_started_within(300, workspace=ws, now=1_000_100):
        fails.append("u) a PEER's boot satisfied the LOCAL just-booted guard (the leak)")

    orig = hc.WORKSPACE_DIR
    hc.WORKSPACE_DIR = ws
    try:
        r = hc.recover_core_if_wedged(
            state_file=ws / "rec.json", now=1_000_100,
            # alive_fn and just_booted_fn deliberately NOT injected
            oldest_task_fn=lambda: ("task-peer-started", 5000),
            status_ts_fn=lambda: None,
            restart_fn=lambda: True,
            sender=lambda text: True,
        )
    finally:
        hc.WORKSPACE_DIR = orig

    if r is None:
        fails.append("u) a peer boot suppressed local recovery entirely — no confirm window started")
    st = _json.loads((ws / "rec.json").read_text()) if (ws / "rec.json").exists() else {}
    if st.get("wedge_mode") != "dead":
        fails.append(f"u) expected the local core recorded DEAD, got wedge_mode={st.get('wedge_mode')!r}")
    return fails


def main() -> int:
    cases = [
        ("a", case_a_healthy_no_action),
        ("b", case_b_just_booted_never_restarts),
        ("c", case_c_first_observation_no_restart),
        ("d", case_d_within_confirm_window_no_restart),
        ("e", case_e_confirmed_restart_keeps_1m),
        ("f", case_f_cooldown_blocks_second_restart),
        ("g", case_g_recurrence_does_not_downgrade_the_model),
        ("h", case_h_give_up_cap),
        ("i", case_i_failed_restart_does_not_burn_state),
        ("j", case_j_dead_core_relaunches),
        ("r", case_r_local_liveness_ignores_peer_heartbeats),
        ("s", case_s_actuator_DEFAULT_alive_fn_is_local_not_fleet),
        ("t", case_t_unresolvable_host_label_reads_NOT_alive),
        ("v", case_v_an_unknown_probe_must_not_restart_a_healthy_core),
        ("w", case_w_every_three_state_branch_of_both_helpers),
        ("x", case_x_uncertainty_between_two_deaths_invalidates_the_window),
        ("u", case_u_peer_boot_does_not_suppress_local_recovery),
        ("j2", case_j2_dead_core_before_after_output),
        ("k", case_k_draining_backlog_never_restarts),
        ("k2", case_k2_deliberate_stop_never_relaunches),
        ("k3", case_k3_stopped_helper_reads_tombstone),
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
