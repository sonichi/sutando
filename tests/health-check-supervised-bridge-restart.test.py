#!/usr/bin/env python3
"""
Regression tests: the bridge restart path never spawns a bridge process
directly while a launchd supervisor owns that bridge — and never treats an
unreadable probe, an unowned pid, or an unconfirmed kill as success.

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
  b) supervised + stale → kickstart; no direct kill, spawn, or eviction.
  c) kickstart refused → only the wrapper's OWN child (ppid = wrapper) is
     killed, and success is claimed only after `kill -0` confirms it exited.
  d) registered-but-inactive job + a bare same-name pid → the bare pid is NOT
     signalled and the restart reports failure; still no spawn.
  e) supervisor seen only as a live wrapper process (launchctl finds no job) →
     treated as supervised.
  f) supervision conclusively absent → stale eviction goes through
     evict-own-bridge.sh (checkout-identity contract) BEFORE the one direct
     spawn, proven on a single ordered event stream; down leg spawns without
     eviction.
  g) UNKNOWN supervision fails CLOSED: launchctl+pgrep probe failures, a
     gateway launchctl failure, and a pgrep hard error all refuse to spawn,
     kill, or evict.
  h) a kill whose signal fails, or whose target survives `kill -0`, is
     reported as failure — never as recovery.
  i) plan-None and failed-spawn legs report failure without side effects.

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
WRAPPER_PID = "4242"
DOWN = {"name": "slack-bridge", "status": "warn", "detail": "configured but not running"}


class Host:
    """Fake subprocess boundary: launchctl/pgrep/ps/kill answered from config,
    Popen recorded. Side effects land on ONE ordered `events` stream so cases
    can assert ordering, not just presence."""

    def __init__(self, *, job_registered=True, kickstart_rc=0,
                 wrapper_alive=False, child_pids="", bare_pids=(),
                 print_raises=False, kickstart_raises=False, pgrep_raises=False,
                 pgrep_rc_error=False, ps_rc_error=False, ps_raises=False,
                 evict_raises=False, kill_rc=0, alive_after_kill=False):
        self.job_registered = job_registered
        self.kickstart_rc = kickstart_rc
        self.wrapper_alive = wrapper_alive
        self.child_pids = [p for p in child_pids.split() if p]
        self.bare_pids = list(bare_pids)
        self.print_raises = print_raises
        self.kickstart_raises = kickstart_raises
        self.pgrep_raises = pgrep_raises
        self.pgrep_rc_error = pgrep_rc_error
        self.ps_rc_error = ps_rc_error
        self.ps_raises = ps_raises
        self.evict_raises = evict_raises
        self.kill_rc = kill_rc
        self.alive_after_kill = alive_after_kill
        self.events = []  # ordered: ("kickstart"|"kill"|"evict"|"spawn", detail)

    def _ps_rows(self):
        rows = []
        if self.wrapper_alive:
            rows.append((WRAPPER_PID, "1",
                         "/bin/bash /repo/src/launchd/channel-bridge-wrapper.sh slack"))
            for pid in self.child_pids:
                rows.append((pid, WRAPPER_PID, "/usr/bin/python3 /repo/src/slack-bridge.py"))
        for pid in self.bare_pids:
            rows.append((pid, "1", "/usr/bin/python3 /elsewhere/src/slack-bridge.py"))
        return rows

    def run(self, argv, *args, **kwargs):
        if not isinstance(argv, list):
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:2] == ["/bin/launchctl", "print"]:
            if self.print_raises:
                raise OSError("launchctl unavailable")
            rc = 0 if self.job_registered else 113
            out = "state = running" if self.job_registered else ""
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
        if argv[:3] == ["/bin/launchctl", "kickstart", "-k"]:
            self.events.append(("kickstart", argv[3]))
            if self.kickstart_raises:
                raise subprocess.TimeoutExpired(argv, 15)
            return subprocess.CompletedProcess(argv, self.kickstart_rc, stdout="", stderr="")
        if argv[:2] == ["/usr/bin/pgrep", "-f"]:
            if self.pgrep_raises:
                raise OSError("pgrep unavailable")
            if self.pgrep_rc_error:
                return subprocess.CompletedProcess(argv, 2, stdout="", stderr="pgrep: bad")
            rc = 0 if self.wrapper_alive else 1
            out = f"{WRAPPER_PID}\n" if self.wrapper_alive else ""
            return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")
        if argv[:2] == ["/bin/ps", "-axo"]:
            if self.ps_raises:
                raise OSError("ps unavailable")
            if self.ps_rc_error:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="ps: bad")
            out = "\n".join(f"{p} {pp} {cmd}" for p, pp, cmd in self._ps_rows())
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        if argv[0] == "/bin/kill" and argv[1] == "-0":
            alive = self.alive_after_kill
            return subprocess.CompletedProcess(argv, 0 if alive else 1, stdout="", stderr="")
        if argv[0] == "/bin/kill":
            self.events.append(("kill", argv[1]))
            return subprocess.CompletedProcess(argv, self.kill_rc, stdout="", stderr="")
        if argv[0] == "/bin/bash" and str(argv[1]).endswith("evict-own-bridge.sh"):
            if self.evict_raises:
                raise OSError("bash unavailable")
            self.events.append(("evict", f"{argv[2]}:{argv[3]}"))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def popen(self, argv, **kwargs):
        self.events.append(("spawn", argv))
        return mock.MagicMock()

    def of(self, kind):
        return [d for k, d in self.events if k == kind]

    def bridge_spawns(self):
        return [a for a in self.of("spawn") if any(str(t).endswith("-bridge.py") for t in a)]


def with_host(host, fn):
    slack_env = {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test",
                 "REMOTE_TASK_TOKEN": "gw-test"}
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
             mock.patch.object(hc, "_bridge_interpreter", return_value="python3"), \
             mock.patch.object(hc, "_load_channel_env", return_value=slack_env), \
             mock.patch.object(hc, "token_from_vault", return_value=""), \
             mock.patch.object(hc.time, "sleep", lambda *_: None), \
             mock.patch.object(hc.subprocess, "run", side_effect=host.run), \
             mock.patch.object(hc.subprocess, "Popen", side_effect=host.popen):
            return fn()


def no_side_effects(host, label):
    fails = []
    if host.bridge_spawns():
        fails.append(f"{label}: spawned: {host.bridge_spawns()}")
    if host.of("kill"):
        fails.append(f"{label}: killed: {host.of('kill')}")
    if host.of("evict"):
        fails.append(f"{label}: evicted: {host.of('evict')}")
    return fails


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
    if not any(t.endswith(LABEL) for t in host.of("kickstart")):
        fails.append(f"a) expected `launchctl kickstart -k .../{LABEL}`, got {host.events}")
    if restarted != ["slack-bridge"]:
        fails.append(f"a) expected slack-bridge reported restarted, got {restarted}")
    return fails


def case_b_supervised_stale_kickstarts() -> list[str]:
    fails = []
    host = Host(job_registered=True, wrapper_alive=True, child_pids="555")
    ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok:
        fails.append(f"b) supervised stale restart reported failure: {how}")
    if not host.of("kickstart"):
        fails.append("b) stale path never kickstarted the supervisor")
    if host.bridge_spawns() or host.of("kill") or host.of("evict"):
        fails.append(f"b) kickstart success took extra side effects: {host.events}")
    return fails


def case_c_kickstart_refused_kills_confirmed_child_only() -> list[str]:
    fails = []
    host = Host(job_registered=True, kickstart_rc=1, wrapper_alive=True,
                child_pids="555", bare_pids=("777",))
    ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok:
        fails.append(f"c) child-kill fallback reported failure: {how}")
    if host.of("kill") != ["555"]:
        fails.append(f"c) expected ONLY the wrapper's child (555) killed, got {host.of('kill')}")
    if host.bridge_spawns():
        fails.append(f"c) fallback hand-spawned despite supervisor: {host.bridge_spawns()}")
    if "confirmed" not in how:
        fails.append(f"c) success must state the kill was confirmed, got {how!r}")
    return fails


def case_d_inactive_job_with_bare_pid_reports_failure() -> list[str]:
    """kewei's re-review control: a registered-but-inactive job plus a bare
    same-name pid must not kill the bare pid and must not claim recovery."""
    fails = []
    host = Host(job_registered=True, kickstart_rc=1, wrapper_alive=False,
                bare_pids=("777",))
    ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge"))
    if ok:
        fails.append("d) restart must report failure when the supervisor cannot be driven")
    if host.of("kill"):
        fails.append(f"d) a bare pid was signalled: {host.of('kill')}")
    if "kickstart" not in how:
        fails.append(f"d) failure message must name the manual remedy, got {how!r}")
    if host.bridge_spawns():
        fails.append(f"d) hand-spawned as a fallback despite supervisor: {host.bridge_spawns()}")
    # Gateway has no wrapper child to fall back to: kickstart-or-refuse.
    hostg = Host(job_registered=True, kickstart_rc=1)
    okg, howg = with_host(hostg, lambda: hc._restart_bridge("gateway-bridge"))
    if okg or "kickstart" not in howg:
        fails.append(f"d) gateway fallback must refuse: ok={okg} how={howg!r}")
    fails += no_side_effects(hostg, "d/gateway")
    # A live wrapper with no bridge child yet (respawn window) → refuse.
    hostw = Host(job_registered=True, kickstart_rc=1, wrapper_alive=True)
    okw, _ = with_host(hostw, lambda: hc._restart_bridge("slack-bridge"))
    if okw:
        fails.append("d) childless wrapper yet the restart claimed success")
    fails += no_side_effects(hostw, "d/childless")
    return fails


def case_e_wrapper_process_counts_as_supervisor() -> list[str]:
    fails = []
    host = Host(job_registered=False, kickstart_rc=1, wrapper_alive=True,
                child_pids="555")
    ok, _how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok:
        fails.append("e) wrapper-supervised restart reported failure")
    if host.bridge_spawns():
        fails.append(f"e) hand-spawned despite a live wrapper process: {host.bridge_spawns()}")
    if host.of("kill") != ["555"]:
        fails.append(f"e) expected the wrapper's child killed for keepalive respawn, got {host.of('kill')}")
    return fails


def case_f_absent_supervision_evicts_then_spawns_in_order() -> list[str]:
    fails = []
    host = Host(job_registered=False, wrapper_alive=False)
    ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok:
        fails.append(f"f) unsupervised restart reported failure: {how}")
    kinds = [k for k, _ in host.events if k in ("evict", "spawn")]
    if kinds != ["evict", "spawn"]:
        fails.append(f"f) expected exactly [evict, spawn] in that order, got {host.events}")
    evicts = host.of("evict")
    if evicts and not evicts[0].startswith("slack:"):
        fails.append(f"f) eviction must target this channel + this repo, got {evicts}")
    if host.of("kill"):
        fails.append(f"f) stale leg must delegate the kill to evict-own-bridge.sh, got {host.of('kill')}")
    # Down (non-stale) leg: no eviction, one spawn.
    host2 = Host(job_registered=False, wrapper_alive=False)
    ok2, _ = with_host(host2, lambda: hc._restart_bridge("slack-bridge"))
    if not ok2 or [k for k, _ in host2.events if k in ("evict", "spawn")] != ["spawn"]:
        fails.append(f"f) down leg changed: ok={ok2} events={host2.events}")
    # Gateway (no wrapper channel) skips the pre-kill entirely.
    host3 = Host(job_registered=False)
    ok3, _ = with_host(host3, lambda: hc._restart_bridge("gateway-bridge", stale=True))
    if not ok3 or host3.of("evict") or len(host3.bridge_spawns()) != 1:
        fails.append(f"f) gateway stale leg: ok={ok3} events={host3.events}")
    # A failing eviction helper must not abort the restart decision.
    host4 = Host(job_registered=False, evict_raises=True)
    ok4, _ = with_host(host4, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok4 or len(host4.bridge_spawns()) != 1:
        fails.append(f"f) evict-raise leg: ok={ok4} events={host4.events}")
    return fails


def case_g_unknown_supervision_fails_closed() -> list[str]:
    """A probe error is not a license to spawn beside a possible supervisor."""
    fails = []
    # Both probes raise → UNKNOWN → refuse everything.
    host = Host(print_raises=True, pgrep_raises=True)
    ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok or "UNKNOWN" not in how:
        fails.append(f"g) probe-raise must refuse with an UNKNOWN verdict: ok={ok} how={how!r}")
    fails += no_side_effects(host, "g/raise-both")
    # launchctl raises; the live wrapper is still a supervision witness, and a
    # raising kickstart falls back to the confirmed child-kill.
    host2 = Host(print_raises=True, kickstart_raises=True,
                 wrapper_alive=True, child_pids="555")
    ok2, _ = with_host(host2, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok2 or host2.of("kill") != ["555"] or host2.bridge_spawns():
        fails.append(f"g) wrapper-witness leg: ok={ok2} events={host2.events}")
    # Gateway has no wrapper witness: launchctl failure alone is UNKNOWN.
    host3 = Host(print_raises=True)
    ok3, how3 = with_host(host3, lambda: hc._restart_bridge("gateway-bridge"))
    if ok3 or "UNKNOWN" not in how3:
        fails.append(f"g) gateway probe-raise must refuse: ok={ok3} how={how3!r}")
    fails += no_side_effects(host3, "g/gateway")
    # launchctl ran (no job) but pgrep hard-errors → still UNKNOWN.
    host4 = Host(job_registered=False, pgrep_rc_error=True)
    ok4, how4 = with_host(host4, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok4 or "UNKNOWN" not in how4:
        fails.append(f"g) pgrep-error must refuse: ok={ok4} how={how4!r}")
    fails += no_side_effects(host4, "g/pgrep-error")
    return fails


def case_h_unconfirmed_kills_report_failure() -> list[str]:
    fails = []
    # Signal refused (kill exits 1) → failure, not recovery.
    host = Host(job_registered=True, kickstart_rc=1, wrapper_alive=True,
                child_pids="555", kill_rc=1)
    ok, _ = with_host(host, lambda: hc._restart_bridge("slack-bridge"))
    if ok:
        fails.append("h) every kill failed yet the restart claimed success")
    if host.bridge_spawns():
        fails.append(f"h) failed kill led to a hand-spawn: {host.bridge_spawns()}")
    # Signal accepted but the child never exits (kill -0 keeps succeeding).
    host2 = Host(job_registered=True, kickstart_rc=1, wrapper_alive=True,
                 child_pids="555", alive_after_kill=True)
    ok2, _ = with_host(host2, lambda: hc._restart_bridge("slack-bridge"))
    if ok2:
        fails.append("h) child survived TERM yet the restart claimed success")
    if host2.bridge_spawns():
        fails.append(f"h) surviving child led to a hand-spawn: {host2.bridge_spawns()}")
    # Negative control (kewei): lineage lookup unreadable → NO signal is sent.
    host3 = Host(job_registered=True, kickstart_rc=1, wrapper_alive=True,
                 child_pids="555", ps_rc_error=True)
    ok3, _ = with_host(host3, lambda: hc._restart_bridge("slack-bridge"))
    if ok3:
        fails.append("h) unreadable lineage yet the restart claimed success")
    if host3.of("kill"):
        fails.append(f"h) unreadable lineage still signalled a pid: {host3.of('kill')}")
    if host3.bridge_spawns():
        fails.append(f"h) unreadable lineage led to a hand-spawn: {host3.bridge_spawns()}")
    host4 = Host(job_registered=True, kickstart_rc=1, wrapper_alive=True,
                 child_pids="555", ps_raises=True)
    ok4, _ = with_host(host4, lambda: hc._restart_bridge("slack-bridge"))
    if ok4 or host4.of("kill") or host4.bridge_spawns():
        fails.append(f"h) ps-raise leg: ok={ok4} events={host4.events}")
    return fails


def case_i_plan_and_spawn_failures_report_failure() -> list[str]:
    fails = []
    host = Host(job_registered=False, wrapper_alive=False)
    with mock.patch.object(hc, "_bridge_launch_plan", return_value=None):
        ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok or "restart skipped" not in how:
        fails.append(f"i) plan-None leg: ok={ok} how={how!r}")
    fails += no_side_effects(host, "i/plan-none")
    host2 = Host(job_registered=False, wrapper_alive=False)
    with mock.patch.object(hc, "_launch_bridge", return_value=False):
        ok2, how2 = with_host(host2, lambda: hc._restart_bridge("slack-bridge"))
    if ok2 or how2 != "spawn failed":
        fails.append(f"i) spawn-failed leg: ok={ok2} how={how2!r}")
    return fails


def main() -> int:
    cases = [
        case_a_supervised_down_restarts_through_launchd,
        case_b_supervised_stale_kickstarts,
        case_c_kickstart_refused_kills_confirmed_child_only,
        case_d_inactive_job_with_bare_pid_reports_failure,
        case_e_wrapper_process_counts_as_supervisor,
        case_f_absent_supervision_evicts_then_spawns_in_order,
        case_g_unknown_supervision_fails_closed,
        case_h_unconfirmed_kills_report_failure,
        case_i_plan_and_spawn_failures_report_failure,
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
