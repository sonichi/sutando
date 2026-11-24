#!/usr/bin/env python3
"""The one route every engine caller takes to restart the core.

On a desktop install whose checkout is the app's own bundle (`<app-support>/engine/<checkout>`) a
restart is the app's `sutando-ctl.sh restart-core`, which holds the app's launch lock, so it never
overlaps the app's own launcher; it runs through `ctl` below. Every route appends one intent line
and one outcome line to `state/recovery/audit.jsonl` (`event: core_restart`, the caller, and the
card's `hitl_id` + `revision` when a card asked; otherwise `actor: host-supervisor`). A bundle with
no ctl, or with a ctl that does
not list `launch-lock`, is refused and nothing runs: `start-cli.sh --restart` would kill the
gateway's session. Anywhere else the restart is `src/agent/start-cli.sh --restart`.

    python3 src/core_restart.py restart --workspace W --caller NAME [--runtime R] [--repo DIR]
                                        [--start] [--start-cli PATH] [-- START_CLI_ARGS...]
        resolve and run a restart in the foreground; exits with its code, or REFUSED_EXIT
    python3 src/core_restart.py ctl --ctl PATH --workspace W --caller NAME
                                    [--hitl-id ID --revision N] [--runtime R] [--start]
        run the ctl's restart-core between its intent and outcome lines; exits with the ctl's code
    python3 src/core_restart.py start-cli --start-cli PATH --workspace W --caller NAME
                                    [--hitl-id ID --revision N] [--runtime R] [--deadline-s S]
                                    [--foreground] [-- ARGS...]
        run start-cli --restart between the same two lines; exits with the launcher's code. With
        --deadline-s, or when this process is told to stop, the launcher is ended and reaped first
        and the outcome line says `deadline` or `cancelled`. --deadline-s covers both audit lines
        too. --foreground leaves the launcher this process's stdin, so it can attach to a terminal

A runtime switch through the ctl takes the app's launch lock itself (the ctl then skips it), makes
the runtime the desktop's selection (the app's launcher reads it), runs the ctl, and when the ctl
does not exit 0 puts the previous selection back (none, if there was none) unless a newer choice
replaced it meanwhile; all of it under that one hold. A lock that cannot be opened refuses before
anything is written. `--start` (a paused core started again) takes the same lock, reads the ctl's
`status` under it and runs the app's `launch-sutando.sh` only when the core is down; a core that is
running is left alone (ALREADY_RUNNING_EXIT), so a start never stops anything.
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
import uuid
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
#: A `start` that found the core already running under the launch lock: nothing was stopped or started.
ALREADY_RUNNING_EXIT = 14
#: The app's own ensure-running launcher beside the ctl, and the ctl's read-only status probe.
DESKTOP_LAUNCHER = "launch-sutando.sh"
CTL_STATUS_TIMEOUT_S = 15
#: Set by an app launcher run that holds the lock; a restart requested from here never inherits it.
LOCK_MARKER = "SUTANDO_ENGINE_LAUNCH_LOCK_HELD"
#: The app's launch lock under its app-support dir, and how long the ctl waits for it.
LAUNCH_LOCK_FILE = "engine-launch.lock"
LOCK_WAIT_ENV, DEFAULT_LOCK_WAIT_S = "SUTANDO_CTL_LAUNCH_LOCK_WAIT_S", 60
LOCK_LOCKED, LOCK_BUSY, LOCK_UNAVAILABLE = "locked", "busy", "unavailable"
AUDIT_EVENT = "core_restart"
AUTOMATIC_ACTOR = "host-supervisor"
INTENT, OUTCOME = "intent", "outcome"
#: This module's own file: the recorder runs from here, whichever checkout's launcher it records.
RECORDER = str(Path(__file__).resolve())
#: A caller that bounds its restart hands the recorder that bound less this margin, so the recorder
#: ends the launcher and writes the outcome before the caller stops waiting for it.
DEADLINE_MARGIN_S = 20
REASON_DEADLINE, REASON_CANCELLED = "deadline", "cancelled"
#: The longest one audit line waits for the audit lock; a line that waits longer is reported, not written.
AUDIT_LOCK_WAIT_S = 5
#: The recorder's stderr line for an audit line it could not write, and how many a caller relays.
AUDIT_DIAGNOSTIC = re.compile(r"core_restart: audit (?:intent|outcome) line not written \(attempt att_[0-9a-f]{12}\)")
MAX_AUDIT_DIAGNOSTICS = 4
#: The launchers' restart trace file, in the directory the caller keeps its logs in.
RESTART_TRACE = "restart-attempts.log"
CANCEL_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


class Cancelled(Exception):
    """This recorder was told to stop while its launcher ran."""


def new_attempt() -> str:
    """One id shared by an attempt's intent and outcome lines, so overlapping attempts pair up."""
    return "att_" + uuid.uuid4().hex[:12]

Runner = Callable[..., subprocess.CompletedProcess]


def start_cli_argv(repo: Path, runtime: Optional[str] = None, extra: Sequence[str] = (),
                   start_cli: Optional[str] = None, start: bool = False) -> List[str]:
    argv = ["bash", start_cli or str(Path(repo) / "src" / "agent" / "start-cli.sh")]
    if runtime:
        argv += ["--runtime", runtime]
    return argv + ([] if start else ["--restart"]) + list(extra)


def recorded_start_cli_argv(repo: Path, workspace: Path, caller: str, *, via: str,
                            runtime: Optional[str] = None, hitl_id: str = "",
                            revision: Optional[int] = None, extra: Sequence[str] = (),
                            start_cli: Optional[str] = None, start: bool = False,
                            deadline_s: Optional[float] = None, foreground: bool = False) -> List[str]:
    argv = [sys.executable, RECORDER, "start-cli",
            "--workspace", str(workspace), "--caller", caller, "--via", via,
            "--start-cli", start_cli or str(Path(repo) / "src" / "agent" / "start-cli.sh")]
    if hitl_id:
        argv += ["--hitl-id", hitl_id, "--revision", str(int(revision or 0))]
    if runtime:
        argv += ["--runtime", runtime]
    if start:
        argv += ["--start"]
    if deadline_s is not None:
        argv += ["--deadline-s", str(max(1, int(deadline_s)))]
    if foreground:
        argv += ["--foreground"]
    if extra:
        argv += ["--", *extra]
    return argv


def route(repo: Path, env: Dict, runner: Runner, *, workspace: Path, caller: str,
          runtime: Optional[str] = None, hitl_id: str = "", revision: Optional[int] = None,
          extra: Sequence[str] = (), start_cli: Optional[str] = None,
          start: bool = False, caller_deadline_s: Optional[float] = None,
          foreground: bool = False) -> Tuple[Optional[List[str]], str]:
    """(argv, via) for one core restart; argv None when nothing may run. `env` is the restart's
    environment and is updated for the ctl. Only the ctl's capability probe runs here. `start`
    starts a core that is down: start-cli with no flag, or `ctl --start` (start-if-down) in the bundle.
    `caller_deadline_s` is how long the caller itself waits: the standalone recorder ends its
    launcher DEADLINE_MARGIN_S before that. Without it the launcher is not bounded here.
    `foreground` is a restart a person runs at a terminal: the standalone launcher keeps stdin."""
    deadline_s = None if caller_deadline_s is None else caller_deadline_s - DEADLINE_MARGIN_S
    app_support = sutando_config.desktop_app_support(repo)
    if app_support is None:
        via = VIA_START_CLI
        return recorded_start_cli_argv(repo, workspace, caller, via=via, runtime=runtime,
                                       hitl_id=hitl_id, revision=revision, extra=extra,
                                       start_cli=start_cli, start=start, deadline_s=deadline_s,
                                       foreground=foreground), via
    resolved = Path(repo).resolve()
    if resolved.parent.name != "engine" or resolved.parent.parent != Path(app_support).resolve():
        via = VIA_FALLBACK
        return recorded_start_cli_argv(repo, workspace, caller, via=via, runtime=runtime,
                                       hitl_id=hitl_id, revision=revision, extra=extra,
                                       start_cli=start_cli, start=start, deadline_s=deadline_s,
                                       foreground=foreground), via
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
    if start:
        argv += ["--start"]
    return argv, VIA_CTL


def ctl_has_launch_lock(ctl: Path, env: Dict, runner: Runner) -> bool:
    try:
        run = runner(["bash", str(ctl), "capabilities"], capture_output=True, text=True,
                     timeout=CTL_CAPABILITIES_TIMEOUT_S, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return run.returncode == 0 and LAUNCH_LOCK_CAPABILITY in (run.stdout or "").split()


def _audit(workspace: Path, entry: Dict, err=None, wait_s: float = AUDIT_LOCK_WAIT_S) -> bool:
    """Append one audit line, waiting at most `wait_s` for the audit lock; a line that could not be
    written is said on stderr, never dropped silently."""
    from hitl import recovery
    written = recovery.audit(Path(workspace), entry, wait_s=wait_s)
    if not written:
        print(f"core_restart: audit {entry.get('phase')} line not written (attempt {entry.get('attempt')})",
              file=err or sys.stderr)
    return written


def audit_diagnostics(stderr) -> List[str]:
    """The recorder's audit-failure lines in a captured stderr, at most MAX_AUDIT_DIAGNOSTICS;
    anything else there (the launcher's own output) is never returned."""
    lines = [line.strip() for line in str(stderr or "").splitlines()]
    return [line for line in lines if AUDIT_DIAGNOSTIC.fullmatch(line)][:MAX_AUDIT_DIAGNOSTICS]


def report_audit_diagnostics(trace: Path, caller: str, stderr, err=None,
                             now: Callable[[], float] = time.time) -> List[str]:
    """For a caller that captures the restart's stderr and reads only its exit code: say the
    recorder's audit-failure lines on `err` and keep them in the caller's restart trace file
    `trace`, on success too."""
    lines = audit_diagnostics(stderr)
    if not lines:
        return lines
    for line in lines:
        print(f"{caller}: {line}", file=err or sys.stderr)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now()))
    try:
        trace = Path(trace)
        trace.parent.mkdir(parents=True, exist_ok=True)
        with open(trace, "a") as f:
            f.writelines(f"{stamp} [audit] {caller}: {line}\n" for line in lines)
    except OSError:
        pass  # stderr above already said it
    return lines


def _audit_outcome(workspace: Path, base: Dict, intent_written: bool, at: int, outcome: str,
                   rc: Optional[int], reason: str = "", wait_s: float = AUDIT_LOCK_WAIT_S) -> None:
    line = dict(base, phase=OUTCOME, at=at, outcome=outcome)
    if rc is not None:
        line["rc"] = rc
    if reason:
        line["reason"] = reason
    if not intent_written:
        line["intent_recorded"] = False  # this outcome has no intent line to pair with
    _audit(workspace, line, wait_s=wait_s)


def _audit_base(via: str, caller: str, hitl_id: str = "",
                revision: Optional[int] = None, runtime: Optional[str] = None) -> Dict:
    base = {"event": AUDIT_EVENT, "via": via, "caller": caller, "attempt": new_attempt()}
    if hitl_id:
        base.update(hitl_id=hitl_id, revision=int(revision or 0))
    else:
        base["actor"] = AUTOMATIC_ACTOR
    if runtime:
        base["runtime"] = runtime
    return base


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


def _core_state(ctl: Path, env: Dict, runner: Runner) -> str:
    """`running`, `stopped` or "" (unknown) from the ctl's read-only `status` line."""
    try:
        run = runner(["bash", str(ctl), "status"], capture_output=True, text=True,
                     timeout=CTL_STATUS_TIMEOUT_S, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    if run.returncode != 0:
        return ""
    states = [t[len("core="):] for t in (run.stdout or "").split() if t.startswith("core=")]
    return states[0] if states and states[0] in ("running", "stopped") else ""


def run_ctl(ctl: Path, workspace: Path, caller: str, *, hitl_id: str = "", revision: Optional[int] = None,
            runtime: Optional[str] = None, runner: Runner = subprocess.run, env: Optional[Dict] = None,
            repo: Path = REPO, now: Callable[[], float] = time.time,
            lock_wait_s: Optional[float] = None, start: bool = False) -> int:
    """Run the ctl's restart-core between one intent and one outcome audit line; its exit code.
    A runtime switch writes, restarts and rolls back under one hold of the app's launch lock, and a
    `start` reads the core's state under that hold and launches only a core that is down."""
    base = _audit_base(VIA_CTL, caller, hitl_id, revision, runtime)
    if start:
        base["start"] = True
    intent_written = _audit(workspace, dict(base, phase=INTENT, at=int(now())))
    env = dict(os.environ if env is None else env)
    env.pop(LOCK_MARKER, None)

    def end(outcome: str, rc: Optional[int], reason: str = "") -> int:
        _audit_outcome(workspace, base, intent_written, int(now()), outcome, rc, reason)
        return 1 if rc is None else rc

    held = None
    try:
        if runtime or start:
            app = env.get("SUTANDO_APP_SUPPORT") or str(sutando_config.desktop_app_support(repo) or "")
            env["SUTANDO_APP_SUPPORT"] = app
            lock = os.path.join(app, "run", LAUNCH_LOCK_FILE)
            wait = float(env.get(LOCK_WAIT_ENV) or DEFAULT_LOCK_WAIT_S) if lock_wait_s is None else lock_wait_s
            held, state = take_launch_lock(Path(lock), wait)
            if state == LOCK_BUSY:
                return end("failed", CTL_LAUNCH_LOCK_BUSY, "launch-lock-busy")
            if held is None:
                return end("refused", REFUSED_EXIT, "launch-lock-unavailable")  # nothing was changed
        if start:
            # Read under the hold: a core that came up while the lock was awaited is never restarted.
            core = _core_state(ctl, env, runner)
            if core == "running":
                return end("skipped", ALREADY_RUNNING_EXIT, "core-already-running")
            launcher = Path(ctl).parent / DESKTOP_LAUNCHER
            if core != "stopped" or not launcher.is_file():
                return end("refused", REFUSED_EXIT, "core-state-unknown" if core != "stopped" else "launcher-missing")
            argv = ["bash", str(launcher)]
        else:
            argv = ["bash", str(ctl), "restart-core"]
            if held is not None:
                env[LOCK_MARKER] = lock  # the ctl skips the lock this process already holds
        rc, changed, previous = None, False, ""
        if runtime:
            previous = sutando_config._desktop_selected_runtime(repo)
            try:
                changed = sutando_config.persist_desktop_runtime_selection(runtime, repo) == "updated"
            except (OSError, ValueError):
                return end("refused", REFUSED_EXIT, "runtime-not-selectable")
        try:
            rc = runner(argv, env=env, stdin=subprocess.DEVNULL).returncode
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
    return end("done" if rc == 0 else "failed", rc)


def run_start_cli(workspace: Path, caller: str, *, via: str = VIA_START_CLI,
                  hitl_id: str = "", revision: Optional[int] = None,
                  runtime: Optional[str] = None, extra: Sequence[str] = (),
                  start_cli: str, runner: Runner = subprocess.run,
                  now: Callable[[], float] = time.time, start: bool = False,
                  deadline_s: Optional[float] = None, foreground: bool = False,
                  clock: Callable[[], float] = time.monotonic) -> int:
    """Run the standalone launcher between the shared restart intent and outcome lines. `deadline_s`
    is one budget for the intent line, the launcher and the outcome line. Past it, or when this
    process is cancelled, the launcher is killed and reaped before the outcome (`failed`, reason
    `deadline` or `cancelled`) is written. `foreground` leaves the launcher this process's stdin."""
    base = _audit_base(via, caller, hitl_id, revision, runtime)
    if start:
        base["start"] = True
    end = None if deadline_s is None else clock() + deadline_s
    # Each audit line may wait this long for the audit lock; the launcher leaves that much for the outcome.
    line_wait = AUDIT_LOCK_WAIT_S if deadline_s is None else min(AUDIT_LOCK_WAIT_S, deadline_s / 4)

    def left(reserve: float = 0.0) -> float:
        return max(0.0, end - clock() - reserve)

    intent_written = _audit(workspace, dict(base, phase=INTENT, at=int(now())), wait_s=line_wait)
    rc, reason = None, ""
    try:
        # subprocess.run kills and reaps its child when the timeout or any other exception ends the wait.
        rc = runner(start_cli_argv(REPO, runtime, extra, start_cli, start),
                    stdin=None if foreground else subprocess.DEVNULL,
                    timeout=None if end is None else left(line_wait)).returncode
    except subprocess.TimeoutExpired:
        reason = REASON_DEADLINE
    except Cancelled:
        reason = REASON_CANCELLED
    except (OSError, subprocess.SubprocessError):
        pass
    _audit_outcome(workspace, base, intent_written, int(now()), "done" if rc == 0 else "failed", rc, reason,
                   wait_s=line_wait if end is None else min(line_wait, left()))
    return 1 if rc is None else rc


def _cancel_on_signal(signals: Sequence[int] = CANCEL_SIGNALS, install: Callable = signal.signal) -> None:
    """A stop signal becomes Cancelled in the main thread, so the wait on the launcher unwinds."""
    def cancel(signum, _frame):
        raise Cancelled(signum)
    for sig in signals:
        install(sig, cancel)


def restart(workspace: Path, caller: str, *, runtime: Optional[str] = None, extra: Sequence[str] = (),
            start_cli: Optional[str] = None, repo: Path = REPO, runner: Runner = subprocess.run,
            execv: Callable = os.execvpe, err=sys.stderr, start: bool = False) -> int:
    """A foreground restart for a caller with no card: start-cli replaces this process; the ctl runs
    here; a refusal says why and exits REFUSED_EXIT."""
    env = dict(os.environ)
    argv, via = route(repo, env, runner, workspace=workspace, caller=caller, runtime=runtime,
                      extra=extra, start_cli=start_cli, start=start, foreground=True)
    if argv is None:
        why = "no launch lock in its sutando-ctl.sh" if via == VIA_NO_LOCK else "its sutando-ctl.sh is missing"
        print(f"core_restart: refused ({via}): this core runs inside the desktop app and {why}; "
              "nothing was restarted. Restart the core from the desktop app.", file=err)
        return REFUSED_EXIT
    if via != VIA_CTL:
        execv(argv[0], argv, env)
        return 1  # execvpe only returns in a test double
    ctl = Path(argv[argv.index("--ctl") + 1])
    return run_ctl(ctl, workspace, caller, runtime=runtime, runner=runner, env=env, repo=repo, start=start)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("restart")
    r.add_argument("--workspace", required=True)
    r.add_argument("--caller", required=True)
    r.add_argument("--runtime", default=None)
    r.add_argument("--start-cli", default=None)
    r.add_argument("--repo", default=str(REPO), help="the caller's checkout (its own path, unresolved)")
    r.add_argument("--start", action="store_true", help="start a core that is down (start-cli without --restart)")
    r.add_argument("extra", nargs=argparse.REMAINDER)
    c = sub.add_parser("ctl")
    c.add_argument("--ctl", required=True)
    c.add_argument("--workspace", required=True)
    c.add_argument("--caller", required=True)
    c.add_argument("--hitl-id", default="")
    c.add_argument("--revision", type=int, default=None)
    c.add_argument("--runtime", default=None)
    c.add_argument("--start", action="store_true", help="start the core only if it is down; never stops one")
    s = sub.add_parser("start-cli")
    s.add_argument("--workspace", required=True)
    s.add_argument("--caller", required=True)
    s.add_argument("--via", choices=(VIA_START_CLI, VIA_FALLBACK), required=True)
    s.add_argument("--start-cli", required=True)
    s.add_argument("--hitl-id", default="")
    s.add_argument("--revision", type=int, default=None)
    s.add_argument("--runtime", default=None)
    s.add_argument("--start", action="store_true", help="start a core that is down (start-cli without --restart)")
    s.add_argument("--deadline-s", type=float, default=None, help="end the launcher after this long")
    s.add_argument("--foreground", action="store_true", help="leave the launcher this process's stdin")
    s.add_argument("extra", nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    if a.cmd == "ctl":
        return run_ctl(Path(a.ctl), Path(a.workspace), a.caller, hitl_id=a.hitl_id,
                       revision=a.revision, runtime=a.runtime, start=a.start)
    if a.cmd == "start-cli":
        extra = a.extra[1:] if a.extra[:1] == ["--"] else a.extra
        _cancel_on_signal()
        return run_start_cli(Path(a.workspace), a.caller, via=a.via, hitl_id=a.hitl_id,
                             revision=a.revision, runtime=a.runtime, extra=extra,
                             start_cli=a.start_cli, start=a.start, deadline_s=a.deadline_s,
                             foreground=a.foreground)
    extra = a.extra[1:] if a.extra[:1] == ["--"] else a.extra
    return restart(Path(a.workspace), a.caller, runtime=a.runtime, extra=extra, start_cli=a.start_cli,
                   repo=Path(a.repo), start=a.start)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
