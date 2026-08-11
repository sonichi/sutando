#!/usr/bin/env python3
"""Regression tests for the start-cli.sh --restart kill-race / false-success bug.

Incident (2026-07-30): an app-menu "Restart Core CLI" killed the core, but no
successor came up — the host was dark ~6.3h while Sutando.app had reported a
successful restart. Root cause in src/agent/claude/cli/start-cli.sh:

  --restart killed the core, polled only ~1s for it to exit, and when the core
  was still shutting down it fell into the "orphan claude already running →
  reuse it → exit 0" guard: the script returned SUCCESS without creating a fresh
  core. The zombie then died and the session was left with no core. Because the
  script exited 0, Sutando.app reported "Core restarted" — a false success.

The fix has three parts, each covered below:
  1. --restart escalates SIGTERM→SIGKILL and aborts (non-zero) if it can't kill.
  2. --restart never adopts an orphan claude (the reuse guard is restart-gated).
  3. the no-TTY create path verifies the core actually came up before exit 0,
     and exits non-zero otherwise (no more false success).

Harness: PATH-shimmed `tmux`/`pgrep`/`ps`/`claude` driven by two marker files —
SESS_MARK (session exists) and CORE_MARK (a live `claude --name sutando-core`).
`tmux new-session` in the stub deliberately does NOT create CORE_MARK, i.e. it
models a launch that never yields a live core — so any path that reports success
without a live core is caught. `tmux kill-session` clears both markers (killing
the session kills its core). No real tmux/claude/core is touched.

Run:  python3 tests/start-cli-restart-verify.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
FAKEPID = "999999"

TMUX_STUB = r"""#!/bin/bash
# Stateful tmux stub. Markers: $SESS_MARK (session), $CORE_MARK (live core).
# Skip the leading `-S <socket>` so $1 is the subcommand.
while [ "$1" = "-S" ]; do shift 2; done
sub="$1"; shift
case "$sub" in
  has-session) [ -f "$SESS_MARK" ] && exit 0 || exit 1 ;;
  # new-session models a launch that creates the SESSION but whose core never
  # comes up (CORE_MARK intentionally NOT created) — the failure under test.
  new-session) touch "$SESS_MARK"; exit 0 ;;
  # killing the session normally kills its core → clear both markers. When
  # $WEDGED is set, model an unresponsive core: the session drops but the
  # `claude` process refuses to die (CORE_MARK stays) — so SIGTERM "doesn't take".
  kill-session) rm -f "$SESS_MARK"; [ -n "$WEDGED" ] || rm -f "$CORE_MARK"; exit 0 ;;
  *) exit 0 ;;  # start-server/set-option/bind/select-window/new-window/attach
esac
"""

PGREP_STUB = r"""#!/bin/bash
# core-input-watch probe (ensure_core_monitor) → pretend running so nothing spawns.
case "$*" in *core-input-watch*) exit 0 ;; esac
# claude probe: report a live core iff CORE_MARK exists.
case "$*" in
  *claude*) if [ -f "$CORE_MARK" ]; then echo "%s claude --name sutando-core"; exit 0; else exit 1; fi ;;
esac
exit 1
""" % FAKEPID

PS_STUB = r"""#!/bin/bash
# core_claude_pids does: ps -p <pid> -o args=
# Report the matching cmdline only for our fake pid while CORE_MARK exists.
want=""
prev=""
for a in "$@"; do
  [ "$prev" = "-p" ] && want="$a"
  prev="$a"
done
if [ "$want" = "%s" ] && [ -f "$CORE_MARK" ]; then
  echo "claude --name sutando-core"
fi
exit 0
""" % FAKEPID


def _run(restart: bool, session: bool, core: bool, force: bool = False, wedged: bool = False):
    """Run start-cli.sh in the stub env with the given initial state.

    force  → invoke --force-restart instead of --restart.
    wedged → the core process refuses to die (SIGTERM/kill-session won't reap it).
    Returns (returncode, stdout+stderr)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bind = td / "bin"
        bind.mkdir()
        sess_mark = td / "sess"
        core_mark = td / "core"
        if session:
            sess_mark.write_text("")
        if core:
            core_mark.write_text("")

        for name, body in (("tmux", TMUX_STUB), ("pgrep", PGREP_STUB),
                           ("ps", PS_STUB), ("claude", "#!/bin/bash\nexit 0\n")):
            p = bind / name
            p.write_text(body)
            p.chmod(0o755)

        env = {
            "PATH": f"{bind}:/usr/bin:/bin",
            "HOME": str(td),
            "SESS_MARK": str(sess_mark),
            "CORE_MARK": str(core_mark),
            # keep the run fast: the fix polls in 0.2s ticks up to a few seconds.
            "SUTANDO_TMUX_SOCKET": str(td / "sock"),
            # start-cli.sh appends to <workspace>/state/session-starts.log and
            # logs/restart-attempts.log; unredirected, the run writes LIVE state.
            "SUTANDO_TEST_MODE": "1",
            "SUTANDO_WORKSPACE": str(td / "workspace"),
        }
        if wedged:
            env["WEDGED"] = "1"
        args = ["/bin/bash", str(SCRIPT)]
        if force:
            args.append("--force-restart")
        elif restart:
            args.append("--restart")
        r = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
        return r.returncode, (r.stdout + r.stderr)


def case_fresh_start_core_down_exits_nonzero():
    """(Fix 3) Fresh start, launched core never comes up → must exit non-zero."""
    rc, out = _run(restart=False, session=False, core=False)
    if rc == 0:
        return [f"fresh start with no live core exited 0 (false success): out={out!r}"]
    return []


def case_restart_core_down_exits_nonzero():
    """(Fix 1+3) --restart with an existing core; successor never comes up →
    must exit non-zero, not the old false-success exit 0."""
    rc, out = _run(restart=True, session=True, core=True)
    if rc == 0:
        return [f"--restart that failed to bring the core back exited 0 (the bug): out={out!r}"]
    return []


def case_non_restart_reuses_orphan_exits_zero():
    """(Guard against Fix 2 over-reach) NON-restart, orphan core alive, no
    session → the reuse path must still fire (exit 0, 'reusing it')."""
    rc, out = _run(restart=False, session=False, core=True)
    fails = []
    if rc != 0:
        fails.append(f"non-restart orphan-reuse should exit 0, got {rc}: out={out!r}")
    if "reusing it" not in out:
        fails.append(f"non-restart orphan should print 'reusing it': out={out!r}")
    return fails


def case_restart_does_not_reuse_orphan():
    """(Fix 2) --restart with an orphan core alive must NOT adopt it — it must
    tear down and go to the verified create path (never 'reusing it')."""
    rc, out = _run(restart=True, session=False, core=True)
    fails = []
    if "reusing it" in out:
        fails.append(f"--restart must not reuse an orphan core: out={out!r}")
    if rc == 0:
        fails.append(f"--restart with a stub core that never comes up should exit non-zero: out={out!r}")
    return fails


def case_restart_wedged_aborts_not_kills():
    """(Split) plain --restart on a wedged core must ABORT and point at
    force-restart — it must NOT SIGKILL (the core may be mid-task)."""
    rc, out = _run(restart=True, session=True, core=True, wedged=True)
    fails = []
    if rc == 0:
        fails.append(f"--restart on a wedged core should abort non-zero, got 0: out={out!r}")
    if "force-restart" not in out:
        fails.append(f"--restart abort should point at force-restart: out={out!r}")
    if "escalating to SIGKILL" in out:
        fails.append(f"plain --restart must NOT escalate to SIGKILL: out={out!r}")
    return fails


def case_force_restart_wedged_escalates_to_sigkill():
    """(Split) --force-restart on a wedged core DOES escalate to SIGKILL; if the
    core still won't die it hard-aborts non-zero (never stacks a second core)."""
    rc, out = _run(restart=True, session=True, core=True, force=True, wedged=True)
    fails = []
    if "escalating to SIGKILL" not in out:
        fails.append(f"--force-restart should escalate to SIGKILL on a wedged core: out={out!r}")
    if rc == 0:
        fails.append(f"--force-restart on an unkillable core should hard-abort non-zero: out={out!r}")
    return fails


def _live_log_lines() -> "tuple[int, int]":
    """Line counts of the two logs start-cli.sh appends to in the LIVE workspace."""
    ws = subprocess.run(["bash", str(REPO / "scripts" / "sutando-config.sh"), "workspace"],
                        capture_output=True, text=True).stdout.strip()
    def count(rel: str) -> int:
        f = Path(ws) / rel
        return len(f.read_text().splitlines()) if ws and f.exists() else 0
    return count("state/session-starts.log"), count("logs/restart-attempts.log")


def case_run_does_not_touch_the_live_workspace():
    """start-cli.sh stamps state/session-starts.log and logs/restart-attempts.log;
    a stub run reaching the live copies fabricates a real-looking session boundary."""
    before = _live_log_lines()
    _run(restart=True, session=True, core=True)
    after = _live_log_lines()
    if after != before:
        return [f"a stub run appended to the live workspace: "
                f"(session-starts, restart-attempts) {before} -> {after}"]
    return []


def main() -> int:
    cases = [
        ("fresh-start-core-down-exits-nonzero", case_fresh_start_core_down_exits_nonzero),
        ("restart-core-down-exits-nonzero", case_restart_core_down_exits_nonzero),
        ("non-restart-reuses-orphan-exits-zero", case_non_restart_reuses_orphan_exits_zero),
        ("restart-does-not-reuse-orphan", case_restart_does_not_reuse_orphan),
        ("restart-wedged-aborts-not-kills", case_restart_wedged_aborts_not_kills),
        ("force-restart-wedged-escalates-to-sigkill", case_force_restart_wedged_escalates_to_sigkill),
        ("run-does-not-touch-the-live-workspace", case_run_does_not_touch_the_live_workspace),
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
    print("\nstart-cli.sh --restart verifies liveness and never false-succeeds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
