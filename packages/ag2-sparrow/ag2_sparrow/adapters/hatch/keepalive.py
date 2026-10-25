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
(`hook-not-running`) or a task has waited over TASK_PICKUP_S (`task-not-picked-up`).
`keepalive report` lists the log's gaps, and reads the lines of the earlier
watchdog-and-probe pair too."""
from __future__ import annotations

import datetime
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from ... import _dirs, agent_identity, diagnostics
from . import cli as _cli, task_fields

TASK, INTERVAL_MIN = "sparrow-keepawake", 15
# Tasks an earlier install wrote; re-running install removes them.
OLD_TASKS = ("sparrow-watchdog", "keepalive-probe")
TASK_MARKER = "<!-- Written by ag2-sparrow-hatch install; re-run install to update it. -->"
STALE_S = 2 * INTERVAL_MIN * 60
NOT_RUNNING = "keepawake-not-running"
HOOK_NOT_RUNNING, HOOK_STALE_S = "hook-not-running", 5 * 60
TASK_NOT_PICKED_UP, TASK_PICKUP_S = "task-not-picked-up", 15 * 60
NO_HEALTH_COMMAND, UNKNOWN_ENV = "no-health-command", "unknown-env"
REASON_HINTS = {
    HOOK_NOT_RUNNING: f"the hook has not run in the last {HOOK_STALE_S // 60} minutes; see "
                      "the hook run line above",
    TASK_NOT_PICKED_UP: f"an AG2 Space task has been open over {TASK_PICKUP_S // 60} minutes: "
                        "the hook is not waking a worker for it, or a worker handed it off",
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


def restart_listener(cfg: dict) -> "tuple[bool, str]":
    """Through the user service install wrote when there is one, else a detached Sparrow."""
    owner = _unit_owner()
    if owner == UNIT_UNREACHABLE:
        return False, (f"{_cli.service_path()} is installed but systemctl --user did not answer; "
                       "no detached Sparrow is started beside that service")
    if owner == "ours":
        # A unit left failed by its start limit stays down until reset.
        if _cli._unit_props() is None:
            return False, (f"systemctl --user show {_cli.SERVICE_NAME} did not give a well-formed state; "
                           "it was neither reset nor restarted")
        _cli._systemctl(("--user", "reset-failed", "ag2-sparrow.service"))
        r = _cli._systemctl(("--user", "restart", "ag2-sparrow.service"))
        if _cli._ok(r):
            return True, f"systemctl --user restart {_cli.SERVICE_NAME}"
        return False, f"systemctl --user restart {_cli.SERVICE_NAME} failed ({_cli._why(r)})"
    return _spawn_sparrow(cfg)


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
    """Whether `pid` runs a Sparrow command line; False when that cannot be read."""
    cmd = _proc(pid, "cmdline")
    return is_sparrow_argv([a.decode(errors="replace") for a in (cmd or b"").split(b"\0") if a])


def sparrow_env(pid: int) -> "dict[str, str] | None":
    """The environment of Sparrow process `pid`; None when it cannot be read (no /proc, or
    another process now has that pid)."""
    env = _proc(pid, "environ")
    if env is None or not is_sparrow_pid(pid):
        return None
    pairs = (p.split(b"=", 1) for p in env.split(b"\0") if b"=" in p)
    return {k.decode(errors="replace"): v.decode(errors="replace") for k, v in pairs}


def _stop(pid: int) -> bool:
    """SIGTERM to `pid`, then wait for it to exit; never a SIGKILL."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as e:
        print(f"error: cannot stop Sparrow (pid {pid}): {diagnostics.describe(e)}", file=sys.stderr)
        return False
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
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


def unmanaged_health_problem(cfg: dict) -> "tuple[int | None, str | None]":
    """For a Sparrow no unit runs: the pid holding its lock, and health_env_problem.
    (None, None) when a systemd unit (ours or hand-made) runs Sparrow."""
    if _unit_owner() is not None:
        return None, None
    pid = _cli.live_lock_holder(_cli._lock_record(_state_dir(cfg), _instance(cfg)))
    return pid, health_env_problem(cfg, pid)


def replace_sparrow(cfg: dict, pid: int) -> "tuple[bool, str]":
    """Stop Sparrow `pid`, which lacks this install's REMOTE_HEALTH_COMMAND, and start one
    that has it."""
    print(f"replacing: Sparrow (pid {pid}) runs without "
          f"REMOTE_HEALTH_COMMAND={health_command(_command(cfg))}")
    if not _stop(pid):
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


def ensure_sparrow(cfg: dict, health: bool) -> int:
    """Leave a Sparrow running and connected: start one if none runs, replace one outside
    the installed service, or (with `health`) one that lacks REMOTE_HEALTH_COMMAND."""
    state, instance = _state_dir(cfg), _instance(cfg)
    started = time.time()
    holder = _cli.live_lock_holder(_cli._lock_record(state, instance))
    owner = _unit_owner()
    if owner == UNIT_UNREACHABLE:
        return _unit_state_unknown(cfg, holder, "systemctl --user did not answer")
    if owner == "other":
        print(f"NOTE sparrow: {_cli.service_path()} was not written by install, so install "
              "neither starts nor replaces the Sparrow it runs; run ag2-sparrow-hatch verify")
        return 0
    start, stopped, since = False, None, None
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
                if not _stop(holder):
                    return 1
                stopped = holder
            start = True
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


def _await_connected(state: Path, instance: str, since: "float | None") -> int:
    """0 once Sparrow reports running and connected (after `since`, when given), else 1."""
    path = _cli.sparrow_status_path(state, instance)
    deadline = time.monotonic() + START_WAIT_S
    while True:
        code, detail = _cli.check_sparrow(state, instance=instance)
        try:
            ts = float(json.loads(path.read_text(encoding="utf-8"))["ts"])
        except (OSError, ValueError, KeyError, TypeError):
            ts = None
        if code == _cli.SPARROW_OK and (since is None or (ts is not None and ts >= int(since))):
            print(f"sparrow: {detail}")
            return 0
        if time.monotonic() >= deadline:
            print(f"error: Sparrow is not running and connected {START_WAIT_S:g}s after install: "
                  f"{detail}", file=sys.stderr)
            return 1
        time.sleep(0.5)


def keepawake(cfg: dict, log: Path, now: float | None = None) -> int:
    now = time.time() if now is None else now
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
        if reason == NO_HEALTH_COMMAND:
            ok, how = replace_sparrow(cfg, pid)
            if ok:
                restarted = 1
                print(f"restarted: {how}")
            else:
                print(f"the replace failed: {how}", file=sys.stderr)
                rc = 1
    _refresh_setup(cfg)
    line = f"{_utc(now)} keepawake listener={'up' if up else 'down'} restarted={restarted}" \
        + (f" reason={reason}" if reason else "")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line)
    return rc


def _refresh_setup(cfg: dict) -> None:
    """Refresh the served setup cache; never fails the run."""
    from . import runtime_text
    try:
        runtime_text.refresh_cache(_state_dir(cfg), _instance(cfg))
    except Exception as e:  # noqa: BLE001 - keepawake's own job must still be logged
        print(f"NOTE served setup: {diagnostics.describe(e)}", file=sys.stderr)


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


def oldest_open_task(cfg: dict) -> "float | None":
    """mtime of the oldest task of any instance with no result, the agent's own excepted."""
    agent = (cfg.get("AGENT_MXID") or "").lower()
    try:
        items = task_fields.list_open_tasks(Path(cfg.get("TASK_DIR") or _dirs.task_dir()),
                                            Path(cfg.get("RESULT_DIR") or _dirs.result_dir()),
                                            instance=None)
    except OSError:
        return None
    for item in items:
        if (item.get("sender") or "").lower() != agent:
            ts = _mtime(Path(item["task_file"]))
            if ts is not None:
                return ts
    return None


def health_row(cfg: dict, log: Path, now: float | None = None) -> dict | None:
    """The core row for Sparrow's heartbeat; None while no keepawake task is installed.
    `alive` also counts the hook's activity marker."""
    from . import runtime_text
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
    oldest = oldest_open_task(cfg)
    if oldest is not None and now - oldest > tune["stuck_task_age_s"]:
        problems.append((TASK_NOT_PICKED_UP, oldest + tune["stuck_task_age_s"]))
    # Fresh but never run: no evidence either way yet.
    condition = "abnormal" if problems else "healthy" if ran is not None else "unknown"
    reason, since = problems[0] if problems else (None, None)
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
