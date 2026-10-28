"""Keepalive for a Hatch agent: one scheduled task, and the health row Sparrow reports.

Every 15 minutes the agent's scheduler runs `keepalive keepawake` (the run is what keeps
the agent awake); the agent registers that task itself, since Hatch's scheduler does not
run task files on disk. It restarts Sparrow if Sparrow is not running, replaces one no unit
runs whose REMOTE_HEALTH_COMMAND is missing or not this install's, and appends one line
to the keepalive log: `<utc> keepawake listener=up|down restarted=0|1[ reason=...]`. A scheduler
that stops running the task cannot report that itself, so Sparrow does: it runs
`keepalive health` (REMOTE_HEALTH_COMMAND, which install puts in Sparrow's
environment) and sends the row on its heartbeat. The row is healthy while the last
keepawake line is under two intervals old, unknown while a task that has not run yet
is that young, else abnormal (`keepawake-not-running`). A fresh keepawake line does not
mask the hook: the row is also abnormal when the hook has not run in HOOK_STALE_S
(`hook-not-running`), a task has waited over TASK_PICKUP_S (`task-not-picked-up`), or a held
task's room line is still queued or parked after HELD_LINE_DELIVER_S (`task-held-line-undelivered`)
or left the queue with no receipt (`task-held-line-unconfirmed`).
`keepalive report` lists the log's gaps, and reads the lines of the earlier
watchdog-and-probe pair too."""
from __future__ import annotations

import calendar
import datetime
import math
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from ... import _dirs, agent_identity, delivery_destination, diagnostics, task_hold, workspace_lock
from ...persisted_number import bounded_number
from . import cli as _cli, task_fields

TASK, INTERVAL_MIN = "sparrow-keepawake", 15
# Tasks an earlier install wrote; re-running install removes them.
OLD_TASKS = ("sparrow-watchdog", "keepalive-probe")
TASK_MARKER = "<!-- Written by ag2-sparrow-hatch install; re-run install to update it. -->"
STALE_S = 2 * INTERVAL_MIN * 60
NOT_RUNNING = "keepawake-not-running"
HOOK_NOT_RUNNING, HOOK_STALE_S = "hook-not-running", 5 * 60
TASK_NOT_PICKED_UP, TASK_PICKUP_S = "task-not-picked-up", 15 * 60
# A healthy row's reason while a task waits on a deliberate hold, never a problem.
TASK_HELD = "task-held-for-owner"
# A hold whose room line the drain has not delivered this long is reported, never acted on: the
# line is queued, being sent, parked or withdrawn (undelivered), or gone with no receipt (unconfirmed).
TASK_HELD_UNDELIVERED, HELD_LINE_DELIVER_S = "task-held-line-undelivered", 10 * 60
TASK_HELD_UNCONFIRMED = "task-held-line-unconfirmed"
UPGRADE_FAILED = "upgrade-failed"
NO_HEALTH_COMMAND, UNKNOWN_ENV = "no-health-command", "unknown-env"
# A running Sparrow from before the send lock: hold refuses for it and keepawake restarts it.
OLD_DRAIN = "drain-predates-send-lock"
# How long a live drain may go without its lane record before it counts as an older drain.
LANE_GRACE_S = 120
REASON_HINTS = {
    HOOK_NOT_RUNNING: f"the hook has not run in the last {HOOK_STALE_S // 60} minutes; see "
                      "the hook run line above",
    TASK_NOT_PICKED_UP: f"an AG2 Space task has been open over {TASK_PICKUP_S // 60} minutes: "
                        "the hook is not waking a worker for it, or a worker handed it off "
                        "without running hold",
    TASK_HELD: "a worker parked an AG2 Space task for your go (ag2-sparrow-hatch hold)",
    TASK_HELD_UNDELIVERED: f"a task was held over {HELD_LINE_DELIVER_S // 60} minutes ago and its "
                           "room notice is still queued, being sent, was parked after failed "
                           "sends or was withdrawn unsent, so the asker may have heard nothing: check Sparrow's proactive "
                           "drain and the room",
    TASK_HELD_UNCONFIRMED: f"a task was held over {HELD_LINE_DELIVER_S // 60} minutes ago and its "
                           "room notice left the queue, but its delivery can't be confirmed (no "
                           "receipt): check the room and Sparrow's log",
    UPGRADE_FAILED: "an automatic ag2-sparrow upgrade failed; its outcome and the version "
                    "installed now are in hatch-upgrade.json and hatch-upgrade.log in the "
                    "state dir",
}
RUN_TIMEOUT_S = 120
GAP_MINUTES = 16
# How long a spawned Sparrow must stay up to count as started (75 = lock held elsewhere).
SPAWN_SETTLE_S = 3.0
LOG_NAME = "keepalive-probe.log"
TAIL_BYTES = 4096
STOP_WAIT_S = 20.0
START_WAIT_S = 30.0


def default_cron_dir() -> Path:
    return _cli._home() / "workspace" / "cron.d" / "minutely"


def cron_dir(cfg: dict) -> Path:
    return Path(cfg["CRON_DIR"]) if cfg.get("CRON_DIR") else default_cron_dir()


def log_path(cfg: dict, override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    return Path(cfg["KEEPALIVE_LOG"]) if cfg.get("KEEPALIVE_LOG") else _cli._home() / LOG_NAME


def _state_dir(cfg: dict) -> Path:
    return Path(cfg.get("STATE_DIR") or _dirs.state_dir())


def _activity_file(cfg: dict) -> Path:
    return Path(cfg.get("ACTIVITY_FILE") or _cli._home() / ".muse" / "last_active")


def task_file(name: str = TASK, minutes: int = INTERVAL_MIN) -> str:
    return f"{name}__interval@{minutes}m.md"


def health_command(command: str) -> str:
    """REMOTE_HEALTH_COMMAND for Sparrow: this adapter's health row."""
    return shlex.join([command, "keepalive", "health"])


def keepawake_command(command: str) -> str:
    return f"timeout {RUN_TIMEOUT_S} {shlex.join([command, 'keepalive', 'keepawake'])}"


def register_step(cfg: dict) -> str:
    """What the agent must do: Hatch's scheduler does not run task files dropped on disk."""
    return (f"create a scheduled task through your scheduler (a file on disk is not enough): "
            f"name {TASK}, every {INTERVAL_MIN} minutes, running exactly: "
            f"{keepawake_command(_command(cfg))} ; then confirm it appears in your scheduled "
            "task list")


def render_task(command: str) -> str:
    return (f"{TASK_MARKER}\n# {TASK} (every {INTERVAL_MIN} minutes)\n\n"
            "Run this command once, then stop. It keeps this agent awake, restarts the AG2 "
            "Space listener (Sparrow) only if it is down, and appends one line to the "
            "keepalive log. Change nothing else, retry nothing, and post nothing anywhere.\n\n"
            f"```sh\n{keepawake_command(command)}\n```\n")


def _command(cfg: dict) -> str:
    return cfg.get("REPLY_COMMAND") or _cli.find_reply_command() or _cli.REPLY_COMMAND


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _parse_utc(text: str) -> float | None:
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _instance(cfg: dict) -> str:
    return (cfg["GATEWAY_INSTANCE"] if "GATEWAY_INSTANCE" in cfg
            else os.environ.get("GATEWAY_INSTANCE") or "").strip()


def listener_up(cfg: dict, now: float) -> bool:
    """Running, connected or not: a rejected token is not fixed by a restart."""
    code, _ = _cli.check_sparrow(_state_dir(cfg), now=now, instance=_instance(cfg))
    return code != _cli.SPARROW_DOWN


def _found(directory: Path, name: str) -> "list[Path]":
    found = sorted(directory.glob(f"{name}__*.md"))
    return found + ([directory / f"{name}.md"] if (directory / f"{name}.md").exists() else [])


def _ours(path: Path) -> bool:
    return path.read_text(encoding="utf-8", errors="replace").startswith(TASK_MARKER)


def _install_task(directory: Path, text: str, force: bool) -> bool:
    """Write the task; a same-named task install did not write is kept unless `force`.
    False = kept, ours not installed."""
    target = directory / task_file()
    others = [p for p in _found(directory, TASK) if p != target]
    foreign = [p for p in [target, *others] if p.exists() and not _ours(p)]
    if foreign and not force:
        for p in foreign:
            print(f"NOTE keepalive: {p} was not written by install (a hand-made {TASK}?), so "
                  f"it was kept and {target.name} was not installed; re-run with --force to "
                  "replace it")
        return False
    for p in others:
        p.unlink()
        print(f"replaced: {p}" + (" (hand-made)" if p in foreign else ""))
    if target in foreign:
        print(f"replaced: {target} (hand-made)")
        _cli._write(target, text, 0o644)
        return True
    return _cli._install_file(target, text, 0o644, refuse_drift=False, force=True)


def _remove_old_tasks(directory: Path, force: bool) -> None:
    for name in OLD_TASKS:
        for p in _found(directory, name):
            ours = _ours(p)
            if ours or force:
                p.unlink()
                print(f"removed: {p} (replaced by {TASK})" + ("" if ours else " (hand-made)"))
            else:
                print(f"NOTE keepalive: {p} was not written by install, so it was kept; "
                      f"{TASK} replaces it, so remove it, or re-run with --force")


def install_tasks(cfg: dict, force: bool) -> None:
    """Install the keepawake task and remove the ones it replaces. Never fails the install."""
    directory = cron_dir(cfg)
    if _install_task(directory, render_task(_command(cfg)), force):
        print(f"keepalive: {directory / task_file()} (install record), log {log_path(cfg)}")
    _remove_old_tasks(directory, force)
    print(f"REQUIRED keepalive: {TASK} every {INTERVAL_MIN} minutes: "
          f"{keepawake_command(_command(cfg))} (register it through your scheduler)")


def uninstall_tasks(cfg: dict) -> int:
    directory, rc = cron_dir(cfg), 0
    found = [p for name in (TASK, *OLD_TASKS) for p in _found(directory, name)]
    if not found:
        print(f"nothing to remove: no {TASK} task in {directory}")
    for p in found:
        if _ours(p):
            p.unlink()
            print(f"removed: {p}")
        else:
            print(f"error: {p} was not written by install; remove it yourself", file=sys.stderr)
            rc = 1
    for name in ("hatch-keepalive.json", "hatch-keepalive-probe.json"):
        (_state_dir(cfg) / name).unlink(missing_ok=True)
    print(f"The keepalive log {log_path(cfg)} was kept.")
    return rc


def _spawn_sparrow(cfg: dict) -> "tuple[bool, str]":
    exe = _cli._find_console_script(_cli.SPARROW_COMMAND)
    if not exe:
        return False, f"{_cli.SPARROW_COMMAND} not found beside {sys.executable} or on PATH"
    mxid = _cli.saved_mxid(cfg.get("AGENT_MXID"))
    if not mxid:
        print(_cli.NO_SAVED_MXID, file=sys.stderr)
    env = agent_identity.launch_environ(os.environ, mxid)
    env.update(AG2_DEVICE_ENV=str(_cli.device_env_path()), REMOTE_HEALTH_COMMAND=health_command(_command(cfg)))
    for key, var in (("TASK_DIR", "AGENT_CONNECT_TASK_DIR"),
                     ("RESULT_DIR", "AGENT_CONNECT_RESULT_DIR"),
                     ("STATE_DIR", "AGENT_CONNECT_STATE_DIR")):
        if cfg.get(key):
            env[var] = cfg[key]
    if _instance(cfg):
        env["GATEWAY_INSTANCE"] = _instance(cfg)
    out = _state_dir(cfg) / "hatch-keepalive-sparrow.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out.open("ab") as fh:
            proc = subprocess.Popen([exe], env=env, stdin=subprocess.DEVNULL, stdout=fh,
                                    stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        return False, f"{exe} could not start ({e})"
    try:
        code = proc.wait(timeout=SPAWN_SETTLE_S)
    except subprocess.TimeoutExpired:
        return True, f"started {exe} (pid {proc.pid}, output in {out})"
    why = "another Sparrow holds the lock" if code == 75 else f"see {out}"
    return False, f"{exe} exited {code} at once ({why})"


def restart_listener(cfg: dict, bound: "tuple | None" = None) -> "tuple[bool | None, str]":
    """Through the user service install wrote when there is one, else a detached Sparrow. With
    `bound`, only the transition keepawake proved, re-read after the unit's own state: ("running",
    old pid, its lock) restarts the unit only while it runs that generation, else (False, why);
    ("stopped", old pid) starts it (`start` alone) only while it is down and no live
    Sparrow holds the lock, else (None, why), a current generation left untouched. Neither resets
    nor restarts on a mismatch."""
    owner = _unit_owner()
    if owner == UNIT_UNREACHABLE:
        return False, (f"{_cli.service_path()} is installed but systemctl --user did not answer; "
                       "no detached Sparrow is started beside that service")
    if bound is not None and owner != "ours":
        return False, f"{_cli.SERVICE_NAME} is no longer this install's unit; nothing was restarted"
    if owner == "ours":
        # A unit left failed by its start limit stays down until reset.
        props = _cli._unit_props()
        if props is None:
            return False, (f"systemctl --user show {_cli.SERVICE_NAME} did not give a well-formed state; "
                           "it was neither reset nor restarted")
        if bound is not None:
            why = _generation_moved(cfg, props, *bound[1:]) if bound[0] == "running" \
                else _not_stopped(cfg, props, bound[1])
            if why:
                return (False if bound[0] == "running" else None), f"{why}; nothing was reset or restarted"
        if bound is not None and bound[0] == "stopped":
            # Only `start`, a no-op for a unit that came up after the check; a failed unit that
            # needs reset-failed is left to the next pass's down-listener restart.
            r = _cli._systemctl(("--user", "start", "ag2-sparrow.service"))
            if _cli._ok(r):
                return True, f"systemctl --user start {_cli.SERVICE_NAME}"
            return False, f"systemctl --user start {_cli.SERVICE_NAME} failed ({_cli._why(r)})"
        _cli._systemctl(("--user", "reset-failed", "ag2-sparrow.service"))
        r = _cli._systemctl(("--user", "restart", "ag2-sparrow.service"))
        if _cli._ok(r):
            return True, f"systemctl --user restart {_cli.SERVICE_NAME}"
        return False, f"systemctl --user restart {_cli.SERVICE_NAME} failed ({_cli._why(r)})"
    return _spawn_sparrow(cfg)


_IDENTITY = ("pid", "acquired_at", "start", "proc_start", "send_lock")


def _generation_moved(cfg: dict, props: dict, old: int, lock: dict) -> "str | None":
    """Why the unit or the lock no longer runs the legacy generation `old` took as `lock`; None
    while the unit is exactly active with it as MainPID and the lock still proves that drain."""
    if props["ActiveState"] != "active" or props["MainPID"] != old:
        return (f"{_cli.SERVICE_NAME} no longer runs the old Sparrow (pid {old}): "
                f"ActiveState={props['ActiveState']}, MainPID={props['MainPID']}")
    now = _cli._lock_record(_state_dir(cfg), _instance(cfg)) or {}
    if any(now.get(k) != lock.get(k) for k in _IDENTITY) or drain_predates_send_lock(cfg) != old:
        return f"the lock no longer names the legacy drain (pid {old}) keepawake selected"
    return None


def _not_stopped(cfg: dict, props: dict, old: int) -> "str | None":
    """Why the unit may not be started after `old` stopped: it is not down, or a live Sparrow
    holds the lock (a current generation came up meanwhile). None while both are clear."""
    if props["ActiveState"] not in ("inactive", "failed") or props["MainPID"] != 0:
        return (f"{_cli.SERVICE_NAME} came up while the old Sparrow (pid {old}) stopped: "
                f"ActiveState={props['ActiveState']}, MainPID={props['MainPID']}")
    holder = _cli.live_lock_holder(_cli._lock_record(_state_dir(cfg), _instance(cfg)))
    if holder is not None:
        return f"Sparrow pid {holder} took the lock while the old Sparrow (pid {old}) stopped"
    return None


def _proc(pid: int, name: str) -> "bytes | None":
    try:
        return Path(f"/proc/{pid}/{name}").read_bytes()
    except OSError:
        return None


def is_sparrow_argv(argv: "list[str]") -> bool:
    """The `ag2-sparrow` console script, run directly or by its Python, or `-m ag2_sparrow`."""
    if any(os.path.basename(a) == _cli.SPARROW_COMMAND for a in argv[:2]):
        return True
    return any(a == "-m" and b in ("ag2_sparrow", "ag2_sparrow.remote_gateway_bridge")
               for a, b in zip(argv, argv[1:]))


def is_sparrow_pid(pid: int) -> bool:
    """Whether `pid` runs a Sparrow command line (/proc, or sysctl on macOS); False when
    that cannot be read."""
    cmd = _proc(pid, "cmdline")
    if cmd is None:
        return is_sparrow_argv(workspace_lock.proc_argv(pid) or [])
    return is_sparrow_argv([a.decode(errors="replace") for a in cmd.split(b"\0") if a])


def sparrow_env(pid: int) -> "dict[str, str] | None":
    """The environment of Sparrow process `pid`; None when it cannot be read (no /proc, or
    another process now has that pid)."""
    env = _proc(pid, "environ")
    if env is None or not is_sparrow_pid(pid):
        return None
    pairs = (p.split(b"=", 1) for p in env.split(b"\0") if b"=" in p)
    return {k.decode(errors="replace"): v.decode(errors="replace") for k, v in pairs}


def proven_sparrow(cfg: dict, pid: int, lock: "dict | None") -> bool:
    """`pid` runs Sparrow now and is the process that took `lock`, this workspace's canonical
    lock (cli.canonical_lock), not a later one given its pid: its start time is the one the lock
    declares or, for a lock that declares none, a bounded epoch no later than the lock's (both
    whole seconds, so the same second passes and the next one does not). False whenever that
    cannot be read."""
    if not _cli.canonical_lock(lock, _state_dir(cfg), _instance(cfg)) or lock.get("pid") != pid \
            or not is_sparrow_pid(pid):
        return False
    start = agent_identity.start_time(pid)
    if start is None:
        return False
    if "start" in lock:
        return lock["start"] == start
    try:
        began = calendar.timegm(time.strptime(start, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return False
    return bounded_number(began, 1, time.time()) is not None and began <= math.floor(lock["acquired_at"])


def stop_holder(cfg: dict, pid: int) -> bool:
    """SIGTERM the lock holder `pid` through a pidfd opened before the lock is re-read and proved
    (proven_sparrow, and the record's precise proc_start where it names one), then wait; nothing
    is signalled without that handle or that proof."""
    try:
        handle = _pidfd_open(pid)
    except ProcessLookupError:
        return True
    except OSError as e:
        print(f"error: no stable process handle for pid {pid} (pidfd_open: {diagnostics.describe(e)}); "
              "it was not signalled", file=sys.stderr)
        return False
    if handle is None:
        print(f"error: no stable process handle for pid {pid} on this platform (no pidfd); it was "
              "not signalled: stop Sparrow by hand", file=sys.stderr)
        return False
    try:
        lock = _cli._lock_record(_state_dir(cfg), _instance(cfg))
        why = None if proven_sparrow(cfg, pid, lock) else \
            f"pid {pid} is not proven to be the Sparrow holding the lock"
        # A legacy lock predates proc_start; one that names it must match it exactly.
        if why is None and "proc_start" in (lock or {}):
            why = unproven_holder(lock, pid)
        if why:
            print(f"error: {why}; it was not signalled", file=sys.stderr)
            return False
        return _stop(pid, handle)
    finally:
        if isinstance(handle, int):
            os.close(handle)


def _pidfd_open(pid: int) -> "int | None":
    """A pidfd naming the process that has `pid` now; None where the platform has none
    (macOS). ProcessLookupError when no process has it; any other failure raises."""
    opener = getattr(os, "pidfd_open", None)
    if opener is None or not hasattr(signal, "pidfd_send_signal"):
        return None
    return opener(pid)


def _stop(pid: int, handle: "int | None" = None) -> bool:
    """SIGTERM to `pid` (through the pidfd `handle` when given), then wait for it to exit;
    never a SIGKILL."""
    try:
        if handle is None:
            os.kill(pid, signal.SIGTERM)
        else:
            signal.pidfd_send_signal(handle, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as e:
        print(f"error: cannot stop Sparrow (pid {pid}): {diagnostics.describe(e)}", file=sys.stderr)
        return False
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        if handle is not None:
            import select
            if select.select([handle], [], [], max(0.0, deadline - time.monotonic()))[0]:
                return True  # the pidfd turns readable when its process exits
            continue
        if _cli.process_gone(pid):
            return True
        time.sleep(0.2)
    print(f"error: Sparrow (pid {pid}) did not exit within {STOP_WAIT_S:g}s of SIGTERM",
          file=sys.stderr)
    return False


def health_env_problem(cfg: dict, pid: "int | None") -> "str | None":
    """None when Sparrow `pid` runs with this install's REMOTE_HEALTH_COMMAND, else
    NO_HEALTH_COMMAND, or UNKNOWN_ENV when its environment cannot be read."""
    env = sparrow_env(pid) if pid is not None else None
    if env is None:
        return UNKNOWN_ENV
    ok = env.get("REMOTE_HEALTH_COMMAND") == health_command(_command(cfg))
    return None if ok else NO_HEALTH_COMMAND


def stale_listener(cfg: dict, pid: int) -> "str | None":
    """Why Sparrow `pid` is not the listener this install configured, which it reads only at its
    start: its drain serves another id than the saved AGENT_MXID, or the token file changed after it
    started. None when neither is shown, including when its start time cannot be read."""
    wrong = _cli.live_identity_mismatch(cfg, _instance(cfg))
    if wrong is not None:
        return f"serves {wrong[1]}, not {cfg.get('AGENT_MXID')}, the AGENT_MXID install saved"
    device_env = _cli.device_env_path()
    try:
        changed = device_env.stat().st_mtime
        began = calendar.timegm(time.strptime(agent_identity.start_time(pid) or "",
                                              "%Y-%m-%dT%H:%M:%SZ"))
    except (OSError, ValueError):
        return None
    # The start time is whole seconds, so only a change a full second after it is later.
    return f"started before {device_env} changed" if changed >= began + 1 else None


def unmanaged_health_problem(cfg: dict) -> "tuple[int | None, str | None]":
    """For a Sparrow no unit runs: the pid holding its lock, and health_env_problem.
    (None, None) when a systemd unit (ours or hand-made) runs Sparrow."""
    if _unit_owner() is not None:
        return None, None
    pid = _cli.live_lock_holder(_cli._lock_record(_state_dir(cfg), _instance(cfg)))
    return pid, health_env_problem(cfg, pid)


def _is_epoch(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def drain_predates_send_lock(cfg: dict, now: "float | None" = None) -> "int | None":
    """The pid of the live, connected Sparrow that is not this package's drain: its own lane
    record, fresh or stale, lacks the send-lock protocol, or it has held its lock past
    LANE_GRACE_S with no record of its own, as a drain from before lane records (the released
    main) does. A drain whose lock declares its protocol is judged by that alone, so a lane record
    it could not write is no evidence against it. A record another process left under a reused
    pid is not its own. None otherwise: when the live pid is not proven to be the Sparrow that
    took the lock (proven_sparrow), and when the record is invalid or cannot be read."""
    state, instance = _state_dir(cfg), _instance(cfg)
    holder = _cli._lock_record(state, instance)
    pid = _cli.live_lock_holder(holder)
    if pid is None or _cli.check_sparrow(state, now=now, instance=instance)[0] != _cli.SPARROW_OK:
        return None
    if not proven_sparrow(cfg, pid, holder):
        return None  # the live pid is not proven to be the Sparrow that took this lock
    acquired = (holder or {}).get("acquired_at")
    if "send_lock" in holder:
        return None if holder["send_lock"] == delivery_destination.SEND_LOCK_PROTOCOL else pid
    lane, rec = agent_identity.lane_state(state, instance)
    if lane in (agent_identity.LANE_INVALID, agent_identity.LANE_UNREADABLE):
        print(f"lane record of Sparrow (pid {pid}) {lane}; not replacing it", file=sys.stderr)
        return None
    # Stale counts too: a drain slowed by its sends still declares what it honours.
    if agent_identity.owned_by(rec, pid, acquired):
        return None if rec.get("send_lock") == delivery_destination.SEND_LOCK_PROTOCOL else pid
    # This package's drain publishes its record on its first pass, every few seconds after.
    now = time.time() if now is None else now
    old = _is_epoch(acquired) and 0 <= now - acquired and now - acquired > LANE_GRACE_S
    return pid if old else None


def replace_sparrow(cfg: dict, pid: int, why: str = "") -> "tuple[bool, str]":
    """Stop Sparrow `pid`, which lacks this install's REMOTE_HEALTH_COMMAND (or `why`), and
    start one that has it."""
    print(f"replacing: Sparrow (pid {pid}) " + (why or "runs without "
          f"REMOTE_HEALTH_COMMAND={health_command(_command(cfg))}"))
    if not stop_holder(cfg, pid):
        return False, f"the old Sparrow (pid {pid}) did not stop"
    ok, how = restart_listener(cfg)
    return ok, (how if ok else
                f"{how}; the old Sparrow (pid {pid}) was stopped, so none is running now")


UNIT_UNREACHABLE = "unreachable"


def _unit_owner() -> "str | None":
    """"ours" for the unit install wrote, "other" for a hand-made one, None for no unit (or no
    systemctl), UNIT_UNREACHABLE for a unit whose user manager did not answer: not "no unit"."""
    unit = _cli.service_path()
    if not unit.is_file() or _cli._systemctl((), locate=True) is None:
        return None
    if not _cli.systemd_user_available():
        return UNIT_UNREACHABLE
    text = unit.read_text(encoding="utf-8", errors="replace")
    return "ours" if text.startswith(_cli.SERVICE_MARKER) else "other"


def _wall_from_monotonic_us(value: "int | str | None") -> "float | None":
    """A systemd *TimestampMonotonic property as wall-clock seconds; None when unset."""
    try:
        us = int(value or 0)
    except ValueError:
        return None
    return time.time() - (time.monotonic() - us / 1e6) if us > 0 else None


def _left_running(pid: int) -> None:
    print(f"NOTE sparrow: pid {pid} holds Sparrow's lock but its command line or environment "
          "cannot be read as Sparrow's, so it was left running as it is")


def _unit_state_unknown(cfg: dict, holder: "int | None", why: str) -> int:
    """Install stops and starts nothing while the unit's state is unknown; 0 only when the lock
    holder is proven to run Sparrow's command line, else 1 saying why."""
    head = (f"{_cli.service_path()} is installed but {why}, so whether that service runs Sparrow is "
            "unknown; install neither stops nor starts a Sparrow")
    if holder is not None and is_sparrow_pid(holder):
        print(f"NOTE sparrow: {head}. Sparrow (pid {holder}) was left as it is; run "
              "ag2-sparrow-hatch verify once the user manager answers")
        return 0
    if holder is not None:
        _left_running(holder)
        print(f"error: Sparrow is not proven running: {head}, and pid {holder} holds Sparrow's lock but "
              "is not proven to be Sparrow", file=sys.stderr)
        return 1
    print(f"error: Sparrow is not running: {head}. Start your user manager (systemctl --user) and "
          f"run install again, or remove {_cli.service_path()} if it is left over", file=sys.stderr)
    return 1


def resolve_sparrow(cfg: dict) -> "tuple[str | None, int | None, bool]":
    """(owner, holder, fresh): the unit's owner (_unit_owner), the pid in Sparrow's lock
    record that is still alive, heartbeat fresh or not, and whether that heartbeat is
    fresh. One reading for install, keepawake and the upgrader."""
    record = _cli._lock_record(_state_dir(cfg), _instance(cfg))
    pid = (record or {}).get("pid")
    alive = (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
             and not _cli.process_gone(pid))
    holder = pid if alive else None
    return _unit_owner(), holder, holder is not None and _cli.live_lock_holder(record) == holder


def process_start(pid: int) -> "str | None":
    return workspace_lock.proc_start(pid)


def unproven_holder(record: "dict | None", pid: int) -> "str | None":
    """Why `pid` is not proven to be the Sparrow that wrote lock `record`; None when it
    runs Sparrow's command line and has the start time the record names."""
    if not is_sparrow_pid(pid):
        return f"pid {pid} holds Sparrow's lock but is not proven to be Sparrow"
    recorded, now = (record or {}).get("proc_start"), process_start(pid)
    if not isinstance(recorded, str) or not recorded:
        why = "the lock record carries no process start time"
    elif now != recorded:
        why = f"it started at {now or 'an unreadable time'}, the record names {recorded}"
    else:
        return None
    return f"pid {pid} is not proven to be the process that wrote Sparrow's lock: {why}"


def stop_proven(record: "dict | None", pid: int) -> "tuple[bool, str | None]":
    """SIGTERM the lock holder `pid` and wait: (stopped, why it was not signalled). The
    proof (unproven_holder) is read after a pidfd is opened and the signal goes through
    that pidfd, so it reaches the proven process or none. With no pidfd (macOS, or one
    that could not be opened) nothing is signalled: a bare pid is never trusted."""
    try:
        handle = _pidfd_open(pid)
    except ProcessLookupError:
        return True, None
    except OSError as e:
        return False, (f"no stable process handle for pid {pid} (pidfd_open: "
                       f"{diagnostics.describe(e)})")
    if handle is None:
        return False, (f"no stable process handle for pid {pid} on this platform (no pidfd); "
                       "stop Sparrow by hand to upgrade")
    try:
        why = unproven_holder(record, pid)
        return (False, why) if why else (_stop(pid, handle), None)
    finally:
        if isinstance(handle, int):
            os.close(handle)


def _wait_gone(pid: int) -> bool:
    deadline = time.monotonic() + STOP_WAIT_S
    while not _cli.process_gone(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


def quiesce_sparrow(cfg: dict) -> "tuple[bool, str, str | None]":
    """Stop the Sparrow this install owns and prove it gone: (quiet, how, manager), where
    the manager ("unit" or "detached") is what starts it again. Refused, with nothing
    touched and no manager: a hand-made unit, one whose user manager did not answer, and
    a lock holder beside our unit not proven to be Sparrow. For our unit the unit is
    stopped and its process, and any Sparrow outside it, must be gone; with no unit the
    lock holder is stopped (SIGTERM, then wait), a stale heartbeat notwithstanding, but
    only once proven to be the process that wrote the lock (stop_proven: Sparrow's command
    line and the start time the record names, bound to the signal by a pidfd)."""
    owner, holder, _ = resolve_sparrow(cfg)
    record = _cli._lock_record(_state_dir(cfg), _instance(cfg))
    unit = _cli.service_path()
    if owner == UNIT_UNREACHABLE:
        return False, f"{unit} is installed but systemctl --user did not answer", None
    if owner == "other":
        return False, f"{unit} was not written by install, so its Sparrow is left alone", None
    if owner == "ours":
        props = _cli._unit_props()
        if props is None:
            return False, f"systemctl --user show {_cli.SERVICE_NAME} did not give a well-formed state", None
        main_pid = props["MainPID"]
        outside = holder if holder is not None and holder != main_pid else None
        unproven = unproven_holder(record, outside) if outside is not None else None
        if unproven:
            return False, f"{unproven}, beside {_cli.SERVICE_NAME}; nothing was signalled", None
        r = _cli._systemctl(("--user", "stop", "ag2-sparrow.service"))
        how = f"systemctl --user stop {_cli.SERVICE_NAME}"
        if not _cli._ok(r):
            return False, f"{how} failed ({_cli._why(r)})", "unit"
        if main_pid and not _wait_gone(main_pid):
            return False, f"{how}, but its Sparrow (pid {main_pid}) is still running", "unit"
        if outside is not None:
            stopped, unproven = stop_proven(record, outside)
            if unproven:
                return False, f"{how}; {unproven}; it was not signalled", "unit"
            if not stopped:
                return False, f"the Sparrow outside {_cli.SERVICE_NAME} (pid {outside}) did not stop", "unit"
            how += f"; stopped the Sparrow outside it (pid {outside})"
        manager = "unit"
    elif holder is None:
        how, manager = "Sparrow was not running", "detached"
    else:
        stopped, unproven = stop_proven(record, holder)
        if unproven:
            return False, f"{unproven}; nothing was signalled", None
        if not stopped:
            return False, f"Sparrow (pid {holder}) did not stop", "detached"
        how, manager = f"stopped Sparrow (pid {holder})", "detached"
    _, left, _ = resolve_sparrow(cfg)
    if left is not None:
        return False, f"{how}, but pid {left} still holds Sparrow's lock", manager
    return True, how, manager


def ensure_sparrow(cfg: dict, health: bool) -> int:
    """Leave a Sparrow running and connected: start one if none runs, replace one outside
    the installed service, one that is a stale_listener, or (with `health`) one that lacks
    REMOTE_HEALTH_COMMAND."""
    state, instance = _state_dir(cfg), _instance(cfg)
    started = time.time()
    owner, alive, fresh = resolve_sparrow(cfg)
    holder = alive if fresh else None
    if owner == UNIT_UNREACHABLE:
        return _unit_state_unknown(cfg, holder, "systemctl --user did not answer")
    if owner == "other":
        print(f"NOTE sparrow: {_cli.service_path()} was not written by install, so install "
              "neither starts nor replaces the Sparrow it runs; run ag2-sparrow-hatch verify")
        return 0
    start, stopped, since = False, None, None
    stale = stale_listener(cfg, alive) if owner is None and alive is not None else None
    if owner == "ours":
        props = _cli._unit_props()
        if props is None:
            return _unit_state_unknown(cfg, holder, f"systemctl --user show {_cli.SERVICE_NAME} did not "
                                       "give a well-formed state")
        active = props["ActiveState"] in ("active", "reloading")
        if active:
            # install_service may have just restarted the unit: only its new process counts.
            since = _wall_from_monotonic_us(props.get("ExecMainStartTimestampMonotonic")) \
                or started
            why = stale_listener(cfg, props["MainPID"]) if props["MainPID"] else None
            if why:
                print(f"replacing: Sparrow (pid {props['MainPID']}) {why}")
                start = True
        else:
            device_env = _cli.device_env_path()
            if not device_env.is_file():
                print(f"error: Sparrow was not started: {device_env} is missing, so "
                      f"{_cli.SERVICE_NAME} was left stopped. Put your relay token in it as one "
                      "line REMOTE_TASK_TOKEN=<token>, chmod 600 it, then run: systemctl --user "
                      f"start {_cli.SERVICE_NAME}", file=sys.stderr)
                return 1
            if holder is not None and holder != props["MainPID"]:
                if not is_sparrow_pid(holder):
                    _left_running(holder)
                    print(f"error: {_cli.SERVICE_NAME} was not started while pid {holder} holds "
                          "Sparrow's lock; run install again once it exits", file=sys.stderr)
                    return 1
                print(f"replacing: the Sparrow outside {_cli.SERVICE_NAME} (pid {holder})")
                if not stop_holder(cfg, holder):
                    return 1
                stopped = holder
            start = True
    elif stale:
        ok, how = replace_sparrow(cfg, alive, stale)
        if not ok:
            # Without a pidfd nothing is signalled (stop_proven), so the stop is left to the user.
            print(f"error: Sparrow was not replaced: {how}. Stop it with: kill -TERM {alive} ; then "
                  "run ag2-sparrow-hatch install again", file=sys.stderr)
            return 1
        print(f"started: {how}")
        return _await_connected(state, instance, started)
    elif holder is not None:
        problem = health_env_problem(cfg, holder)
        if problem == UNKNOWN_ENV:
            _left_running(holder)
        elif health and problem == NO_HEALTH_COMMAND:
            ok, how = replace_sparrow(cfg, holder)
            if not ok:
                print(f"error: Sparrow was not replaced: {how}", file=sys.stderr)
                return 1
            print(f"started: {how}")
            return _await_connected(state, instance, started)
    else:
        start = not listener_up(cfg, started)
    if start:
        ok, how = restart_listener(cfg)
        if not ok:
            gone = "" if stopped is None else \
                f"; the old Sparrow (pid {stopped}) was stopped, so none is running now"
            print(f"error: Sparrow did not start: {how}{gone}", file=sys.stderr)
            return 1
        print(f"started: {how}")
    return _await_connected(state, instance, started if start else since)


def _connected_since(state: Path, instance: str, since: "float | None") -> "tuple[bool, str]":
    """Whether Sparrow reports running and connected (after `since`, when given), and its detail."""
    code, detail = _cli.check_sparrow(state, instance=instance)
    try:
        ts = bounded_number(json.loads(_cli.sparrow_status_path(state, instance).read_text(
            encoding="utf-8"))["ts"], 0, 1e11)
    except (OSError, ValueError, KeyError, TypeError):
        ts = None
    return code == _cli.SPARROW_OK and (since is None or (ts is not None and ts >= int(since))), detail


def _await_connected(state: Path, instance: str, since: "float | None") -> int:
    """0 once Sparrow reports running and connected (after `since`, when given), else 1."""
    deadline = time.monotonic() + START_WAIT_S
    while True:
        ok, detail = _connected_since(state, instance, since)
        if ok:
            print(f"sparrow: {detail}")
            return 0
        if time.monotonic() >= deadline:
            print(f"error: Sparrow is not running and connected {START_WAIT_S:g}s after install: "
                  f"{detail}", file=sys.stderr)
            return 1
        time.sleep(0.5)


def _status_snapshot(state: Path, instance: str) -> "dict | None":
    """Sparrow's status file, read once; None when it cannot be read as an object."""
    try:
        status = json.loads(_cli.sparrow_status_path(state, instance).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return status if isinstance(status, dict) else None


def _successor(cfg: dict, old: "tuple[int, str | None]", since: float) -> "tuple[int | None, str]":
    """The pid of a Sparrow other than `old` (pid, start) proven to hold the lock, whose own
    status, read in one snapshot, names its pid and start and says connected, written after
    `since`; else None and what is missing."""
    state, instance = _state_dir(cfg), _instance(cfg)
    lock, file_id = _cli.lock_snapshot(state, instance)
    pid = _cli.live_lock_holder(lock)
    if pid is None:
        return None, "no live Sparrow holds the lock"
    start = agent_identity.start_time(pid)
    if (pid, start) == old:
        return None, f"the old Sparrow (pid {pid}) still holds the lock"
    if start is None or not proven_sparrow(cfg, pid, lock):
        return None, f"pid {pid} holds the lock but is not proven to be the Sparrow that took it"
    gen = (lock or {}).get("proc_start")
    if not isinstance(gen, str) or not gen or process_start(pid) != gen:
        return None, f"pid {pid} is not proven to be the generation its lock names (proc_start)"
    status = _status_snapshot(state, instance) or {}
    if status.get("pid") != pid or status.get("start") != start or status.get("proc_start") != gen:
        return None, f"the status file was not written by Sparrow pid {pid}"
    at = bounded_number(status.get("written_at"), 0, 1e11)
    if at is None or not since < at <= time.time() + _cli.SPARROW_STATUS_FUTURE_SKEW_S:
        return None, f"Sparrow pid {pid} has written no status since the restart"
    if status.get("connected") is not True:
        return None, f"Sparrow pid {pid} is running but not connected"
    now, now_id = _cli.lock_snapshot(state, instance)
    if file_id is None or now_id != file_id or not _cli.canonical_lock(now, state, instance) \
            or any(now.get(k) != lock.get(k) for k in _IDENTITY) or process_start(pid) != gen:
        return None, f"the lock moved off Sparrow pid {pid} while its status was read"
    return pid, f"Sparrow pid {pid} now holds the lock and is connected"


def _await_successor(cfg: dict, old: "tuple[int, str | None]", since: float) -> "tuple[bool, str]":
    """True once _successor finds one; False after START_WAIT_S, saying what is missing."""
    deadline = time.monotonic() + START_WAIT_S
    while True:
        pid, detail = _successor(cfg, old, since)
        if pid is not None:
            return True, detail
        if time.monotonic() >= deadline:
            return False, f"no successor holds the lock and reports connected {START_WAIT_S:g}s later ({detail})"
        time.sleep(0.5)


def replace_old_drain(cfg: dict, old: int) -> "tuple[bool | None, str]":
    """Replace `old`, the legacy drain keepawake selected, re-proving it at the action: None when
    the lock no longer names it (the generation rolled: nothing is touched). The unit is
    restarted in place only when it is active with `old` as its MainPID, and restart_listener
    re-proves both right before it; otherwise `old` is an outside holder, stopped through
    stop_holder first, and the unit started only while still down with no live holder. True only
    once a successor holds the lock and is connected (_successor)."""
    owner = _unit_owner()
    if owner == "other":
        return False, "a hand-made unit runs it; restart that unit"
    if owner == UNIT_UNREACHABLE:
        return False, f"{_cli.service_path()} is installed but systemctl --user did not answer; nothing was restarted"
    props = _cli._unit_props() if owner == "ours" else None
    if owner == "ours" and props is None:
        return False, (f"systemctl --user show {_cli.SERVICE_NAME} did not give a well-formed state; "
                       "nothing was restarted")
    since = time.time()
    lock = _cli._lock_record(_state_dir(cfg), _instance(cfg))
    if drain_predates_send_lock(cfg) != old:
        return None, f"the lock no longer names the legacy drain (pid {old}); it was left untouched"
    identity = (old, agent_identity.start_time(old))
    runs_it = props is not None and props["ActiveState"] in ("active", "reloading") \
        and props["MainPID"] == old
    if runs_it:
        ok, how = restart_listener(cfg, bound=("running", old, lock or {}))
    elif owner == "ours":
        print(f"replacing: Sparrow (pid {old}) predates the hold-line send lock and runs outside "
              f"{_cli.SERVICE_NAME} (ActiveState={props['ActiveState']}, MainPID={props['MainPID']})")
        if not stop_holder(cfg, old):
            return False, f"the old Sparrow (pid {old}) was not stopped; the unit was not restarted"
        ok, how = restart_listener(cfg, bound=("stopped", old))
        if ok is None:
            print(f"not restarted: {how}")
            ok = True  # the generation that came up is judged as the successor below
    else:
        ok, how = replace_sparrow(cfg, old, "predates the hold-line send lock")
    if not ok:
        return False, how
    ok, detail = _await_successor(cfg, identity, since)
    return ok, f"{how}; {detail}"


def keepawake(cfg: dict, log: Path, now: float | None = None) -> int:
    from . import self_upgrade
    now = time.time() if now is None else now
    settled, blocked = None, None
    # Held across the probe and the restart, so an upgrade cannot start in between.
    with self_upgrade.upgrade_lock(_state_dir(cfg)) as free:
        if free:
            # A dead upgrade is settled first: a restart must never run its unverified target.
            settled, blocked = _settle_upgrade(cfg, now)
        if free and not blocked:
            up, restarted, rc, reason = _keep_listener(cfg, now)
        else:
            up, restarted, rc, reason = listener_up(cfg, now), 0, 0, None
    if not free:
        print("an upgrade is running; Sparrow is left as it is")
    elif blocked:
        rc, reason = 1, UPGRADE_FAILED
        print(f"error: Sparrow is not restarted: {blocked}; run ag2-sparrow-hatch install to "
              "put a verified version back", file=sys.stderr)
    upgrade = _served_setup(cfg, settled, blocked) if free else "running"
    line = f"{_utc(now)} keepawake listener={'up' if up else 'down'} restarted={restarted}" \
        + (f" reason={reason}" if reason else "") + (f" upgrade={upgrade}" if upgrade else "")
    _log_line(log, line)
    return rc


def _log_line(log: Path, line: str) -> None:
    """Append to the keepalive log, best effort: a log that cannot be written goes to stderr."""
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as e:
        print(f"keepalive log not written ({diagnostics.describe(e)})", file=sys.stderr)
    print(line)


def _keep_listener(cfg: dict, now: float) -> "tuple[bool, int, int, str | None]":
    """Restart Sparrow if it is down, or replace one without the health command:
    (up, restarted, rc, reason)."""
    up, restarted, rc, reason = listener_up(cfg, now), 0, 0, None
    if not up:
        ok, how = restart_listener(cfg)
        if ok:
            restarted = 1
            print(f"listener was down; restarted: {how}")
        else:
            print(f"listener is down and the restart failed: {how}", file=sys.stderr)
            rc = 1
    else:
        pid, reason = unmanaged_health_problem(cfg)
        old = drain_predates_send_lock(cfg) if reason != NO_HEALTH_COMMAND else None
        if reason == NO_HEALTH_COMMAND or old is not None:
            if old is not None:
                reason = OLD_DRAIN
                ok, how = replace_old_drain(cfg, old)
            else:
                ok, how = replace_sparrow(cfg, pid)
            if ok is None:
                print(f"not replaced: {how}")
            elif ok:
                restarted = 1
                print(f"restarted: {how}")
            else:
                print(f"the replace failed: {how}", file=sys.stderr)
                rc = 1
    return up, restarted, rc, reason


def _settle_upgrade(cfg: dict, now: float) -> "tuple[str | None, str | None]":
    """Settle a dead upgrade attempt, with the upgrade lock held: (verdict, why Sparrow must
    not be started). A settlement that raises blocks too; never fails the run."""
    from . import self_upgrade
    try:
        settled = self_upgrade.settle_attempt(cfg, _state_dir(cfg), now)
    except self_upgrade.JournalError:
        return None, self_upgrade.recovery_blocked(_state_dir(cfg))
    except Exception as e:  # noqa: BLE001 - keepawake's own job must still be logged
        print(f"NOTE upgrade: {diagnostics.describe(e)}", file=sys.stderr)
        return None, f"the upgrade attempt could not be settled ({type(e).__name__})"
    return settled, self_upgrade.recovery_blocked(_state_dir(cfg))


def _served_setup(cfg: dict, settled: "str | None" = None,
                  blocked: "str | None" = None) -> "str | None":
    """Refresh the served setup and decide on the upgrade it calls for; never fails the run.
    `settled` is this run's verdict on a dead attempt, which is the line's decision; while
    `blocked` (a held recovery) no upgrade is started."""
    from . import runtime_text, self_upgrade
    try:
        setup = runtime_text.refresh_cache(_state_dir(cfg), _instance(cfg))
        if blocked:
            return settled or "recovery-blocked"
        return settled or self_upgrade.maybe_start(cfg, setup)
    except Exception as e:  # noqa: BLE001 - keepawake's own job must still be logged
        print(f"NOTE served setup: {diagnostics.describe(e)}", file=sys.stderr)
        return settled


def last_keepawake(log: Path) -> float | None:
    """The time of the log's last keepawake line, or None."""
    try:
        with log.open("rb") as fh:
            fh.seek(max(0, fh.seek(0, os.SEEK_END) - TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        parts = line.split()
        if len(parts) > 1 and parts[1] == "keepawake":
            ts = _parse_utc(parts[0])
            if ts is not None:
                return ts
    return None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _within(now: float, ts: "float | None", limit: float = STALE_S) -> bool:
    """`ts` at most `limit` old; one from the future (a clock stepped back) is not."""
    return ts is not None and 0 <= now - ts <= limit


def hook_last_run(cfg: dict) -> "float | None":
    """The hook's last run; one that never ran ages from install's last script write."""
    run = _cli.hook_last_run(_state_dir(cfg))
    return run[0] if run is not None else _mtime(_cli.installed_marker_path())


def open_tasks(cfg: dict, now: float) -> "tuple[float | None, float | None, dict]":
    """(mtime of the oldest open task nobody holds, earliest deliberate hold, {outcome: earliest
    hold} for holds whose room line is 'undelivered' or 'unconfirmed') across every instance,
    the agent's own tasks excepted."""
    agent = (cfg.get("AGENT_MXID") or "").lower()
    result_dir = Path(cfg.get("RESULT_DIR") or _dirs.result_dir())
    try:
        items = task_fields.list_open_tasks(Path(cfg.get("TASK_DIR") or _dirs.task_dir()),
                                            result_dir, instance=None)
    except OSError:
        return None, None, {}
    oldest = held = None
    stuck: dict = {}
    for item in items:
        if (item.get("sender") or "").lower() == agent:
            continue
        mark = task_hold.read(result_dir, item["task_id"])
        ts = _mtime(Path(item["task_file"]))
        # A task whose answer the outbox already has is a failed cleanup, never a hold.
        if mark is not None and not _cli.task_settled(result_dir, item["task_id"]):
            held = min(t for t in (held, mark["since"]) if t is not None)
            outcome = _cli.hold_line_outcome(result_dir, item["task_id"])
            # The task is open, so a line withdrawn at its send left the asker with nothing.
            outcome = "undelivered" if outcome == "withdrawn" else outcome
            if outcome != "delivered":
                stuck[outcome] = min(t for t in (stuck.get(outcome), mark["since"]) if t is not None)
        elif oldest is None and ts is not None:
            oldest = ts
    return oldest, held, stuck


def health_row(cfg: dict, log: Path, now: float | None = None) -> dict | None:
    """The core row for Sparrow's heartbeat; None while no keepawake task is installed.
    `alive` also counts the hook's activity marker."""
    from . import runtime_text, self_upgrade
    now = time.time() if now is None else now
    tasks = _found(cron_dir(cfg), TASK)
    if not tasks:
        return None
    tune = runtime_text.tunables(runtime_text.current_cache(_state_dir(cfg), _instance(cfg)))
    # The installed task runs every INTERVAL_MIN, so a shorter served interval cannot tighten it.
    stale_s = 2 * max(tune["keepawake_interval_s"], INTERVAL_MIN * 60)
    hook_stale_s = tune["hook_silent_age_s"]
    ran = last_keepawake(log)
    # Install leaves an unchanged task's mtime alone, so a task that never ran ages from it.
    last = ran if ran is not None else max(
        (m for m in map(_mtime, tasks) if m is not None), default=now)
    fresh = _within(now, last, stale_s)
    alive = True if (fresh and ran is not None) or _within(now, _mtime(_activity_file(cfg))) \
        else None
    problems = [] if fresh else [(NOT_RUNNING, last + stale_s)]
    hook = hook_last_run(cfg)
    if hook is not None and not _within(now, hook, hook_stale_s):
        problems.append((HOOK_NOT_RUNNING, hook + hook_stale_s))
    oldest, held, stuck = open_tasks(cfg, now)
    if oldest is not None and now - oldest > tune["stuck_task_age_s"]:
        problems.append((TASK_NOT_PICKED_UP, oldest + tune["stuck_task_age_s"]))
    for outcome, reason in (("undelivered", TASK_HELD_UNDELIVERED), ("unconfirmed", TASK_HELD_UNCONFIRMED)):
        since = stuck.get(outcome)
        if since is not None and now - since > HELD_LINE_DELIVER_S:
            problems.append((reason, since + HELD_LINE_DELIVER_S))
    failed = self_upgrade.failed_since(_state_dir(cfg))
    if failed is not None:
        problems.append((UPGRADE_FAILED, failed))
    # Fresh but never run: no evidence either way yet.
    condition = "abnormal" if problems else "healthy" if ran is not None else "unknown"
    reason, since = (problems[0] if problems else (TASK_HELD, held) if held is not None
                     else (None, None))
    return {"alive": alive, "motion": "unknown", "condition": condition,
            "reason": reason, "since": since}


def status_line(cfg: dict, now: float | None = None) -> "tuple[str, dict | None]":
    """What keepalive status prints, and the health row. Observes only."""
    now = time.time() if now is None else now
    log = log_path(cfg)
    last, row = last_keepawake(log), health_row(cfg, log, now)
    health = ("none" if row is None else row["condition"]
              + (f" ({row['reason']})" if row["reason"] else ""))
    up = listener_up(cfg, now)
    # The row cannot carry this: a Sparrow without the command never asks for the row.
    flag = up and unmanaged_health_problem(cfg)[1] == NO_HEALTH_COMMAND
    return (f"{_utc(now)} listener={'up' if up else 'down'} "
            f"last_keepawake={_utc(last) if last is not None else 'never'} health={health}"
            + (f" flag=sparrow-{NO_HEALTH_COMMAND}" if flag else ""), row)


def report(log: Path, gap_minutes: float = GAP_MINUTES) -> int:
    """Print the log's gaps over `gap_minutes`, its listener-down lines and restarts."""
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        print(f"error: cannot read {log}: {diagnostics.describe(e)}", file=sys.stderr)
        return 1
    rows, unreadable = [], 0
    for line in lines:
        parts = line.split()
        ts = _parse_utc(parts[0]) if parts else None
        if ts is None:
            unreadable += bool(parts)
            continue
        rows.append((ts, dict(p.split("=", 1) for p in parts[1:] if "=" in p), parts[1:]))
    if not rows:
        print(f"{log}: no keepalive lines")
        return 0
    rows.sort(key=lambda r: r[0])
    print(f"{log}: {len(rows)} lines, {_utc(rows[0][0])} to {_utc(rows[-1][0])}"
          + (f" ({unreadable} unreadable lines skipped)" if unreadable else ""))
    gaps = [(a[0], b[0]) for a, b in zip(rows, rows[1:]) if b[0] - a[0] > gap_minutes * 60]
    print(f"gaps over {gap_minutes:g} min: {len(gaps)}"
          + (" (the scheduler or the VM did not run)" if gaps else ""))
    for start, end in gaps:
        print(f"  {_utc(start)} -> {_utc(end)}  {round((end - start) / 60)} min")
    restarts = 0
    for _ts, fields, _words in rows:
        for key in ("restarted", "listener_restarts"):
            try:
                restarts += int(fields.get(key, 0))
            except ValueError:
                pass
    print(f"listener down at a run: {sum(f.get('listener') == 'down' for _, f, _ in rows)}")
    print(f"listener restarts: {restarts}")
    old = [(f, w) for _, f, w in rows if "watchdog" in f]
    if old:
        print(f"earlier probe lines: {len(old)}; watchdog down at one: "
              f"{sum(f['watchdog'] == 'down' for f, _ in old)}; restarted by the probe: "
              f"{sum('watchdog_restarted' in w for _, w in old)}")
    return 0


def main(args) -> int:
    _cli._register_device_secrets()  # the cache's revision rule withholds this host's secrets
    cfg = _cli._installed_config()
    log = log_path(cfg, args.log)
    if args.action == "keepawake":
        return keepawake(cfg, log)
    if args.action == "health":
        row = health_row(cfg, log)
        if row is not None:
            print(json.dumps(row, sort_keys=True))
        return 0
    if args.action == "status":
        print(status_line(cfg)[0])
        return 0
    return report(log, args.gap_minutes)
