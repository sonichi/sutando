#!/usr/bin/env python3
"""
Regression tests: the bridge restart path never spawns a bridge process
directly while a launchd supervisor owns that bridge — and never treats an
unreadable probe, a foreign checkout's process, an unowned pid, an unconfirmed
kill, or an unverified eviction as success.

Incidents (2026-08-28 and 2026-08-29, same shape): a git pull made the on-disk
slack-bridge code newer than the running process, health-check's stale-restart
path killed the running bridge — which was com.sutando.slack-bridge's wrapper
child — and hand-spawned a replacement via Popen. The orphan (ppid 1) took the
singleton lock; the wrapper's keepalive respawns then lost the lock every ~10s
forever, one owner alert per cycle, until the orphan was killed by hand.

Guards:

  a) supervisor registered in launchd + bridge down → fix_down_bridges restarts
     THROUGH launchd (`kickstart -k`) and Popen never receives a bridge argv.
     This is the incident witness: at the merge base the bridge is hand-spawned
     here, so this case fails there and passes at HEAD.
  b) supervised + stale → kickstart; no direct kill, spawn, or eviction.
  c) two-checkout control: our wrapper (child 555) + a foreign checkout's
     wrapper (child 888) + a bare pid 777 → ONLY 555 is signalled, confirmed
     exited; never 888 or 777, never a spawn.
  d) a supervisor that cannot be driven (inactive job, childless wrapper,
     gateway with no wrapper) reports failure; nothing is signalled or spawned.
  e) this checkout's wrapper alone (no launchd job) supervises: no kickstart
     of an unregistered label, child-kill only.
  f) supervision conclusively absent → stale eviction goes through
     evict-own-bridge.sh AND is verified (survivor scan clean) BEFORE the one
     direct spawn, on a single ordered event stream; down leg spawns without
     eviction; a foreign-checkout survivor does not block our spawn.
  g) UNKNOWN fails CLOSED: probe exceptions, launchctl rc=2 (completed error,
     NOT rc=113 not-found), and pgrep hard errors all refuse to spawn, kill,
     or evict — for slack and gateway alike.
  h) FOREIGN fails CLOSED: a same-name launchd job or wrapper owned by another
     checkout is never kickstarted, killed under, or spawned beside.
  i) a kill whose signal fails, whose target survives, or whose lineage lookup
     is unreadable is reported as failure with NO signal sent where unproven.
  j) an eviction that fails (helper rc != 0, helper unrunnable, survivor still
     alive, survivor scan unprovable, or gateway's no-path) refuses the spawn
     and claims no recovery; plan-None and failed-spawn legs report failure.

Run: python3 tests/health-check-supervised-bridge-restart.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent

# Isolate host config BEFORE exec_module: module-level path resolution must see
# a seeded temp CLAUDE_CONFIG_DIR, never this operator's real channel files.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-hc-supervised-restart-")
_slack_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_slack_cfg.mkdir(parents=True, exist_ok=True)
(_slack_cfg / "access.json").write_text('{"allowFrom": []}')
_discord_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_discord_cfg.mkdir(parents=True, exist_ok=True)
(_discord_cfg / "access.json").write_text('{"allowFrom": []}')
_telegram_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "telegram"
_telegram_cfg.mkdir(parents=True, exist_ok=True)
(_telegram_cfg / "access.json").write_text('{"allowFrom": []}')

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

LABEL = "com.sutando.slack-bridge"
OUR_WRAPPER = "4242"
FOREIGN_WRAPPER = "9999"
DOWN = {"name": "slack-bridge", "status": "warn", "detail": "configured but not running"}


class Host:
    """Fake subprocess boundary: launchctl/pgrep/ps/lsof/kill answered from
    config, Popen recorded. Side effects land on ONE ordered `events` stream so
    cases can assert ordering, not just presence."""

    def __init__(self, *, job="ours", print_rc=None, print_raises=False,
                 kickstart_rc=0, kickstart_raises=False,
                 wrapper_alive=False, foreign_wrapper_alive=False,
                 child_pids="", foreign_child_pids="", bare_pids=(),
                 pgrep_raises=False, pgrep_rc_error=False,
                 ps_rc_error=False, ps_raises=False,
                 evict_rc=0, evict_raises=False,
                 survivors=(), script_pgrep_raises=False, script_pgrep_rc_error=False,
                 ghost_wrapper=False, ghost_survivor=False, lsof_raises=False,
                 ps_fail_after=None, kill_rc=0, kill_raises=False,
                 alive_after_kill=False):
        # job: "ours" (registered, program under REPO_DIR), "foreign"
        # (registered elsewhere), "absent" (rc 113); print_rc overrides.
        self.job = job
        self.print_rc = print_rc
        self.print_raises = print_raises
        self.kickstart_rc = kickstart_rc
        self.kickstart_raises = kickstart_raises
        self.wrapper_alive = wrapper_alive
        self.foreign_wrapper_alive = foreign_wrapper_alive
        self.child_pids = [p for p in child_pids.split() if p]
        self.foreign_child_pids = [p for p in foreign_child_pids.split() if p]
        self.bare_pids = list(bare_pids)
        self.pgrep_raises = pgrep_raises
        self.pgrep_rc_error = pgrep_rc_error
        self.ps_rc_error = ps_rc_error
        self.ps_raises = ps_raises
        self.evict_rc = evict_rc
        self.evict_raises = evict_raises
        # survivors: (pid, cmd, cwd) rows answered to the post-eviction scan.
        self.survivors = list(survivors)
        self.script_pgrep_raises = script_pgrep_raises
        self.script_pgrep_rc_error = script_pgrep_rc_error
        self.ghost_wrapper = ghost_wrapper
        self.ghost_survivor = ghost_survivor
        self.lsof_raises = lsof_raises
        self.ps_fail_after = ps_fail_after  # ps calls answered before failing
        self.ps_calls = 0
        self.kill_rc = kill_rc
        self.kill_raises = kill_raises
        self.alive_after_kill = alive_after_kill
        self.events = []  # ordered: ("kickstart"|"kill"|"evict"|"spawn", detail)

    def _ps_rows(self):
        rows = []
        if self.wrapper_alive:
            rows.append((OUR_WRAPPER, "1",
                         f"/bin/bash {hc.REPO_DIR}/src/launchd/channel-bridge-wrapper.sh slack"))
            for pid in self.child_pids:
                rows.append((pid, OUR_WRAPPER, f"/usr/bin/python3 {hc.REPO_DIR}/src/slack-bridge.py"))
        if self.foreign_wrapper_alive:
            rows.append((FOREIGN_WRAPPER, "1",
                         "/bin/bash /repo-b/src/launchd/channel-bridge-wrapper.sh slack"))
            for pid in self.foreign_child_pids:
                rows.append((pid, FOREIGN_WRAPPER, "/usr/bin/python3 /repo-b/src/slack-bridge.py"))
        for pid in self.bare_pids:
            rows.append((pid, "1", "/usr/bin/python3 /elsewhere/src/slack-bridge.py"))
        for pid, cmd, _cwd in self.survivors:
            rows.append((pid, "1", cmd))
        return rows

    def run(self, argv, *args, **kwargs):
        if not isinstance(argv, list):
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:2] == ["/bin/launchctl", "print"]:
            if self.print_raises:
                raise OSError("launchctl unavailable")
            if self.print_rc is not None:
                return subprocess.CompletedProcess(argv, self.print_rc, stdout="", stderr="")
            if self.job == "ours":
                out = (f"state = running\n\t\t/bin/bash\n"
                       f"\t\t{hc.REPO_DIR}/src/launchd/channel-bridge-wrapper.sh slack\n")
                return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
            if self.job == "foreign":
                out = ("state = running\n\t\t/bin/bash\n"
                       "\t\t/repo-b/src/launchd/channel-bridge-wrapper.sh slack\n")
                return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
            return subprocess.CompletedProcess(argv, 113, stdout="", stderr="")
        if argv[:3] == ["/bin/launchctl", "kickstart", "-k"]:
            self.events.append(("kickstart", argv[3]))
            if self.kickstart_raises:
                raise subprocess.TimeoutExpired(argv, 15)
            return subprocess.CompletedProcess(argv, self.kickstart_rc, stdout="", stderr="")
        if argv[:2] == ["/usr/bin/pgrep", "-f"]:
            if "wrapper" in argv[2]:
                if self.pgrep_raises:
                    raise OSError("pgrep unavailable")
                if self.pgrep_rc_error:
                    return subprocess.CompletedProcess(argv, 2, stdout="", stderr="pgrep: bad")
                pids = ([OUR_WRAPPER] if self.wrapper_alive else []) + \
                       ([FOREIGN_WRAPPER] if self.foreign_wrapper_alive else []) + \
                       (["4343"] if self.ghost_wrapper else [])
                rc = 0 if pids else 1
                return subprocess.CompletedProcess(argv, rc, stdout="\n".join(pids), stderr="")
            # post-eviction survivor scan (<script>.py$)
            if self.script_pgrep_raises:
                raise OSError("pgrep unavailable")
            if self.script_pgrep_rc_error:
                return subprocess.CompletedProcess(argv, 3, stdout="", stderr="pgrep: bad")
            pids = [p for p, _c, _w in self.survivors] + \
                   (["670"] if self.ghost_survivor else [])
            rc = 0 if pids else 1
            return subprocess.CompletedProcess(argv, rc, stdout="\n".join(pids), stderr="")
        if argv[0] in ("ps", "/bin/ps"):
            self.ps_calls += 1
            if self.ps_fail_after is not None and self.ps_calls > self.ps_fail_after:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="ps: bad")
            if self.ps_raises:
                raise OSError("ps unavailable")
            if self.ps_rc_error:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="ps: bad")
            out = "  PID  PPID ARGS\n" + "\n".join(
                f"{p} {pp} {cmd}" for p, pp, cmd in self._ps_rows())
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
        if argv[0] == "/usr/sbin/lsof":
            if self.lsof_raises:
                raise OSError("lsof unavailable")
            pid = argv[argv.index("-p") + 1]
            cwd = next((w for p, _c, w in self.survivors if p == pid), "")
            out = f"p{pid}\nn{cwd}\n" if cwd else ""
            return subprocess.CompletedProcess(argv, 0 if cwd else 1, stdout=out, stderr="")
        if argv[0] == "/bin/kill" and argv[1] == "-0":
            alive = self.alive_after_kill
            return subprocess.CompletedProcess(argv, 0 if alive else 1, stdout="", stderr="")
        if argv[0] == "/bin/kill":
            self.events.append(("kill", argv[1]))
            if self.kill_raises:
                raise OSError("kill unavailable")
            return subprocess.CompletedProcess(argv, self.kill_rc, stdout="", stderr="")
        if argv[0] == "/bin/bash" and str(argv[1]).endswith("evict-own-bridge.sh"):
            if self.evict_raises:
                raise OSError("bash unavailable")
            self.events.append(("evict", f"{argv[2]}:{argv[3]}"))
            return subprocess.CompletedProcess(argv, self.evict_rc, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def popen(self, argv, **kwargs):
        self.events.append(("spawn", argv))
        return mock.MagicMock()

    def of(self, kind):
        return [d for k, d in self.events if k == kind]

    def bridge_spawns(self):
        return [a for a in self.of("spawn") if any(str(t).endswith("-bridge.py") for t in a)]


def with_host(host, fn):
    env = {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_APP_TOKEN": "xapp-test",
           "REMOTE_TASK_TOKEN": "gw-test"}
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(hc, "WORKSPACE_DIR", Path(td)), \
             mock.patch.object(hc, "_bridge_interpreter", return_value="python3"), \
             mock.patch.object(hc, "_load_channel_env", return_value=env), \
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
    """The incident witness: fails at the merge base (hand-spawn), passes at
    HEAD (kickstart of this checkout's job, no spawn)."""
    fails = []
    host = Host(job="ours")
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
    host = Host(job="ours", wrapper_alive=True, child_pids="555")
    ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok:
        fails.append(f"b) supervised stale restart reported failure: {how}")
    if not host.of("kickstart"):
        fails.append("b) stale path never kickstarted the supervisor")
    if host.bridge_spawns() or host.of("kill") or host.of("evict"):
        fails.append(f"b) kickstart success took extra side effects: {host.events}")
    return fails


def case_c_two_checkouts_only_our_child_is_killed() -> list[str]:
    """kewei's round-2 control: wrappers in two checkouts. Only THIS repo's
    wrapper's child may be signalled; the foreign child and a bare pid never."""
    fails = []
    host = Host(job="ours", kickstart_rc=1, wrapper_alive=True, child_pids="555",
                foreign_wrapper_alive=True, foreign_child_pids="888",
                bare_pids=("777",))
    ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok:
        fails.append(f"c) child-kill fallback reported failure: {how}")
    if host.of("kill") != ["555"]:
        fails.append(f"c) expected ONLY this checkout's child (555) killed, got {host.of('kill')}")
    if host.bridge_spawns():
        fails.append(f"c) fallback hand-spawned despite supervisor: {host.bridge_spawns()}")
    if "confirmed" not in how:
        fails.append(f"c) success must state the kill was confirmed, got {how!r}")
    # A RAISING kickstart falls back to the same lineage-bound child-kill.
    host2 = Host(job="ours", kickstart_raises=True, wrapper_alive=True, child_pids="555")
    ok2, _ = with_host(host2, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok2 or host2.of("kill") != ["555"] or host2.bridge_spawns():
        fails.append(f"c) kickstart-raise leg: ok={ok2} events={host2.events}")
    return fails


def case_d_undrivable_supervisor_reports_failure() -> list[str]:
    fails = []
    # Registered-but-inactive job + a bare same-name pid: nothing signalled.
    host = Host(job="ours", kickstart_rc=1, bare_pids=("777",))
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
    hostg = Host(job="ours", kickstart_rc=1)
    okg, howg = with_host(hostg, lambda: hc._restart_bridge("gateway-bridge"))
    if okg or "kickstart" not in howg:
        fails.append(f"d) gateway fallback must refuse: ok={okg} how={howg!r}")
    fails += no_side_effects(hostg, "d/gateway")
    # A live wrapper with no bridge child yet (respawn window) → refuse.
    hostw = Host(job="ours", kickstart_rc=1, wrapper_alive=True)
    okw, _ = with_host(hostw, lambda: hc._restart_bridge("slack-bridge"))
    if okw:
        fails.append("d) childless wrapper yet the restart claimed success")
    fails += no_side_effects(hostw, "d/childless")
    return fails


def case_e_wrapper_only_supervision_never_kickstarts() -> list[str]:
    """No registered job, but THIS checkout's wrapper is alive: supervised —
    drive it via its child; never kickstart an unregistered label."""
    fails = []
    host = Host(job="absent", wrapper_alive=True, child_pids="555")
    ok, _how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok:
        fails.append("e) wrapper-supervised restart reported failure")
    if host.of("kickstart"):
        fails.append(f"e) kickstarted an unregistered label: {host.of('kickstart')}")
    if host.bridge_spawns():
        fails.append(f"e) hand-spawned despite a live wrapper process: {host.bridge_spawns()}")
    if host.of("kill") != ["555"]:
        fails.append(f"e) expected the wrapper's child killed for keepalive respawn, got {host.of('kill')}")
    return fails


def case_f_absent_supervision_verified_evict_then_spawn() -> list[str]:
    fails = []
    host = Host(job="absent")
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
    host2 = Host(job="absent")
    ok2, _ = with_host(host2, lambda: hc._restart_bridge("slack-bridge"))
    if not ok2 or [k for k, _ in host2.events if k in ("evict", "spawn")] != ["spawn"]:
        fails.append(f"f) down leg changed: ok={ok2} events={host2.events}")
    # A foreign checkout's absolute-path survivor is not ours: spawn proceeds.
    host3 = Host(job="absent", survivors=[("666", "/usr/bin/python3 /repo-b/src/slack-bridge.py", "/repo-b")])
    ok3, _ = with_host(host3, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok3 or len(host3.bridge_spawns()) != 1:
        fails.append(f"f) foreign survivor wrongly blocked our spawn: ok={ok3} events={host3.events}")
    # A relative-launch survivor whose cwd is elsewhere is not ours either.
    host4 = Host(job="absent", survivors=[("667", "python3 src/slack-bridge.py", "/repo-b")])
    ok4, _ = with_host(host4, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok4 or len(host4.bridge_spawns()) != 1:
        fails.append(f"f) foreign-cwd survivor wrongly blocked our spawn: ok={ok4} events={host4.events}")
    # A pgrep hit that exits before ps reads it counts as gone, not unprovable.
    host5 = Host(job="absent", ghost_survivor=True)
    ok5, _ = with_host(host5, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok5 or len(host5.bridge_spawns()) != 1:
        fails.append(f"f) vanished pgrep hit wrongly blocked our spawn: ok={ok5} events={host5.events}")
    return fails


def case_g_unknown_supervision_fails_closed() -> list[str]:
    """A probe error — raised OR completed-with-error — is not absence."""
    fails = []
    # Both probes raise → UNKNOWN → refuse everything.
    host = Host(print_raises=True, pgrep_raises=True)
    ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok or "UNKNOWN" not in how:
        fails.append(f"g) probe-raise must refuse with an UNKNOWN verdict: ok={ok} how={how!r}")
    fails += no_side_effects(host, "g/raise-both")
    # kewei's round-2 control: launchctl COMPLETES with rc=2 (not 113) while
    # pgrep runs clean — a completed error is not conclusive absence.
    host2 = Host(print_rc=2)
    ok2, how2 = with_host(host2, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok2 or "UNKNOWN" not in how2:
        fails.append(f"g) launchctl rc=2 must refuse: ok={ok2} how={how2!r}")
    fails += no_side_effects(host2, "g/rc2")
    hostg = Host(print_rc=2)
    okg, howg = with_host(hostg, lambda: hc._restart_bridge("gateway-bridge"))
    if okg or "UNKNOWN" not in howg:
        fails.append(f"g) gateway launchctl rc=2 must refuse: ok={okg} how={howg!r}")
    fails += no_side_effects(hostg, "g/gateway-rc2")
    # launchctl raises; the live wrapper is still a supervision witness.
    host3 = Host(print_raises=True, wrapper_alive=True, child_pids="555")
    ok3, _ = with_host(host3, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if not ok3 or host3.of("kill") != ["555"] or host3.bridge_spawns():
        fails.append(f"g) wrapper-witness leg: ok={ok3} events={host3.events}")
    # Gateway has no wrapper witness: a raised probe alone is UNKNOWN.
    host4 = Host(print_raises=True)
    ok4, how4 = with_host(host4, lambda: hc._restart_bridge("gateway-bridge"))
    if ok4 or "UNKNOWN" not in how4:
        fails.append(f"g) gateway probe-raise must refuse: ok={ok4} how={how4!r}")
    # Job conclusively absent but the wrapper pgrep hard-errors → still UNKNOWN.
    host5 = Host(job="absent", pgrep_rc_error=True)
    ok5, how5 = with_host(host5, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok5 or "UNKNOWN" not in how5:
        fails.append(f"g) pgrep-error must refuse: ok={ok5} how={how5!r}")
    fails += no_side_effects(host5, "g/pgrep-error")
    # A wrapper pid ps cannot classify (gone or table torn) → UNKNOWN.
    host6 = Host(job="absent", ghost_wrapper=True)
    ok6, how6 = with_host(host6, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok6 or "UNKNOWN" not in how6:
        fails.append(f"g) unclassifiable wrapper pid must refuse: ok={ok6} how={how6!r}")
    fails += no_side_effects(host6, "g/ghost-wrapper")
    return fails


def case_h_foreign_supervisor_fails_closed() -> list[str]:
    """kewei's round-2 control: a same-name supervisor owned by another
    checkout is never driven, killed under, or spawned beside."""
    fails = []
    # Foreign launchd job: no kickstart of another install's job.
    host = Host(job="foreign")
    ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok or "NOT this" not in how:
        fails.append(f"h) foreign job must refuse: ok={ok} how={how!r}")
    if host.of("kickstart"):
        fails.append(f"h) kickstarted a foreign install's job: {host.of('kickstart')}")
    fails += no_side_effects(host, "h/foreign-job")
    # Foreign wrapper process only (our job absent): refuse, touch nothing.
    host2 = Host(job="absent", foreign_wrapper_alive=True, foreign_child_pids="888")
    ok2, how2 = with_host(host2, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok2 or "NOT this" not in how2:
        fails.append(f"h) foreign wrapper must refuse: ok={ok2} how={how2!r}")
    fails += no_side_effects(host2, "h/foreign-wrapper")
    # Foreign gateway job.
    host3 = Host(job="foreign")
    ok3, _ = with_host(host3, lambda: hc._restart_bridge("gateway-bridge"))
    if ok3 or host3.of("kickstart"):
        fails.append(f"h) foreign gateway job driven: ok={ok3} events={host3.events}")
    return fails


def case_i_unconfirmed_kills_report_failure() -> list[str]:
    fails = []
    # Signal refused (kill exits 1) → failure, not recovery.
    host = Host(job="ours", kickstart_rc=1, wrapper_alive=True,
                child_pids="555", kill_rc=1)
    ok, _ = with_host(host, lambda: hc._restart_bridge("slack-bridge"))
    if ok:
        fails.append("i) every kill failed yet the restart claimed success")
    if host.bridge_spawns():
        fails.append(f"i) failed kill led to a hand-spawn: {host.bridge_spawns()}")
    # Signal accepted but the child never exits (kill -0 keeps succeeding).
    host2 = Host(job="ours", kickstart_rc=1, wrapper_alive=True,
                 child_pids="555", alive_after_kill=True)
    ok2, _ = with_host(host2, lambda: hc._restart_bridge("slack-bridge"))
    if ok2:
        fails.append("i) child survived TERM yet the restart claimed success")
    # Lineage lookup unreadable → NO signal is sent (negative control).
    for label, kw in (("ps-error", {"ps_rc_error": True}), ("ps-raise", {"ps_raises": True})):
        host3 = Host(job="ours", kickstart_rc=1, wrapper_alive=True,
                     child_pids="555", **kw)
        ok3, _ = with_host(host3, lambda: hc._restart_bridge("slack-bridge"))
        if ok3:
            fails.append(f"i) {label}: unreadable lineage yet the restart claimed success")
        if host3.of("kill"):
            fails.append(f"i) {label}: unreadable lineage still signalled a pid: {host3.of('kill')}")
        if host3.bridge_spawns():
            fails.append(f"i) {label}: unreadable lineage led to a hand-spawn: {host3.bridge_spawns()}")
    # kill itself raising is a reported failure, not a crash or a success.
    host4 = Host(job="ours", kickstart_rc=1, wrapper_alive=True,
                 child_pids="555", kill_raises=True)
    ok4, _ = with_host(host4, lambda: hc._restart_bridge("slack-bridge"))
    if ok4 or host4.bridge_spawns():
        fails.append(f"i) kill-raise leg: ok={ok4} events={host4.events}")
    # ps readable for wrapper classification but torn before the child lookup.
    host5 = Host(job="ours", kickstart_rc=1, wrapper_alive=True,
                 child_pids="555", ps_fail_after=1)
    ok5, _ = with_host(host5, lambda: hc._restart_bridge("slack-bridge"))
    if ok5 or host5.of("kill") or host5.bridge_spawns():
        fails.append(f"i) torn-ps leg: ok={ok5} events={host5.events}")
    return fails


def case_j_unverified_eviction_refuses_spawn() -> list[str]:
    """kewei's round-2 control set: no Popen and no success claim unless the
    eviction is verified end-to-end."""
    fails = []
    for label, kw, needle in (
        ("helper-rc", {"evict_rc": 7}, "exited 7"),
        ("helper-unrunnable", {"evict_raises": True}, "could not be run"),
        ("our-survivor", {"survivors": [("666", f"/usr/bin/python3 {hc.REPO_DIR}/src/slack-bridge.py", "")]},
         "still alive"),
        ("our-cwd-survivor", {"survivors": [("667", "python3 src/slack-bridge.py", str(hc.REPO_DIR))]},
         "still alive"),
        ("scan-unprovable", {"survivors": [("668", "python3 src/slack-bridge.py", "")]},
         "could not prove"),
        ("scan-error", {"script_pgrep_raises": True}, "could not prove"),
        ("scan-rc-error", {"script_pgrep_rc_error": True}, "could not prove"),
        ("scan-ps-error", {"survivors": [("666", "python3 src/slack-bridge.py", "x")], "ps_fail_after": 0},
         "could not prove"),
        ("lsof-raise", {"survivors": [("667", "python3 src/slack-bridge.py", str(hc.REPO_DIR)+"IGNORED")],
                        "lsof_raises": True}, "could not prove"),
    ):
        host = Host(job="absent", **kw)
        ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
        if ok:
            fails.append(f"j) {label}: unverified eviction yet the restart claimed success")
        if host.bridge_spawns():
            fails.append(f"j) {label}: unverified eviction still spawned: {host.bridge_spawns()}")
        if needle not in how:
            fails.append(f"j) {label}: failure must say why, got {how!r}")
    # Gateway stale with no supervisor: no eviction path → refuse, no spawn.
    hostg = Host(job="absent")
    okg, howg = with_host(hostg, lambda: hc._restart_bridge("gateway-bridge", stale=True))
    if okg or "no eviction path" not in howg:
        fails.append(f"j) gateway stale must refuse: ok={okg} how={howg!r}")
    fails += no_side_effects(hostg, "j/gateway-stale")
    # Gateway DOWN (not stale) with no supervisor still spawns.
    hostg2 = Host(job="absent")
    okg2, _ = with_host(hostg2, lambda: hc._restart_bridge("gateway-bridge"))
    if not okg2 or len(hostg2.bridge_spawns()) != 1:
        fails.append(f"j) gateway down leg changed: ok={okg2} events={hostg2.events}")
    # Plan-None and failed-spawn legs report failure without side effects.
    host2 = Host(job="absent")
    with mock.patch.object(hc, "_bridge_launch_plan", return_value=None):
        ok2, how2 = with_host(host2, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok2 or "restart skipped" not in how2:
        fails.append(f"j) plan-None leg: ok={ok2} how={how2!r}")
    fails += no_side_effects(host2, "j/plan-none")
    host3 = Host(job="absent")
    with mock.patch.object(hc, "_launch_bridge", return_value=False):
        ok3, how3 = with_host(host3, lambda: hc._restart_bridge("slack-bridge"))
    if ok3 or how3 != "spawn failed":
        fails.append(f"j) spawn-failed leg: ok={ok3} how={how3!r}")
    return fails


def main() -> int:
    cases = [
        case_a_supervised_down_restarts_through_launchd,
        case_b_supervised_stale_kickstarts,
        case_c_two_checkouts_only_our_child_is_killed,
        case_d_undrivable_supervisor_reports_failure,
        case_e_wrapper_only_supervision_never_kickstarts,
        case_f_absent_supervision_verified_evict_then_spawn,
        case_g_unknown_supervision_fails_closed,
        case_h_foreign_supervisor_fails_closed,
        case_i_unconfirmed_kills_report_failure,
        case_j_unverified_eviction_refuses_spawn,
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
