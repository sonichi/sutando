#!/usr/bin/env python3
"""
Regression tests: the bridge restart path never spawns a bridge process
directly while a launchd supervisor owns that bridge.

Incidents (2026-08-28 and 2026-08-29, same shape): a git pull made the on-disk
slack-bridge code newer than the running process, health-check's stale-restart
path killed the running bridge — which was com.sutando.slack-bridge's wrapper
child — and hand-spawned a replacement via Popen. The orphan (ppid 1) took the
singleton lock; the wrapper's keepalive respawns then lost the lock every ~10s
forever, one owner alert per cycle, until the orphan was killed by hand.

Guards:

  a) supervisor registered in launchd + bridge down → fix_down_bridges restarts
     THROUGH launchd (`kickstart -k`) and Popen never receives a bridge argv.
     This is the incident witness: at the parent commit the bridge is
     hand-spawned here, so this case fails there and passes at HEAD.
  b) supervised + stale → _restart_bridge kickstarts; no direct kill+spawn.
  c) supervised, kickstart refused → the supervised CHILD is killed so the
     wrapper's keepalive respawns it; still no direct spawn.
  d) supervised, kickstart refused, no child found → reported as NOT restarted;
     still no direct spawn (never trade a warning for a lock-contention loop).
  e) supervisor seen only as a live wrapper process (launchctl blind) →
     treated as supervised.
  f) no supervisor → the pre-existing behavior is intact: stale kills the old
     pid first, then the bridge is spawned directly.

Run: python3 tests/health-check-supervised-bridge-restart.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

LABEL = "com.sutando.slack-bridge"
DOWN = {"name": "slack-bridge", "status": "warn", "detail": "configured but not running"}


class Host:
    """Fake subprocess boundary: launchctl + pgrep + kill answered from config,
    Popen recorded. Everything is asserted at this boundary so the cases run
    against any internal shape of the restart path (including the parent's)."""

    def __init__(self, *, job_registered=True, kickstart_rc=0,
                 wrapper_alive=False, child_pids=""):
        self.job_registered = job_registered
        self.kickstart_rc = kickstart_rc
        self.wrapper_alive = wrapper_alive
        self.child_pids = child_pids
        self.spawned = []  # Popen argvs
        self.kickstarted = []
        self.killed = []  # pids passed to /bin/kill

    def run(self, argv, *args, **kwargs):
        if not isinstance(argv, list):
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:2] == ["/bin/launchctl", "print"]:
            rc = 0 if self.job_registered else 113
            out = "state = running" if self.job_registered else ""
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
        if argv[:3] == ["/bin/launchctl", "kickstart", "-k"]:
            self.kickstarted.append(argv)
            return subprocess.CompletedProcess(argv, self.kickstart_rc, stdout="", stderr="")
        if argv[:2] == ["/usr/bin/pgrep", "-f"]:
            if "wrapper" in argv[2]:
                rc = 0 if self.wrapper_alive else 1
                out = "4242\n" if self.wrapper_alive else ""
            else:
                rc = 0 if self.child_pids else 1
                out = self.child_pids
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
        if argv[0] == "/bin/kill":
            self.killed.append(argv[1])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def popen(self, argv, **kwargs):
        self.spawned.append(argv)
        return mock.MagicMock()

    def bridge_spawns(self):
        return [a for a in self.spawned if any(str(t).endswith("-bridge.py") for t in a)]


def with_host(host, fn):
    slack_env = {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test"}
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
             mock.patch.object(hc, "_bridge_interpreter", return_value="python3"), \
             mock.patch.object(hc, "_load_channel_env", return_value=slack_env), \
             mock.patch.object(hc, "token_from_vault", return_value=""), \
             mock.patch.object(hc.time, "sleep", lambda *_: None), \
             mock.patch.object(hc.subprocess, "run", side_effect=host.run), \
             mock.patch.object(hc.subprocess, "Popen", side_effect=host.popen):
            return fn()


def case_a_supervised_down_restarts_through_launchd() -> list[str]:
    """The incident witness: fails at the parent commit (hand-spawn), passes at
    HEAD (kickstart, no spawn)."""
    fails = []
    host = Host(job_registered=True)
    restarted = with_host(host, lambda: hc.fix_down_bridges(
        [dict(DOWN)], action="restart", guard=lambda _r: (True, "test-clean"),
        sender=lambda _m: True, notifier=lambda _m: False))
    if host.bridge_spawns():
        fails.append(f"a) bridge hand-spawned alongside its launchd supervisor: {host.bridge_spawns()}")
    if not any(a[-1].endswith(LABEL) for a in host.kickstarted):
        fails.append(f"a) expected `launchctl kickstart -k .../{LABEL}`, got {host.kickstarted}")
    if restarted != ["slack-bridge"]:
        fails.append(f"a) expected slack-bridge reported restarted, got {restarted}")
    return fails


def case_b_supervised_stale_kickstarts() -> list[str]:
    fails = []
    host = Host(job_registered=True, child_pids="555\n")
    ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok:
        fails.append(f"b) supervised stale restart reported failure: {how}")
    if host.bridge_spawns():
        fails.append(f"b) stale path hand-spawned despite supervisor: {host.bridge_spawns()}")
    if not host.kickstarted:
        fails.append("b) stale path never kickstarted the supervisor")
    if host.killed:
        fails.append(f"b) kickstart succeeded yet pids were also killed directly: {host.killed}")
    return fails


def case_c_kickstart_refused_kills_child_only() -> list[str]:
    fails = []
    host = Host(job_registered=True, kickstart_rc=1, child_pids="555\n")
    ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok:
        fails.append(f"c) child-kill fallback reported failure: {how}")
    if host.killed != ["555"]:
        fails.append(f"c) expected the supervised child (555) killed, got {host.killed}")
    if host.bridge_spawns():
        fails.append(f"c) fallback hand-spawned despite supervisor: {host.bridge_spawns()}")
    return fails


def case_d_supervised_with_no_child_is_reported_not_spawned() -> list[str]:
    fails = []
    host = Host(job_registered=True, kickstart_rc=1, child_pids="")
    ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge"))
    if ok:
        fails.append("d) restart must report failure when the supervisor cannot be driven")
    if "kickstart" not in how:
        fails.append(f"d) failure message must name the manual remedy, got {how!r}")
    if host.bridge_spawns():
        fails.append(f"d) hand-spawned as a fallback despite supervisor: {host.bridge_spawns()}")
    return fails


def case_e_wrapper_process_counts_as_supervisor() -> list[str]:
    fails = []
    host = Host(job_registered=False, kickstart_rc=1, wrapper_alive=True, child_pids="555\n")
    ok, _how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok:
        fails.append("e) wrapper-supervised restart reported failure")
    if host.bridge_spawns():
        fails.append(f"e) hand-spawned despite a live wrapper process: {host.bridge_spawns()}")
    if host.killed != ["555"]:
        fails.append(f"e) expected the wrapper's child killed for keepalive respawn, got {host.killed}")
    return fails


def case_f_unsupervised_direct_spawn_is_preserved() -> list[str]:
    fails = []
    host = Host(job_registered=False, wrapper_alive=False, child_pids="555\n")
    ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok:
        fails.append(f"f) unsupervised restart reported failure: {how}")
    if host.killed != ["555"]:
        fails.append(f"f) stale pid must be killed before the respawn, got {host.killed}")
    if len(host.bridge_spawns()) != 1:
        fails.append(f"f) expected exactly one direct spawn, got {host.spawned}")
    # Down (non-stale) leg: no kill, one spawn.
    host2 = Host(job_registered=False, wrapper_alive=False, child_pids="")
    ok2, _ = with_host(host2, lambda: hc._restart_bridge("slack-bridge"))
    if not ok2 or len(host2.bridge_spawns()) != 1 or host2.killed:
        fails.append(f"f) down leg changed: ok={ok2} spawns={host2.spawned} killed={host2.killed}")
    return fails


def main() -> int:
    cases = [
        case_a_supervised_down_restarts_through_launchd,
        case_b_supervised_stale_kickstarts,
        case_c_kickstart_refused_kills_child_only,
        case_d_supervised_with_no_child_is_reported_not_spawned,
        case_e_wrapper_process_counts_as_supervisor,
        case_f_unsupervised_direct_spawn_is_preserved,
    ]
    failures = []
    for case in cases:
        try:
            failures.extend(case())
        except Exception as e:  # noqa: BLE001 — a crashed case is a failed case
            failures.append(f"{case.__name__} raised {type(e).__name__}: {e}")
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print(f"ok — {len(cases)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
