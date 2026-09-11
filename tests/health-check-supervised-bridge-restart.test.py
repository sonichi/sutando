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
  c) kickstart refused in a PURE-ours state → only OUR wrapper's child (555)
     is signalled, ESRCH-confirmed exited; a bare pid (777) never. (The
     two-checkout MIXED state refuses outright — case k.)
  d) a supervisor that cannot be driven (inactive job, childless wrapper,
     gateway with no wrapper) reports failure; nothing is signalled or spawned.
  e) this checkout's wrapper alone (no launchd job) supervises: no kickstart
     of an unregistered label, child-kill only.
  f) supervision conclusively absent → stale eviction AND its survivor
     verification both go through evict-own-bridge.sh (the ONE identity
     owner; `--list` mode) before the one direct spawn, on a single ordered
     event stream; down leg spawns without eviction.
  g) UNKNOWN fails CLOSED: probe exceptions, launchctl rc=2 (completed error,
     NOT rc=113 not-found), pgrep hard errors, and an unknowable job beside a
     live wrapper all refuse to spawn, kill, or evict — slack and gateway.
  h) FOREIGN fails CLOSED: a same-name launchd job or wrapper owned by another
     checkout is never kickstarted, killed under, or spawned beside — including
     a foreign EXECUTED wrapper carrying our path as a later argument.
  i) a kill whose signal fails, whose target survives, or whose lineage lookup
     is unreadable is reported as failure with NO signal sent where unproven.
  j) an eviction that fails (helper rc != 0, helper unrunnable, an OWN
     survivor listed, any INDETERMINATE line, an erroring/unrunnable --list,
     or gateway's no-path) refuses the spawn and claims no recovery; the
     plan-None and real-Popen-failure legs report failure.
  k) the FULL job×wrapper ownership matrix: every own/foreign mix refuses
     with zero side effects; pure states act as advertised.
  l) a repo path containing spaces owns its wrapper (ps flattens argv, so
     identity is whole-command equality, never tokenization); same-prefix
     and embedded-path argvs stay foreign.

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


def job_dump(repo: str, *, gateway=False, extra=""):
    """A realistic `launchctl print` dump whose ownership lives ONLY in the
    arguments block, exactly like the real output."""
    wrapper = ("gateway-bridge-wrapper.sh" if gateway else "channel-bridge-wrapper.sh")
    chan = "" if gateway else "\t\tslack\n"
    return ("gui/501/com.sutando.x = {\n"
            "\tstate = running\n"
            "\tprogram = /bin/bash\n"
            "\targuments = {\n"
            "\t\t/bin/bash\n"
            f"\t\t{repo}/src/launchd/{wrapper}\n"
            f"{chan}"
            "\t}\n"
            f"{extra}"
            "}\n")


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
                 evict_rc=0, evict_raises=False, job_stdout=None,
                 popen_raises=False, kill0_eperm=False,
                 list_lines=(), list_rc=0, list_raises=False,
                 ghost_wrapper=False,
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
        self.job_stdout = job_stdout  # overrides the launchctl print dump
        self.popen_raises = popen_raises
        self.kill0_eperm = kill0_eperm
        self.list_lines = list(list_lines)  # evict-own-bridge.sh --list answers
        self.list_rc = list_rc
        self.list_raises = list_raises
        self.ghost_wrapper = ghost_wrapper
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
        return rows

    def run(self, argv, *args, **kwargs):
        if not isinstance(argv, list):
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:2] == ["/bin/launchctl", "print"]:
            if self.print_raises:
                raise OSError("launchctl unavailable")
            if self.print_rc is not None:
                return subprocess.CompletedProcess(argv, self.print_rc, stdout="", stderr="")
            if self.job_stdout is not None:
                return subprocess.CompletedProcess(argv, 0, stdout=self.job_stdout, stderr="")
            gateway = "gateway" in argv[2]
            if self.job == "ours":
                out = job_dump(str(hc.REPO_DIR), gateway=gateway)
                return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
            if self.job == "foreign":
                out = job_dump("/repo-b", gateway=gateway)
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
            # No production caller pgreps the bridge script any more (the
            # survivor scan delegates to evict-own-bridge.sh --list).
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
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
        if argv[0] == "/bin/kill" and argv[1] == "-0":
            if self.kill0_eperm:
                return subprocess.CompletedProcess(argv, 1, stdout="",
                                                   stderr="kill: Operation not permitted")
            alive = self.alive_after_kill
            return subprocess.CompletedProcess(argv, 0 if alive else 1, stdout="", stderr="")
        if argv[0] == "/bin/kill":
            self.events.append(("kill", argv[1]))
            if self.kill_raises:
                raise OSError("kill unavailable")
            return subprocess.CompletedProcess(argv, self.kill_rc, stdout="", stderr="")
        if argv[0] == "/bin/bash" and str(argv[1]).endswith("evict-own-bridge.sh"):
            if argv[2] == "--list":
                if self.list_raises:
                    raise OSError("bash unavailable")
                self.events.append(("list", f"{argv[3]}:{argv[4]}"))
                return subprocess.CompletedProcess(
                    argv, self.list_rc, stdout="\n".join(self.list_lines), stderr="")
            if self.evict_raises:
                raise OSError("bash unavailable")
            self.events.append(("evict", f"{argv[2]}:{argv[3]}"))
            return subprocess.CompletedProcess(argv, self.evict_rc, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def popen(self, argv, **kwargs):
        self.events.append(("spawn", argv))
        if self.popen_raises:
            raise OSError(24, "EMFILE: too many open files")
        return mock.MagicMock()

    def os_kill(self, pid, sig):
        if sig != 0:
            raise AssertionError(f"unexpected os.kill signal {sig}")
        if self.kill0_eperm:
            raise PermissionError(1, "Operation not permitted")
        if self.alive_after_kill:
            return None
        raise ProcessLookupError(3, "No such process")

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
             mock.patch.object(hc.os, "kill", side_effect=host.os_kill), \
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


def case_c_kickstart_refused_kills_only_our_confirmed_child() -> list[str]:
    """Pure-ours state (no foreign presence anywhere): kickstart refused →
    only OUR wrapper's child is signalled, confirmed exited; a bare pid never.
    (The two-checkout MIXED state now refuses outright — see the matrix case.)"""
    fails = []
    host = Host(job="ours", kickstart_rc=1, wrapper_alive=True, child_pids="555",
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
    # The verify leg goes through the ONE owner too: an empty --list from
    # evict-own-bridge.sh (foreign survivors print nothing) clears the spawn.
    if host.of("list") != [f"slack:{hc.REPO_DIR}"]:
        fails.append(f"f) survivor scan must delegate to evict-own-bridge.sh --list, got {host.events}")
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
    # launchctl raises while our wrapper is alive: the job dimension is
    # unknowable, so the complete-state rule refuses even the child-kill.
    host3 = Host(print_raises=True, wrapper_alive=True, child_pids="555")
    ok3, how3 = with_host(host3, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok3 or "UNKNOWN" not in how3:
        fails.append(f"g) unknowable-job + live wrapper must refuse: ok={ok3} how={how3!r}")
    fails += no_side_effects(host3, "g/unknowable-job-wrapper")
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
    # A registered job whose dump has no parseable arguments block is UNPROVED
    # ownership → UNKNOWN, not foreign and never ours.
    hostu = Host(job_stdout="gui/501/com.sutando.x = {\n\tstate = running\n}\n")
    oku, howu = with_host(hostu, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if oku or "UNKNOWN" not in howu:
        fails.append(f"g) unparseable job dump must be UNKNOWN: ok={oku} how={howu!r}")
    fails += no_side_effects(hostu, "g/unparseable-dump")
    # An empty arguments block, or an interpreter flag before the script,
    # leaves the EXECUTED position undeterminable -> UNKNOWN.
    for label6, dump in (
        ("empty-args", "\targuments = {\n\t}\n"),
        ("interp-flag", "\targuments = {\n\t\t/bin/bash\n\t\t-x\n\t\t/repo-b/x.sh\n\t}\n"),
        ("interp-only", "\targuments = {\n\t\t/bin/bash\n\t}\n"),
    ):
        hostn = Host(job_stdout=f"gui/501/x = {{\n\tstate = running\n{dump}}}\n")
        okn, hown = with_host(hostn, lambda: hc._restart_bridge("slack-bridge", stale=True))
        if okn or "UNKNOWN" not in hown:
            fails.append(f"g) {label6}: undeterminable executed position must be UNKNOWN: ok={okn} how={hown!r}")
        fails += no_side_effects(hostn, f"g/{label6}")
    # An UNTERMINATED arguments block proves nothing either.
    hostt = Host(job_stdout="\targuments = {\n\t\t/bin/bash\n")
    okt, howt = with_host(hostt, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if okt or "UNKNOWN" not in howt:
        fails.append(f"g) unterminated arguments block must be UNKNOWN: ok={okt} how={howt!r}")
    fails += no_side_effects(hostt, "g/unterminated-dump")
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
    # kewei r3 control: a SIBLING checkout sharing this repo's path as a PREFIX
    # (<repo>-copy) is foreign — substring presence of our path must not own it.
    host4 = Host(job_stdout=job_dump(f"{hc.REPO_DIR}-copy"))
    ok4, how4 = with_host(host4, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok4 or "NOT this" not in how4:
        fails.append(f"h) same-prefix sibling job must be foreign: ok={ok4} how={how4!r}")
    if host4.of("kickstart"):
        fails.append(f"h) kickstarted a same-prefix sibling's job: {host4.of('kickstart')}")
    fails += no_side_effects(host4, "h/same-prefix")
    # kewei r3 control: our repo path in an INCIDENTAL field (log path) while
    # the program argument points elsewhere — still foreign.
    host5 = Host(job_stdout=job_dump(
        "/repo-b", extra=f"\tstdout path = {hc.REPO_DIR}/workspace/logs/slack-bridge.log\n"))
    ok5, how5 = with_host(host5, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok5 or "NOT this" not in how5:
        fails.append(f"h) incidental-field mention must not own the job: ok={ok5} how={how5!r}")
    if host5.of("kickstart"):
        fails.append(f"h) kickstarted on an incidental-field match: {host5.of('kickstart')}")
    fails += no_side_effects(host5, "h/incidental-field")
    # kewei r6 control: the EXECUTED wrapper is foreign; ours appears only as
    # a LATER argument (data, not the program) — FOREIGN, zero side effects.
    later_arg_dump = ("gui/501/com.sutando.x = {\n"
                      "\tstate = running\n"
                      "\targuments = {\n"
                      "\t\t/bin/bash\n"
                      "\t\t/repo-b/src/launchd/channel-bridge-wrapper.sh\n"
                      "\t\tslack\n"
                      f"\t\t{hc.REPO_DIR}/src/launchd/channel-bridge-wrapper.sh\n"
                      "\t\tslack\n"
                      "\t}\n}\n")
    own = with_host(Host(job="ours"), lambda: hc._job_is_ours("slack-bridge", job_dump(str(hc.REPO_DIR))))
    frn = with_host(Host(job="ours"), lambda: hc._job_is_ours("slack-bridge", job_dump("/repo-b")))
    lat = with_host(Host(job="ours"), lambda: hc._job_is_ours("slack-bridge", later_arg_dump))
    if (own, frn, lat) != (True, False, False):
        fails.append(f"h) three-probe control: own={own} foreign={frn} later_arg={lat} "
                     f"(expected True/False/False)")
    host7 = Host(job_stdout=later_arg_dump)
    ok7, how7 = with_host(host7, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok7 or "NOT this" not in how7:
        fails.append(f"h) later-arg job must refuse as foreign: ok={ok7} how={how7!r}")
    if host7.of("kickstart"):
        fails.append(f"h) kickstarted a job whose EXECUTED wrapper is foreign: {host7.of('kickstart')}")
    fails += no_side_effects(host7, "h/later-arg")
    # A same-prefix FOREIGN WRAPPER argv token is likewise not ours.
    host6 = Host(job="absent", foreign_wrapper_alive=True, foreign_child_pids="888")
    host6._ps_rows_orig = host6._ps_rows
    def _rows6():
        rows = host6._ps_rows_orig()
        return [(p_, pp, cmd.replace("/repo-b", f"{hc.REPO_DIR}-copy")) for p_, pp, cmd in rows]
    host6._ps_rows = _rows6
    ok6, how6 = with_host(host6, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok6 or "NOT this" not in how6:
        fails.append(f"h) same-prefix wrapper must be foreign: ok={ok6} how={how6!r}")
    fails += no_side_effects(host6, "h/same-prefix-wrapper")
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
    # kewei r3 control: kill -0 EPERM means the pid may still exist and be
    # unprobeable — never a confirmed exit, never a success claim.
    hostp = Host(job="ours", kickstart_rc=1, wrapper_alive=True,
                 child_pids="555", kill0_eperm=True)
    okp, howp = with_host(hostp, lambda: hc._restart_bridge("slack-bridge"))
    if okp:
        fails.append(f"i) EPERM probe read as confirmed exit: how={howp!r}")
    if hostp.bridge_spawns():
        fails.append(f"i) EPERM leg hand-spawned: {hostp.bridge_spawns()}")
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
    # ps readable for the supervision scan + the kill's wrapper re-check, but
    # torn before the child lookup (3rd read) — refuse without signalling.
    host5 = Host(job="ours", kickstart_rc=1, wrapper_alive=True,
                 child_pids="555", ps_fail_after=2)
    ok5, _ = with_host(host5, lambda: hc._restart_bridge("slack-bridge"))
    if ok5 or host5.of("kill") or host5.bridge_spawns():
        fails.append(f"i) torn-ps leg: ok={ok5} events={host5.events}")
    # And torn at the kill's own wrapper re-check (2nd read).
    host6 = Host(job="ours", kickstart_rc=1, wrapper_alive=True,
                 child_pids="555", ps_fail_after=1)
    ok6, _ = with_host(host6, lambda: hc._restart_bridge("slack-bridge"))
    if ok6 or host6.of("kill") or host6.bridge_spawns():
        fails.append(f"i) torn-ps-2nd leg: ok={ok6} events={host6.events}")
    return fails


def case_j_unverified_eviction_refuses_spawn() -> list[str]:
    """kewei's round-2 control set: no Popen and no success claim unless the
    eviction is verified end-to-end."""
    fails = []
    for label, kw, needle in (
        ("helper-rc", {"evict_rc": 7}, "exited 7"),
        ("helper-unrunnable", {"evict_raises": True}, "could not be run"),
        ("own-survivor", {"list_lines": ["OWN 666"]}, "still alive"),
        ("indeterminate-survivor", {"list_lines": ["INDETERMINATE 668"]}, "could not prove"),
        ("mixed-list", {"list_lines": ["OWN 666", "INDETERMINATE 668"]}, "could not prove"),
        ("list-rc-error", {"list_rc": 3}, "could not prove"),
        ("list-unrunnable", {"list_raises": True}, "could not prove"),
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
    # Gateway has no entry in the identity owner: the scan itself is unprovable.
    hostg1 = Host(job="absent")
    if with_host(hostg1, lambda: hc._surviving_own_bridge_pids("gateway-bridge")) is not None:
        fails.append("j) gateway survivor scan must be unprovable (None)")
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
    # kewei r3 control: a REAL Popen EMFILE (no _launch_bridge mock) degrades
    # to a reported failure on BOTH legs instead of aborting the fix pass.
    host3 = Host(job="absent", popen_raises=True)
    ok3, how3 = with_host(host3, lambda: hc._restart_bridge("slack-bridge"))
    if ok3 or "spawn failed" not in how3 or "OSError" not in how3:
        fails.append(f"j) down-leg Popen raise escaped or misreported: ok={ok3} how={how3!r}")
    host4 = Host(job="absent", popen_raises=True)
    ok4, how4 = with_host(host4, lambda: hc._restart_bridge("slack-bridge", stale=True))
    if ok4 or "spawn failed" not in how4 or "OSError" not in how4:
        fails.append(f"j) post-eviction Popen raise escaped or misreported: ok={ok4} how={how4!r}")
    if [k for k, _ in host4.events if k in ("evict", "spawn")] != ["evict", "spawn"]:
        fails.append(f"j) post-eviction raise leg lost its event order: {host4.events}")
    return fails


def case_k_full_ownership_matrix() -> list[str]:
    """kewei r4 blocker 2: the complete job×wrapper state decides the verdict,
    and EVERY own/foreign mix refuses. Verdicts asserted via _bridge_supervision
    directly; the two riskiest mixes also prove refusal at the action layer."""
    fails = []
    matrix = [
        # (job, wrapper_alive(ours), foreign_wrapper, expected verdict, expected label)
        ("ours",    False, False, "supervised", LABEL),
        ("ours",    True,  False, "supervised", LABEL),
        ("ours",    False, True,  "mixed",      None),
        ("ours",    True,  True,  "mixed",      None),
        ("foreign", True,  False, "mixed",      None),
        ("foreign", True,  True,  "mixed",      None),
        ("foreign", False, False, "foreign",    None),
        ("foreign", False, True,  "foreign",    None),
        ("absent",  True,  False, "supervised", None),
        ("absent",  True,  True,  "mixed",      None),
        ("absent",  False, True,  "foreign",    None),
        ("absent",  False, False, "absent",     None),
    ]
    for job, ours_w, foreign_w, want, want_label in matrix:
        host = Host(job=job, wrapper_alive=ours_w, child_pids="555" if ours_w else "",
                    foreign_wrapper_alive=foreign_w,
                    foreign_child_pids="888" if foreign_w else "")
        verdict = with_host(host, lambda: hc._bridge_supervision("slack-bridge"))
        if verdict != (want, want_label):
            fails.append(f"k) job={job} ours_w={ours_w} foreign_w={foreign_w}: "
                         f"expected {(want, want_label)}, got {verdict}")
    # The two riskiest mixes also prove refusal at the action layer, with
    # zero side effects (no kickstart, kill, evict, or spawn).
    for label_, kw in (("own-job+foreign-wrapper",
                        dict(job="ours", foreign_wrapper_alive=True, foreign_child_pids="888")),
                       ("foreign-job+own-wrapper",
                        dict(job="foreign", wrapper_alive=True, child_pids="555"))):
        host = Host(**kw)
        ok, how = with_host(host, lambda: hc._restart_bridge("slack-bridge", stale=True))
        if ok or "BOTH" not in how:
            fails.append(f"k) {label_}: mixed state must refuse: ok={ok} how={how!r}")
        if host.of("kickstart"):
            fails.append(f"k) {label_}: kickstarted in a mixed state: {host.of('kickstart')}")
        fails += no_side_effects(host, f"k/{label_}")
    return fails


def case_l_spaced_repo_path_owns_its_wrapper() -> list[str]:
    """kewei r4 blocker 1: a supported repo path containing spaces (the bundled
    "Application Support" install) must classify OUR wrapper as ours — ps
    flattens argv, so tokenization cannot be the identity mechanism — and a
    sibling sharing the spaced path as a prefix stays foreign."""
    fails = []
    spaced = Path("/synthetic fixture/spaced path/engine/sutando")
    with mock.patch.object(hc, "REPO_DIR", spaced):
        # Positive control (kewei's exact shape): our wrapper under the spaced path.
        host = Host(job="absent", wrapper_alive=True, child_pids="555")
        got = with_host(host, lambda: hc._wrapper_pids("slack-bridge"))
        if got != ([OUR_WRAPPER], []):
            fails.append(f"l) spaced-path wrapper must be OURS: got {got}")
        # End-to-end: wrapper-only supervision drives the child, never spawns.
        host2 = Host(job="absent", wrapper_alive=True, child_pids="555")
        ok2, _ = with_host(host2, lambda: hc._restart_bridge("slack-bridge", stale=True))
        if not ok2 or host2.of("kill") != ["555"] or host2.bridge_spawns():
            fails.append(f"l) spaced-path restart leg: ok={ok2} events={host2.events}")
        # Same-prefix sibling of the spaced path stays foreign.
        host3 = Host(job="absent", foreign_wrapper_alive=True, foreign_child_pids="888")
        host3._ps_rows_orig = host3._ps_rows
        def _rows3():
            return [(p_, pp, cmd.replace("/repo-b", f"{spaced}-copy"))
                    for p_, pp, cmd in host3._ps_rows_orig()]
        host3._ps_rows = _rows3
        got3 = with_host(host3, lambda: hc._wrapper_pids("slack-bridge"))
        if got3 != ([], [FOREIGN_WRAPPER]):
            fails.append(f"l) same-prefix sibling of the spaced path must be FOREIGN: got {got3}")
        # An adversarial argv that merely EMBEDS our spaced path is not ours.
        host4 = Host(job="absent", foreign_wrapper_alive=True)
        host4._ps_rows_orig = host4._ps_rows
        def _rows4():
            rows = host4._ps_rows_orig()
            evil = (f"/bin/bash /repo-b/x.sh {spaced}/src/launchd/channel-bridge-wrapper.sh slack")
            return [(p_, pp, evil if p_ == FOREIGN_WRAPPER else cmd) for p_, pp, cmd in rows]
        host4._ps_rows = _rows4
        got4 = with_host(host4, lambda: hc._wrapper_pids("slack-bridge"))
        if got4 != ([], [FOREIGN_WRAPPER]):
            fails.append(f"l) embedded-path argv must stay FOREIGN: got {got4}")
    return fails


def main() -> int:
    cases = [
        case_a_supervised_down_restarts_through_launchd,
        case_b_supervised_stale_kickstarts,
        case_c_kickstart_refused_kills_only_our_confirmed_child,
        case_d_undrivable_supervisor_reports_failure,
        case_e_wrapper_only_supervision_never_kickstarts,
        case_f_absent_supervision_verified_evict_then_spawn,
        case_g_unknown_supervision_fails_closed,
        case_h_foreign_supervisor_fails_closed,
        case_i_unconfirmed_kills_report_failure,
        case_j_unverified_eviction_refuses_spawn,
        case_k_full_ownership_matrix,
        case_l_spaced_repo_path_owns_its_wrapper,
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
