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
  l) socket resolution fallbacks     → default-workspace branch, no cores
     dir, stale heartbeat, corrupt heartbeat all fall back to the default
     socket (env-overridable)
  m) tmux binary resolution          → existing candidate wins, none → PATH
  n) REAL nudge subprocess path      → fake tmux binary: missing session →
     False; live session → send-keys carries /schedule-crons + Enter on the
     right socket; failed send-keys → False; missing binary → False
  o) degraded state files            → defaulted args quiet on an empty
     workspace; corrupt / non-dict / unreadable+unwritable state never
     crash; unwritable parent degrades to 'locked' (no nudge)
  p) healthy pass clears observation → stale-observed then fresh stamp
     resets cron_first_seen so a future episode starts over
  q) give-up DM fails                → gave_up_at NOT recorded (re-DMs next
     pass), warning logged, still no nudge
  r) nudge DM fails                  → still nudges (recovery > notification),
     records dm_sent=False
  s) main() --recover-core wiring    → cron recovery runs after the wedge
     path EXCEPT when the wedge path just restarted the core

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


def case_l_socket_resolution_fallbacks() -> list[str]:
    """_live_core_socket must fall back to the (env-overridable) default
    socket when there is no cores dir, only a STALE heartbeat, or a corrupt
    heartbeat file — and must honor the default-workspace branch."""
    fails = []
    saved_ws = hc.WORKSPACE_DIR
    saved_env = os.environ.get("SUTANDO_TMUX_SOCKET")
    try:
        os.environ["SUTANDO_TMUX_SOCKET"] = "/tmp/env-default.sock"
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            hc.WORKSPACE_DIR = ws
            # No cores dir at all → default, via the default-workspace branch.
            if hc._live_core_socket() != "/tmp/env-default.sock":
                fails.append("l) empty workspace should fall back to the env default socket")
            cores = ws / "state" / "cores"
            cores.mkdir(parents=True)
            # Stale heartbeat (old mtime) → not a live core → default.
            stale = cores / "stale.alive"
            stale.write_text(json.dumps({"socket": "/tmp/stale.sock"}))
            old = time.time() - 600
            os.utime(stale, (old, old))
            if hc._live_core_socket(ws) != "/tmp/env-default.sock":
                fails.append("l) stale heartbeat must not supply the socket")
            # Corrupt heartbeat (fresh mtime, garbage JSON) → skipped → default.
            corrupt = cores / "corrupt.alive"
            corrupt.write_text("not json{")
            if hc._live_core_socket(ws) != "/tmp/env-default.sock":
                fails.append("l) corrupt heartbeat must not crash or supply the socket")
            # NON-OBJECT heartbeat (fresh mtime, VALID json that isn't a mapping).
            # Distinct from the garbage case above: `null` / `[]` / `"x"` / `3` all
            # decode fine and then raise AttributeError on `.get`, which the
            # (OSError, ValueError) handler does NOT catch — one junk file took the
            # whole call down, and its caller `_rearm_core_crons()` is a RECOVERY path.
            nonobj = cores / "nonobject.alive"
            for raw in ("null", "[]", '"sock"', "3"):
                nonobj.write_text(raw)
                try:
                    got = hc._live_core_socket(ws)
                except Exception as exc:  # noqa: BLE001 — a crash IS the failure
                    fails.append(f"l) non-object heartbeat {raw!r} crashed: {type(exc).__name__}")
                    continue
                if got != "/tmp/env-default.sock":
                    fails.append(f"l) non-object heartbeat {raw!r} should fall back, got {got}")
            nonobj.unlink()
            # Fresh heartbeat with a socket beats the fallbacks.
            good = cores / "good.alive"
            good.write_text(json.dumps({"socket": "/tmp/live.sock"}))
            if hc._live_core_socket(ws) != "/tmp/live.sock":
                fails.append("l) fresh heartbeat socket should win over the default")
            # A junk PEER file must not hide a good one. This glob reads *.alive for
            # EVERY host, so one machine writing an unexpected shape must not blind
            # this host to its own live core.
            (cores / "peerjunk.alive").write_text("null")
            if hc._live_core_socket(ws) != "/tmp/live.sock":
                fails.append("l) a junk peer heartbeat must not mask a good one")
    finally:
        hc.WORKSPACE_DIR = saved_ws
        if saved_env is None:
            os.environ.pop("SUTANDO_TMUX_SOCKET", None)
        else:
            os.environ["SUTANDO_TMUX_SOCKET"] = saved_env
    return fails


def case_m_tmux_bin_resolution() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "tmux"
        fake.write_text("#!/bin/sh\nexit 0\n")
        if hc._resolve_tmux_bin(candidates=(str(Path(td) / "nope"), str(fake))) != str(fake):
            fails.append("m) existing candidate should be returned")
        if hc._resolve_tmux_bin(candidates=(str(Path(td) / "nope"),)) != "tmux":
            fails.append("m) no candidate should fall back to a PATH lookup ('tmux')")
    return fails


def _fake_tmux(td: Path, has_rc: int, send_rc: int) -> "tuple[str, Path]":
    """Write an executable fake tmux that logs its argv and exits has_rc for
    has-session, send_rc for send-keys. Returns (bin_path, log_path)."""
    log = td / "tmux.log"
    log.unlink(missing_ok=True)  # each scenario starts with a clean call log
    script = td / "tmux"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        f'case "$*" in *has-session*) exit {has_rc};; *send-keys*) exit {send_rc};; esac\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return str(script), log


def case_n_real_nudge_subprocess_path() -> list[str]:
    """Drive _default_cron_nudge's REAL subprocess path against a fake tmux
    binary — the failure mode is a keystroke that never lands, so prove what
    is actually typed and how each tmux failure maps to False."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        # Missing session: has-session exits non-zero → False, no send-keys.
        bin_a, log_a = _fake_tmux(tdp, has_rc=1, send_rc=0)
        if hc._default_cron_nudge(tmux_bin=bin_a, sock="/tmp/x.sock", session="sutando-core"):
            fails.append("n) missing tmux session should return False")
        if log_a.exists() and "send-keys" in log_a.read_text():
            fails.append("n) must not send-keys when the session is missing")
        log_a.unlink(missing_ok=True)
        # Live session: True, and the send-keys call carries the exact
        # re-arm keystroke on the resolved socket + session.
        bin_b, log_b = _fake_tmux(tdp, has_rc=0, send_rc=0)
        if not hc._default_cron_nudge(tmux_bin=bin_b, sock="/tmp/x.sock", session="sutando-core"):
            fails.append("n) live session should return True")
        sent = log_b.read_text() if log_b.exists() else ""
        send_lines = [ln for ln in sent.splitlines() if "send-keys" in ln]
        if len(send_lines) != 1:
            fails.append(f"n) expected exactly one send-keys call, log: {sent!r}")
        elif not all(tok in send_lines[0] for tok in ("-S /tmp/x.sock", "-t sutando-core", "/schedule-crons", "Enter")):
            fails.append(f"n) send-keys must type /schedule-crons + Enter at the pane, got: {send_lines[0]!r}")
        log_b.unlink(missing_ok=True)
        # send-keys itself fails → False (nudge did not land).
        bin_c, _ = _fake_tmux(tdp, has_rc=0, send_rc=1)
        if hc._default_cron_nudge(tmux_bin=bin_c, sock="/tmp/x.sock", session="sutando-core"):
            fails.append("n) failed send-keys should return False")
        # Binary missing entirely → exception path → False, never raises.
        if hc._default_cron_nudge(tmux_bin=str(tdp / "no-such-tmux"), sock="/tmp/x.sock", session="s"):
            fails.append("n) missing tmux binary should return False")
        # Default sock/session resolution: with only the binary injected, the
        # socket must come from the live heartbeat (via _live_core_socket) and
        # the session from the canonical default.
        saved_ws = hc.WORKSPACE_DIR
        saved_session_env = os.environ.pop("SUTANDO_TMUX_SESSION", None)
        try:
            ws = tdp / "ws"
            cores = ws / "state" / "cores"
            cores.mkdir(parents=True)
            (cores / "h.alive").write_text(json.dumps({"socket": "/tmp/hb.sock"}))
            hc.WORKSPACE_DIR = ws
            bin_d, log_d = _fake_tmux(tdp, has_rc=0, send_rc=0)
            if not hc._default_cron_nudge(tmux_bin=bin_d):
                fails.append("n) default sock/session resolution should still nudge")
            logged = log_d.read_text() if log_d.exists() else ""
            if "-S /tmp/hb.sock" not in logged or "-t sutando-core" not in logged:
                fails.append(f"n) defaults must resolve heartbeat socket + canonical session, got: {logged!r}")
        finally:
            hc.WORKSPACE_DIR = saved_ws
            if saved_session_env is not None:
                os.environ["SUTANDO_TMUX_SESSION"] = saved_session_env
        # Default tmux resolution (real binary or PATH lookup) against a
        # socket no server listens on: fails closed, never raises.
        if hc._default_cron_nudge(sock=str(tdp / "no-server.sock"), session="sutando-core-test"):
            fails.append("n) default tmux against a serverless socket should return False")
    return fails


def case_o_degraded_state_files() -> list[str]:
    """State-file damage must never crash recovery or fire a spurious nudge."""
    fails = []
    saved_ws = hc.WORKSPACE_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            hc.WORKSPACE_DIR = ws
            # Fully-defaulted call (real time, default state_file under the
            # patched workspace, default detection fns) on an empty workspace:
            # no heartbeat → not alive → quiet.
            r = hc.recover_cron_if_dead()
            if r is not None:
                fails.append(f"o) defaulted call on an empty workspace should be quiet, got {r}")
        frozen = 1_000_000 - 3600
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "cron.json"
            sf.write_text("garbage{{{")               # corrupt JSON
            h = Harness(sf)
            r = h.run(now=1_000_000, status_ts=frozen)
            if not r or r.get("action") != "observed":
                fails.append(f"o) corrupt state should reset to observed, got {r}")
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "cron.json"
            sf.write_text("[1, 2]")                    # valid JSON, wrong shape
            h = Harness(sf)
            r = h.run(now=1_000_000, status_ts=frozen)
            if not r or r.get("action") != "observed":
                fails.append(f"o) non-dict state should reset to observed, got {r}")
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "cron.json"
            sf.mkdir()                                 # unreadable AND unwritable as a file
            h = Harness(sf)
            r = h.run(now=1_000_000, status_ts=frozen)
            if not r or r.get("action") != "observed":
                fails.append(f"o) dir-shaped state (read+save both fail) should still observe, got {r}")
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / "blocker"
            blocker.write_text("a file where a dir must go")
            h = Harness(blocker / "cron.json")         # parent mkdir + lock open both fail
            r = h.run(now=1_000_000, status_ts=frozen)
            if r != {"action": "locked"}:
                fails.append(f"o) unwritable state parent should degrade to 'locked', got {r}")
            if h.nudges:
                fails.append("o) degraded state paths must never nudge")
    finally:
        hc.WORKSPACE_DIR = saved_ws
    return fails


def case_p_healthy_pass_clears_observation() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "cron.json"
        h = Harness(sf)
        h.run(now=1_000_000, status_ts=1_000_000 - 3600)   # observed
        r = h.run(now=1_000_100, status_ts=1_000_090)      # stamp fresh again
        if r is not None:
            fails.append(f"p) fresh stamp should be quiet, got {r}")
        st = json.loads(sf.read_text())
        if st.get("cron_first_seen") != 0 or st.get("cron_status_ts") is not None:
            fails.append(f"p) healthy pass must clear the observation, state: {st}")
    return fails


def case_q_giveup_dm_failure_not_silenced() -> list[str]:
    """A failed give-up DM must NOT record gave_up_at (a Slack outage would
    otherwise silence the alert for an hour) — the next pass re-DMs."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "cron.json"
        h = Harness(sf)
        h.send_ok = False
        now = 2_000_000
        frozen = now - 7200
        sf.write_text(json.dumps({
            "cron_first_seen": now - 500,
            "cron_status_ts": frozen,
            "last_nudge": now - hc.RECOVER_COOLDOWN_SEC - 10,
            "nudge_history": [now - 3000, now - 2000, now - hc.RECOVER_COOLDOWN_SEC - 10],
        }))
        r = h.run(now=now, status_ts=frozen)
        if not r or r.get("action") != "gave_up":
            fails.append(f"q) should give up, got {r}")
        if h.nudges:
            fails.append("q) gave-up state still nudged")
        st = json.loads(sf.read_text())
        if st.get("gave_up_at"):
            fails.append("q) failed give-up DM must not record gave_up_at")
        h.run(now=now + 60, status_ts=frozen)
        if len(h.sent) != 2:
            fails.append(f"q) failed give-up DM should retry next pass, sent {len(h.sent)}")
    return fails


def case_r_nudge_dm_failure_still_nudges() -> list[str]:
    """Recovery > notification: a failed pre-nudge DM still nudges, and the
    result + state record dm_sent=False so the nudge is never invisible."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "cron.json"
        h = Harness(sf)
        h.send_ok = False
        frozen = 1_000_000 - 3600
        h.run(now=1_000_000, status_ts=frozen)
        r = h.run(now=1_000_200, status_ts=frozen)
        if not r or r.get("action") != "nudged":
            fails.append(f"r) should still nudge when the DM fails, got {r}")
        if r and r.get("dm_sent") is not False:
            fails.append(f"r) failed DM should record dm_sent=False, got {r.get('dm_sent')}")
        if h.nudges != 1:
            fails.append(f"r) should have nudged once, got {h.nudges}")
        st = json.loads(sf.read_text())
        if st.get("last_nudge_dm_sent") is not False:
            fails.append(f"r) state should record last_nudge_dm_sent=False, got {st.get('last_nudge_dm_sent')}")
    return fails


def case_s_main_wiring_recover_flag() -> list[str]:
    """main() --recover-core runs the cron-layer check after the wedge path,
    EXCEPT when the wedge path just RESTARTED the core (a restart re-arms
    crons via startup; keystrokes into a relaunching pane are noise)."""
    import contextlib
    import io
    fails = []
    saved = (hc.run_all_checks, hc.recover_core_if_wedged, hc.recover_cron_if_dead, sys.argv)
    try:
        hc.run_all_checks = lambda: []
        for wedge_result, expect_cron in [
            ({"action": "restarted", "mode": "1m"}, False),
            ({"action": "observed"}, True),
            (None, True),
        ]:
            calls = []
            hc.recover_core_if_wedged = lambda wr=wedge_result: wr
            hc.recover_cron_if_dead = lambda: calls.append(1)
            sys.argv = ["health-check.py", "--recover-core", "--json"]
            with contextlib.redirect_stdout(io.StringIO()):
                hc.main()
            if bool(calls) != expect_cron:
                fails.append(
                    f"s) wedge={wedge_result and wedge_result.get('action')} → cron recovery "
                    f"{'skipped' if expect_cron else 'ran'} (calls={len(calls)})"
                )
    finally:
        hc.run_all_checks, hc.recover_core_if_wedged, hc.recover_cron_if_dead, sys.argv = saved
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
        ("l", case_l_socket_resolution_fallbacks),
        ("m", case_m_tmux_bin_resolution),
        ("n", case_n_real_nudge_subprocess_path),
        ("o", case_o_degraded_state_files),
        ("p", case_p_healthy_pass_clears_observation),
        ("q", case_q_giveup_dm_failure_not_silenced),
        ("r", case_r_nudge_dm_failure_still_nudges),
        ("s", case_s_main_wiring_recover_flag),
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
