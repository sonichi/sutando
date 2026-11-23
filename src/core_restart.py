#!/usr/bin/env python3
"""The one route every engine caller takes to restart the core.

On a desktop install whose checkout is the app's own bundle (`<app-support>/engine/<checkout>`) a
restart is the app's `sutando-ctl.sh restart-core`, which holds the app's launch lock, so it never
overlaps the app's own launcher; it runs through `ctl` below, which appends one intent line and one
outcome line to `state/recovery/audit.jsonl` (`event: ctl_restart`, the caller's name, and the
card's `hitl_id` + `revision` when a card asked). That bundle with no ctl, or with a ctl that does
not list `launch-lock`, is refused and nothing runs: `start-cli.sh --restart` would kill the
gateway's session. Anywhere else the restart is `src/agent/start-cli.sh --restart`.

    python3 src/core_restart.py restart --workspace W --caller NAME [--runtime R] [--repo DIR]
                                        [--start-cli PATH] [-- START_CLI_ARGS...]
        resolve and run a restart in the foreground; exits with its code, or REFUSED_EXIT
    python3 src/core_restart.py ctl --ctl PATH --workspace W --caller NAME
                                    [--hitl-id ID --revision N] [--runtime R]
        run the ctl's restart-core between its intent and outcome lines; exits with the ctl's code

A runtime switch through the ctl takes the app's launch lock itself (the ctl then skips it), makes
the runtime the desktop's selection (the app's launcher reads it), runs the ctl, and when the ctl
does not exit 0 puts the previous selection back (none, if there was none) unless a newer choice
replaced it meanwhile; all of it under that one hold.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sutando_config  # noqa: E402

REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root

#: Which command a restart ran, as the audit's `via`.
VIA_CTL, VIA_START_CLI, VIA_FALLBACK = "ctl", "start-cli", "start-cli-fallback"
#: The app's bundle without its controller, or with one that takes no launch lock: refused, nothing run.
VIA_NO_CTL, VIA_NO_LOCK = "ctl-missing", "ctl-no-lock"
#: The ctl's side-effect-free capability list; `launch-lock` means restart-core takes the app's lock.
CTL_CAPABILITIES_TIMEOUT_S = 5
LAUNCH_LOCK_CAPABILITY = "launch-lock"
#: The desktop app's control script, installed beside the engine checkout it drives.
DESKTOP_CTL = "sutando-ctl.sh"
#: Its restart-core exit when another launcher run held the launch lock: nothing was killed or started.
CTL_LAUNCH_LOCK_BUSY = 12
#: This module's exit when it refused before anything ran; no launcher or ctl code uses it.
REFUSED_EXIT = 13
#: Set by an app launcher run that holds the lock; a restart requested from here never inherits it.
LOCK_MARKER = "SUTANDO_ENGINE_LAUNCH_LOCK_HELD"
#: The app's launch lock under its app-support dir, and how long the ctl waits for it.
LAUNCH_LOCK_FILE = "engine-launch.lock"
LOCK_WAIT_ENV, DEFAULT_LOCK_WAIT_S = "SUTANDO_CTL_LAUNCH_LOCK_WAIT_S", 60
LOCK_LOCKED, LOCK_BUSY, LOCK_UNAVAILABLE = "locked", "busy", "unavailable"
AUDIT_EVENT = "ctl_restart"
INTENT, OUTCOME = "intent", "outcome"

Runner = Callable[..., subprocess.CompletedProcess]


def start_cli_argv(repo: Path, runtime: Optional[str] = None, extra: Sequence[str] = (),
                   start_cli: Optional[str] = None) -> List[str]:
    argv = ["bash", start_cli or str(Path(repo) / "src" / "agent" / "start-cli.sh")]
    if runtime:
        argv += ["--runtime", runtime]
    return argv + ["--restart", *extra]


def route(repo: Path, env: Dict, runner: Runner, *, workspace: Path, caller: str,
          runtime: Optional[str] = None, hitl_id: str = "", revision: Optional[int] = None,
          extra: Sequence[str] = (), start_cli: Optional[str] = None) -> Tuple[Optional[List[str]], str]:
    """(argv, via) for one core restart; argv None when nothing may run. `env` is the restart's
    environment and is updated for the ctl. Only the ctl's capability probe runs here."""
    fallback = start_cli_argv(repo, runtime, extra, start_cli)
    app_support = sutando_config.desktop_app_support(repo)
    if app_support is None:
        return fallback, VIA_START_CLI
    resolved = Path(repo).resolve()
    if resolved.parent.name != "engine" or resolved.parent.parent != Path(app_support).resolve():
        return fallback, VIA_FALLBACK  # a standalone clone the app's controller does not drive
    ctl = Path(repo).parent / DESKTOP_CTL
    if not ctl.is_file():
        return None, VIA_NO_CTL
    env["SUTANDO_APP_SUPPORT"] = str(app_support)
    env.pop(LOCK_MARKER, None)
    if not ctl_has_launch_lock(ctl, env, runner):
        return None, VIA_NO_LOCK  # an app older than the lock: its restart can overlap the launcher
    argv = [sys.executable, str(Path(repo) / "src" / "core_restart.py"), "ctl", "--ctl", str(ctl),
            "--workspace", str(workspace), "--caller", caller]
    if hitl_id:
        argv += ["--hitl-id", hitl_id, "--revision", str(int(revision or 0))]
    if runtime:
        argv += ["--runtime", runtime]
    return argv, VIA_CTL


def ctl_has_launch_lock(ctl: Path, env: Dict, runner: Runner) -> bool:
    try:
        run = runner(["bash", str(ctl), "capabilities"], capture_output=True, text=True,
                     timeout=CTL_CAPABILITIES_TIMEOUT_S, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return run.returncode == 0 and LAUNCH_LOCK_CAPABILITY in (run.stdout or "").split()


def _audit(workspace: Path, entry: Dict) -> None:
    from hitl import recovery
    recovery.audit(Path(workspace), entry)


def take_launch_lock(lock: Path, wait_s: float, sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic):
    """(open file, LOCKED) once this process holds the app's launch lock (the same flock the ctl and
    the app take); (None, BUSY) after `wait_s`; (None, UNAVAILABLE) when it cannot be opened."""
    import fcntl
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock, "a")
    except OSError:
        return None, LOCK_UNAVAILABLE
    end = clock() + wait_s
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh, LOCK_LOCKED
        except OSError:
            if clock() >= end:
                fh.close()
                return None, LOCK_BUSY
            sleep(0.2)


def run_ctl(ctl: Path, workspace: Path, caller: str, *, hitl_id: str = "", revision: Optional[int] = None,
            runtime: Optional[str] = None, runner: Runner = subprocess.run, env: Optional[Dict] = None,
            repo: Path = REPO, now: Callable[[], float] = time.time,
            lock_wait_s: Optional[float] = None) -> int:
    """Run the ctl's restart-core between one intent and one outcome audit line; its exit code.
    A runtime switch writes, restarts and rolls back under one hold of the app's launch lock."""
    base = {"event": AUDIT_EVENT, "via": VIA_CTL, "caller": caller}
    if hitl_id:
        base.update(hitl_id=hitl_id, revision=int(revision or 0))
    if runtime:
        base["runtime"] = runtime
    _audit(workspace, dict(base, phase=INTENT, at=int(now())))
    env = dict(os.environ if env is None else env)
    env.pop(LOCK_MARKER, None)
    held = None
    try:
        if runtime:
            app = env.get("SUTANDO_APP_SUPPORT") or str(sutando_config.desktop_app_support(repo) or "")
            env["SUTANDO_APP_SUPPORT"] = app
            lock = os.path.join(app, "run", LAUNCH_LOCK_FILE)
            wait = float(env.get(LOCK_WAIT_ENV) or DEFAULT_LOCK_WAIT_S) if lock_wait_s is None else lock_wait_s
            held, state = take_launch_lock(Path(lock), wait)
            if state == LOCK_BUSY:
                _audit(workspace, dict(base, phase=OUTCOME, at=int(now()), outcome="failed",
                                       rc=CTL_LAUNCH_LOCK_BUSY, reason="launch-lock-busy"))
                return CTL_LAUNCH_LOCK_BUSY
            if held is not None:
                env[LOCK_MARKER] = lock  # the ctl skips the lock this process already holds
        rc, changed, previous = None, False, ""
        if runtime:
            previous = sutando_config._desktop_selected_runtime(repo)
            try:
                changed = sutando_config.persist_desktop_runtime_selection(runtime, repo) == "updated"
            except (OSError, ValueError):
                _audit(workspace, dict(base, phase=OUTCOME, at=int(now()), outcome="refused",
                                       reason="runtime-not-selectable"))
                return REFUSED_EXIT
        try:
            rc = runner(["bash", str(ctl), "restart-core"], env=env, stdin=subprocess.DEVNULL).returncode
        except (OSError, subprocess.SubprocessError):
            rc = None
        if rc != 0 and changed:
            try:
                sutando_config.restore_desktop_runtime_selection(runtime, previous, repo)
            except (OSError, ValueError):
                pass  # the outcome line still says the switch failed
    finally:
        if held is not None:
            held.close()
    outcome = dict(base, phase=OUTCOME, at=int(now()), outcome="done" if rc == 0 else "failed")
    if rc is not None:
        outcome["rc"] = rc
    _audit(workspace, outcome)
    return 1 if rc is None else rc


def restart(workspace: Path, caller: str, *, runtime: Optional[str] = None, extra: Sequence[str] = (),
            start_cli: Optional[str] = None, repo: Path = REPO, runner: Runner = subprocess.run,
            execv: Callable = os.execvpe, err=sys.stderr) -> int:
    """A foreground restart for a caller with no card: start-cli replaces this process; the ctl runs
    here; a refusal says why and exits REFUSED_EXIT."""
    env = dict(os.environ)
    argv, via = route(repo, env, runner, workspace=workspace, caller=caller, runtime=runtime,
                      extra=extra, start_cli=start_cli)
    if argv is None:
        why = "no launch lock in its sutando-ctl.sh" if via == VIA_NO_LOCK else "its sutando-ctl.sh is missing"
        print(f"core_restart: refused ({via}): this core runs inside the desktop app and {why}; "
              "nothing was restarted. Restart the core from the desktop app.", file=err)
        return REFUSED_EXIT
    if via != VIA_CTL:
        execv(argv[0], argv, env)
        return 1  # execvpe only returns in a test double
    ctl = Path(argv[argv.index("--ctl") + 1])
    return run_ctl(ctl, workspace, caller, runtime=runtime, runner=runner, env=env, repo=repo)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("restart")
    r.add_argument("--workspace", required=True)
    r.add_argument("--caller", required=True)
    r.add_argument("--runtime", default=None)
    r.add_argument("--start-cli", default=None)
    r.add_argument("--repo", default=str(REPO), help="the caller's checkout (its own path, unresolved)")
    r.add_argument("extra", nargs=argparse.REMAINDER)
    c = sub.add_parser("ctl")
    c.add_argument("--ctl", required=True)
    c.add_argument("--workspace", required=True)
    c.add_argument("--caller", required=True)
    c.add_argument("--hitl-id", default="")
    c.add_argument("--revision", type=int, default=None)
    c.add_argument("--runtime", default=None)
    a = p.parse_args(argv)
    if a.cmd == "ctl":
        return run_ctl(Path(a.ctl), Path(a.workspace), a.caller, hitl_id=a.hitl_id,
                       revision=a.revision, runtime=a.runtime)
    extra = a.extra[1:] if a.extra[:1] == ["--"] else a.extra
    return restart(Path(a.workspace), a.caller, runtime=a.runtime, extra=extra, start_cli=a.start_cli,
                   repo=Path(a.repo))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
