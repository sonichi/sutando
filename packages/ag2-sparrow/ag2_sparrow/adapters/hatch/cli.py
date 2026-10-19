"""`ag2-sparrow-hatch install|uninstall|verify|sparrow|reply|keepalive`: set up, check and answer through the Hatch hook.

install writes the hook script to ~/hooks/scripts/ (a registered hook's script is never
rewritten: a changed one goes beside it), stages the hook definition for the agent's hooks
tool (~/hooks/definitions/ is the tool's own), writes the config the script reads, where systemd runs a user
manager the ag2-sparrow user service that restarts Sparrow, and the keepalive's
install record, then leaves Sparrow running and connected; verify checks that setup;
sparrow checks only that Sparrow is running (the hook's heartbeat runs it); reply
writes a worker's answer to its result_file and waits until Sparrow has sent it;
keepalive runs the scheduled task, prints Sparrow's health row, and reports on its log."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time
from importlib import resources
from pathlib import Path

from ... import _dirs, gateway_instance, outbox, workspace_lock
from ...result_markers import parse_markers
from .task_fields import item_id, valid_mxid

HOOK_ID = "ag2-sparrow-hatch"
SCRIPT_NAME = f"{HOOK_ID}.sh"
# The hook script rewrites this every run: "<epoch seconds> <sha256 of the script>".
HOOK_RUN_FILE = "hatch-hook-last-run"
HOOK_VERIFY_MAX_AGE_S = 120
# Hatch lists a definition missing any of these as invalid and never runs it.
HOOK_REQUIRED_KEYS = ("id", "version", "poll_interval_secs", "prompt", "script_path",
                      "script_timeout_secs")
HEARTBEAT_INTERVAL_S = 300
# Sparrow rewrites its status file every poll round (long poll or backoff, each
# at most about a minute), so an older file means it is not running.
SPARROW_STATUS_MAX_AGE_S = 180
# sha256 of hook scripts shipped before install recorded what it wrote (0.5.0).
RELEASED_SCRIPT_SHA256 = frozenset({
    "c68a6fce020a5aaea9f5c593021daccb95aeaf15ba6962d0303e42933ee23fb7",
})
INSTANCE_RE = gateway_instance.INSTANCE_RE
# `sparrow` exit codes, read by the hook; 1 stays a crash, 2 a usage error.
SPARROW_OK, SPARROW_DOWN, SPARROW_NOT_CONNECTED = 0, 10, 11
# The one command the hook prompt and worker_note tell a worker to run.
REPLY_COMMAND = "ag2-sparrow-hatch"
# install writes the absolute path here, so the worker's own PATH never decides it.
REPLY_PLACEHOLDER = "<REPLY_COMMAND>"
# `reply` exit codes: posted, not posted, usage, sent to Sparrow but not yet confirmed.
REPLY_POSTED, REPLY_NOT_POSTED, REPLY_UNCONFIRMED = 0, 1, 3
REPLY_WAIT_S, REPLY_WAIT_MAX_S, REPLY_MAX_BYTES = 30.0, 300.0, 64 * 1024
SPARROW_COMMAND = "ag2-sparrow"
SERVICE_NAME = "ag2-sparrow.service"
# First line of every unit install writes: a unit without it is the owner's and is kept.
SERVICE_MARKER = "# Written by ag2-sparrow-hatch install; re-run install to update it."
SYSTEMD_TIMEOUT_S = 30


def _shipped(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8")


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def scripts_dir() -> Path:
    return _home() / "hooks" / "scripts"


def definitions_dir() -> Path:
    return _home() / "hooks" / "definitions"


def config_path() -> Path:
    env = os.environ.get("SPARROW_HATCH_CONFIG")
    return Path(env) if env else _home() / ".config" / HOOK_ID / "config"


def installed_marker_path() -> Path:
    """sha256 of each hook script install wrote, one per line, so an upgrade can tell its
    own earlier copy from a local edit; its mtime is when install last wrote a script."""
    return config_path().with_name("installed-script.sha256")


def staged_definition_path() -> Path:
    """Where install puts a changed hook definition for the agent's hooks tool."""
    return config_path().with_name("hook-definition.json")


def hook_run_path(state_dir: Path) -> Path:
    return state_dir / HOOK_RUN_FILE


def hook_last_run(state_dir: Path) -> "tuple[float, str] | None":
    """(when, script sha256) of the hook's last run, or None when it has not written one."""
    try:
        parts = hook_run_path(state_dir).read_text(encoding="utf-8").split()
        return float(parts[0]), (parts[1] if len(parts) > 1 else "")
    except (OSError, ValueError, IndexError):
        return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ours() -> frozenset:
    try:
        recorded = installed_marker_path().read_text(encoding="utf-8").split()
    except OSError:
        recorded = []
    return RELEASED_SCRIPT_SHA256 | set(recorded)


def _record_script(sha: str) -> None:
    try:
        recorded = installed_marker_path().read_text(encoding="utf-8").split()
    except OSError:
        recorded = []
    _write(installed_marker_path(), "".join(f"{s}\n" for s in
                                            [r for r in recorded if r != sha] + [sha]), 0o644)


def _diff(old: str, new: str, path: Path) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"{path} (installed)", f"{path} (ag2-sparrow)"))


def _write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _install_file(path: Path, text: str, mode: int, *, refuse_drift: bool, force: bool,
                  ours: frozenset = frozenset()) -> bool:
    """Write `text` to `path`; print a diff for a differing copy that is not one of
    `ours` (sha256 of copies ag2-sparrow wrote). False = refused."""
    if path.exists():
        old = path.read_text(encoding="utf-8", errors="replace")
        if old == text:
            # Not even a chmod when the mode is right: Hatch stops a hook whose files change.
            if stat.S_IMODE(path.stat().st_mode) != mode:
                os.chmod(path, mode)
            print(f"unchanged: {path}")
            return True
        if _sha256(old) in ours:
            _write(path, text, mode)
            print(f"upgraded: {path}")
            return True
        print(f"DIFF: {path} differs from the ag2-sparrow copy:")
        print(_diff(old, text, path), end="")
        if refuse_drift and not force:
            print(f"REFUSED: {path} was modified locally; keep it, or re-run with --force "
                  "to replace it with the copy above", file=sys.stderr)
            return False
    _write(path, text, mode)
    print(f"installed: {path}")
    return True


def definition_path() -> Path:
    return definitions_dir() / f"{HOOK_ID}.json"


def render_definition(mxid: str, reply_command: str, script_path: str) -> str:
    d = json.loads(_shipped("hook-definition.json").replace("<AGENT_MXID>", mxid)
                   .replace(REPLY_PLACEHOLDER, reply_command))
    d["script_path"] = script_path
    return json.dumps(d, indent=2, ensure_ascii=False) + "\n"


def registered_definition() -> "dict | None":
    try:
        d = json.loads(definition_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def definition_matches(registered: dict, want: dict) -> bool:
    """Whether every field install sets has its value; the hooks tool may add others."""
    return all(registered.get(k) == v for k, v in want.items())


def _script_path(path: Path) -> str:
    return f"~/hooks/scripts/{path.name}"


def _stage_definition(want: str) -> Path:
    staged = staged_definition_path()
    try:
        same = staged.read_text(encoding="utf-8") == want
    except OSError:
        same = False
    if not same:
        _write(staged, want, 0o644)
    print(f"{'unchanged' if same else 'staged'}: {staged}")
    return staged


def install_hook(mxid: str, reply_command: str, force: bool) -> bool:
    """Stage the hook definition for the agent's hooks tool (REQUIRED hook:); never write
    ~/hooks/definitions, which the hooks tool owns. False = refused."""
    shipped = _shipped(SCRIPT_NAME)
    sha = _sha256(shipped)
    canonical, def_path = scripts_dir() / SCRIPT_NAME, definition_path()
    registered = registered_definition()
    if registered is None:
        fresh = not (canonical.is_file()
                     and canonical.read_text(encoding="utf-8", errors="replace") == shipped)
        if not _install_file(canonical, shipped, 0o755, refuse_drift=True, force=force,
                             ours=_ours()):
            return False
        if fresh:
            _record_script(sha)
        want = render_definition(mxid, reply_command, _script_path(canonical))
        staged = _stage_definition(want)
        print(f"REQUIRED hook: {staged} (register it through your hooks tool)")
        print(want, end="")
        return True
    script_path = str(registered.get("script_path") or _script_path(canonical))
    current = Path(os.path.expanduser(script_path))
    try:
        old = current.read_text(encoding="utf-8", errors="replace")
    except OSError:
        old = None
    if old != shipped:
        if old is not None and _sha256(old) not in _ours():
            print(f"DIFF: {current} differs from the ag2-sparrow copy:")
            print(_diff(old, shipped, current), end="")
            if not force:
                print(f"REFUSED: {current} was modified locally; keep it, or re-run with "
                      "--force to stage the copy above for your hooks tool", file=sys.stderr)
                return False
        # A new file beside the running hook's script, which stays as it is.
        target = scripts_dir() / f"{HOOK_ID}.{sha[:12]}.sh"
        if target.is_file() and target.read_text(encoding="utf-8", errors="replace") == shipped:
            print(f"unchanged: {target}")
        else:
            _write(target, shipped, 0o755)
            print(f"installed: {target} (the running hook's {current} was left as it is)")
            _record_script(sha)
        script_path = _script_path(target)
    want = render_definition(mxid, reply_command, script_path)
    if definition_matches(registered, json.loads(want)):
        print(f"unchanged: hook {HOOK_ID} ({current} and {def_path}; install did not touch them)")
        return True
    staged = _stage_definition(want)
    print(f"REQUIRED hook: {staged} (update hook {HOOK_ID} to it through your hooks tool)")
    print(want, end="")
    return True


def render_config(mxid: str, task_dir: Path, result_dir: Path, activity_file: Path,
                  python: str, state_dir: Path, gateway_instance: str = "",
                  reply_command: str = "", cron_dir: str = "", keepalive_log: str = "") -> str:
    rows = (("AGENT_MXID", mxid), ("TASK_DIR", str(task_dir)), ("RESULT_DIR", str(result_dir)),
            ("ACTIVITY_FILE", str(activity_file)), ("SPARROW_PYTHON", python),
            ("STATE_DIR", str(state_dir)), ("GATEWAY_INSTANCE", gateway_instance),
            ("REPLY_COMMAND", reply_command), ("CRON_DIR", cron_dir),
            ("KEEPALIVE_LOG", keepalive_log))
    return "".join(f"{k}={shlex.quote(v)}\n" for k, v in rows)


def read_config(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = [raw]
        out[key.strip()] = parts[0] if parts else ""
    return out


def _script_dirs() -> list:
    """Where pip put this interpreter's console scripts (venv, system, then --user)."""
    dirs = []
    for scheme in (None, f"{os.name}_user", "osx_framework_user"):
        try:
            d = sysconfig.get_path("scripts", *(() if scheme is None else (scheme,)))
        except KeyError:
            continue
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def _runnable(path: "str | Path") -> bool:
    return Path(path).is_file() and os.access(path, os.X_OK)


def _find_console_script(name: str) -> str:
    """Absolute path of console script `name` beside this interpreter, else on PATH; '' if none."""
    for d in _script_dirs():
        if _runnable(Path(d) / name):
            return str(Path(d) / name)
    found = shutil.which(name)
    return os.path.abspath(found) if found else ""


def find_reply_command() -> str:
    return _find_console_script(REPLY_COMMAND)


def reply_command_problem(path: str) -> str:
    """Why a worker could not run `path` as the reply command; empty when it can."""
    if not path:
        return (f"{REPLY_COMMAND} was not found beside {sys.executable} or on PATH, so a woken "
                "worker could not post a reply; reinstall ag2-sparrow[hatch] for this Python")
    if not _runnable(path):
        return f"{path} is not an executable file"
    if path != shlex.quote(path):
        return f"{path} needs shell quoting; install ag2-sparrow under a path without spaces"
    return ""


def service_path() -> Path:
    return _home() / ".config" / "systemd" / "user" / SERVICE_NAME


def device_env_path() -> Path:
    """The .env file holding REMOTE_TASK_TOKEN that Sparrow reads at startup."""
    env = os.environ.get("AG2_DEVICE_ENV")
    return Path(env).expanduser() if env else _home() / ".config" / "ag2-sparrow" / "relay.env"


def _systemd_run(cmd: list) -> "subprocess.CompletedProcess | None":
    """Run a systemctl/loginctl command; None when it could not run at all.
    Never prompts: over SSH a polkit password prompt would hang until the timeout."""
    try:
        return subprocess.run([cmd[0], "--no-ask-password", *cmd[1:]], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=SYSTEMD_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None


def _ok(r) -> bool:
    return r is not None and r.returncode == 0


def _why(r) -> str:
    if r is None:
        return "could not run"
    lines = (r.stderr or r.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit {r.returncode}"


def systemd_user_available() -> bool:
    """Whether `systemctl --user` reaches a running user manager."""
    return shutil.which("systemctl") is not None and \
        _ok(_systemd_run(["systemctl", "--user", "show-environment"]))


def render_service(exec_start: str, env: dict) -> str:
    rows = "".join(f'Environment="{k}={v}"\n' if " " in v else f"Environment={k}={v}\n"
                   for k, v in env.items())
    return (f"{SERVICE_MARKER}\n[Unit]\nDescription=ag2-sparrow (AG2 Space listener)\n"
            "# A Sparrow that dies 5 times in 5 minutes (e.g. a rejected token) is left failed.\n"
            "StartLimitIntervalSec=300\nStartLimitBurst=5\n\n"
            f"[Service]\n{rows}ExecStart={exec_start}\nRestart=always\nRestartSec=10\n"
            "# 75: another Sparrow already polls this workspace; do not relaunch over it.\n"
            "RestartPreventExitStatus=75\n\n[Install]\nWantedBy=default.target\n")


# Characters an unquoted Environment=/ExecStart= value cannot carry as themselves.
_UNIT_UNSAFE = re.compile(r"[\s\"'\\$;]|[\x00-\x1f\x7f]")


def _unit_value(value: str) -> str:
    """`value` as an unquoted unit value ('%' escaped); ValueError names what cannot be."""
    bad = _UNIT_UNSAFE.search(value)
    if bad:
        raise ValueError(f"{value!r} contains {bad.group()!r} (whitespace, a quote, a "
                         "backslash, $ or ;), which a systemd unit cannot carry unquoted; "
                         "use a path without it")
    return value.replace("%", "%%")


def _unit_props(*names: str) -> dict:
    r = _systemd_run(["systemctl", "--user", "show", SERVICE_NAME,
                      *(f"--property={n}" for n in names)])
    if not _ok(r):
        return {}
    return dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)


def _lock_record(state_dir: Path, instance: str) -> "dict | None":
    """This host's Sparrow single-instance lock record, or None when there is none to read."""
    lock = state_dir.parent / "state" / "locks" / \
        f"gateway-bridge{gateway_instance.suffix(instance)}.lock"
    try:
        holder = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = isinstance(holder, dict) and holder.get("host") == workspace_lock._host_label()
    return holder if ok else None


def _proc_stat(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return b""


def process_gone(pid: int) -> bool:
    """Whether `pid` has exited, a zombie included; False when that cannot be told."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    stat = _proc_stat(pid)
    return stat[stat.rfind(b")") + 1:].split()[:1] == [b"Z"]


def live_lock_holder(holder: "dict | None") -> "int | None":
    """The pid of a live Sparrow holding `holder` with a fresh heartbeat, else None."""
    pid = (holder or {}).get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if process_gone(pid):
        return None  # its holder crashed; the fresh status file is a corpse
    return pid if workspace_lock._is_fresh(holder, workspace_lock.DEFAULT_STALE_SECONDS) else None


def _outside_sparrow(state_dir: Path, instance: str, main_pid: int, ran: bool) -> bool:
    """Whether a Sparrow this service does not own polls the workspace now."""
    holder = _lock_record(state_dir, instance)
    if holder is not None:
        pid = live_lock_holder(holder)
        return pid is not None and pid != main_pid
    # No lock to read: a fresh status file is another Sparrow's only if this unit never ran.
    code, _ = check_sparrow(state_dir, instance=instance)
    return code != SPARROW_DOWN and not ran


def keepawake_note(reply_command: str, keepalive_on: bool = True) -> str:
    """Who keeps Sparrow running when no user service does."""
    if not keepalive_on:
        return "with no keepalive task, nothing restarts Sparrow after a crash or reboot"
    cmd = shlex.join([reply_command or REPLY_COMMAND, "keepalive", "keepawake"])
    return f"the agent keeps Sparrow running with {cmd} from its scheduler"


def install_service(task_dir: Path, result_dir: Path, state_dir: Path, instance: str,
                    force: bool, health_command: str = "", reply_command: str = "") -> None:
    """Write, enable and start the ag2-sparrow user service. Never fails the install:
    each problem prints a NOTE saying what was not done and how to finish by hand."""
    def note(text: str) -> None:
        print(f"NOTE service: {text}")

    if not systemd_user_available():
        note("systemctl --user is not available here, so no service was set up; "
             + keepawake_note(reply_command, bool(health_command)))
        return
    exec_start = _find_console_script(SPARROW_COMMAND)
    if not exec_start:
        note(f"{SPARROW_COMMAND} was not found beside {sys.executable} or on PATH; no service "
             "was set up. Reinstall ag2-sparrow for this Python and run install again")
        return
    device_env = device_env_path()
    try:
        st = device_env.stat()
    except OSError:
        st = None
    if st is not None and st.st_mode & 0o077:
        note(f"{device_env} holds your relay token but is mode {oct(st.st_mode & 0o777)[2:]}; "
             f"run: chmod 600 {shlex.quote(str(device_env))}")
    if st is not None and hasattr(os, "getuid") and st.st_uid != os.getuid():
        note(f"{device_env} is not owned by you (uid {st.st_uid}); another user can change "
             "the token Sparrow uses")
    env = {"AG2_DEVICE_ENV": str(device_env), "AGENT_CONNECT_TASK_DIR": str(task_dir),
           "AGENT_CONNECT_RESULT_DIR": str(result_dir), "AGENT_CONNECT_STATE_DIR": str(state_dir)}
    if instance:
        env["GATEWAY_INSTANCE"] = instance
    try:
        exec_start = _unit_value(exec_start)
        env = {k: _unit_value(v) for k, v in env.items()}
        if health_command:
            env["REMOTE_HEALTH_COMMAND"] = " ".join(map(_unit_value, shlex.split(health_command)))
    except ValueError as e:
        note(f"{e}; no service was set up")
        return
    unit, text = service_path(), render_service(exec_start, env)
    changed = True
    if unit.exists():
        old = unit.read_text(encoding="utf-8", errors="replace")
        if old == text:
            changed = False
            print(f"unchanged: {unit}")
        elif not old.startswith(SERVICE_MARKER) and not force:
            print(f"DIFF: {unit} differs from the ag2-sparrow unit:")
            print(_diff(old, text, unit), end="")
            note(f"{unit} was not written by install, so it was kept; re-run with --force to "
                 "replace it with the unit above")
            return
    stopped = False
    if changed and _unit_props("ActiveState").get("ActiveState") in ("active", "reloading"):
        r = _systemd_run(["systemctl", "--user", "stop", SERVICE_NAME])
        stopped = _ok(r)
        print(f"stopped: {SERVICE_NAME} before installing" if stopped else
              f"NOTE service: stop failed ({_why(r)}); it is restarted after the install instead")
    if changed:
        _write(unit, text, 0o644)
        print(f"installed: {unit}")
        r = _systemd_run(["systemctl", "--user", "daemon-reload"])
        if not _ok(r):
            note(f"systemctl --user daemon-reload failed ({_why(r)}); run it, then "
                 f"systemctl --user enable --now {SERVICE_NAME}")
            return
    props = _unit_props("ActiveState", "MainPID", "ExecMainStartTimestampMonotonic")
    state = props.get("ActiveState", "inactive")
    active = state in ("active", "reloading")
    try:
        main_pid = int(props.get("MainPID") or 0)
    except ValueError:
        main_pid = 0
    ran = props.get("ExecMainStartTimestampMonotonic", "0") not in ("", "0")
    # failed/activating/deactivating: the unit's own Sparrow crashed or is restarting.
    outside = not stopped and state == "inactive" and _outside_sparrow(state_dir, instance, main_pid, ran)
    has_token = device_env.is_file()
    start = has_token and not outside
    if start and state == "failed":
        _systemd_run(["systemctl", "--user", "reset-failed", SERVICE_NAME])
    r = _systemd_run(["systemctl", "--user", "enable", *(["--now"] if start else []),
                      SERVICE_NAME])
    if not _ok(r):
        note(f"systemctl --user enable {SERVICE_NAME} failed ({_why(r)}); fix that and run "
             f"systemctl --user enable --now {SERVICE_NAME}")
        return
    if active and changed:
        r = _systemd_run(["systemctl", "--user", "restart", SERVICE_NAME])
        print(f"restarted: {SERVICE_NAME} with the new unit" if _ok(r) else
              f"NOTE service: restart failed ({_why(r)}); run systemctl --user restart "
              f"{SERVICE_NAME}")
    print(f"enabled: {SERVICE_NAME}" + (" (running)" if start or active else " (not started)"))
    if outside:
        note("Sparrow is already running outside systemd, so the service was enabled but not "
             "started yet; install replaces that Sparrow with the service below")
    elif not has_token:
        note(f"{device_env} is missing, so the service was not started. Put your relay token "
             f"in it as one line REMOTE_TASK_TOKEN=<token>, chmod 600 it, then run: "
             f"systemctl --user start {SERVICE_NAME}")
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("linger: skipped (running as root)")
        return
    r = _systemd_run(["loginctl", "show-user", user, "--property=Linger", "--value"]) \
        if user else None
    if _ok(r) and r.stdout.strip() == "yes":
        print(f"linger: already on for {user}")
        return
    r = _systemd_run(["loginctl", "enable-linger", user]) if user else None
    if _ok(r):
        print(f"linger: enabled for {user}, so Sparrow runs while you are logged out")
    else:
        note(f"loginctl enable-linger {user or '$USER'} failed ({_why(r)}), so Sparrow stops "
             "when you log out and starts again at your next login. Ask an administrator to "
             f"run: loginctl enable-linger {user or '$USER'}")


def service_status(reply_command: str = "") -> "tuple[bool | None, str]":
    """(True, detail) installed and enabled; (False, detail) installed but not enabled;
    (None, detail) when there is nothing to judge (no systemd, or no unit)."""
    unit = service_path()
    if not systemd_user_available():
        return None, f"systemctl --user is not available; {keepawake_note(reply_command)}"
    if not unit.is_file():
        return None, (f"{unit} not installed; {keepawake_note(reply_command)}. Run "
                      "ag2-sparrow-hatch install to set the service up")
    enabled = _systemd_run(["systemctl", "--user", "is-enabled", SERVICE_NAME])
    if not _ok(enabled):
        return False, (f"{unit} installed but not enabled; run systemctl --user enable --now "
                       f"{SERVICE_NAME}")
    active = _ok(_systemd_run(["systemctl", "--user", "is-active", "--quiet", SERVICE_NAME]))
    return True, f"{unit} installed and enabled ({'active' if active else 'not active'})"


def install(args) -> int:
    from . import keepalive
    reply_command = find_reply_command()
    problem = reply_command_problem(reply_command)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 1
    if not valid_mxid(args.mxid):
        print(f"error: {args.mxid!r} is not an MXID like @my-agent:ag2.space", file=sys.stderr)
        return 2
    instance = (os.environ.get("GATEWAY_INSTANCE") if args.gateway_instance is None
                else args.gateway_instance)
    instance = (instance or "").strip()
    if instance and not INSTANCE_RE.fullmatch(instance):
        print(f"error: GATEWAY_INSTANCE {instance!r} must match {INSTANCE_RE.pattern}",
              file=sys.stderr)
        return 2
    task_dir = Path(args.task_dir) if args.task_dir else _dirs.task_dir()
    result_dir = Path(args.result_dir) if args.result_dir else _dirs.result_dir()
    state_dir = Path(args.state_dir) if args.state_dir else _dirs.state_dir()
    activity = Path(args.activity_file) if args.activity_file else _home() / ".muse" / "last_active"
    if not install_hook(args.mxid, reply_command, args.force):
        return 1
    # Empty = resolved under the home directory when the keepalive runs.
    cron = str(Path(args.cron_dir).expanduser().resolve()) if args.cron_dir else ""
    keepalive_log = (str(Path(args.keepalive_log).expanduser().resolve())
                     if args.keepalive_log else "")
    config = render_config(args.mxid, task_dir.resolve(), result_dir.resolve(),
                           activity, sys.executable, state_dir.resolve(), instance,
                           reply_command, cron, keepalive_log)
    _install_file(config_path(), config, 0o600, refuse_drift=False, force=True)
    activity.parent.mkdir(parents=True, exist_ok=True)
    if args.no_service:
        print("skipped: the ag2-sparrow user service (--no-service); "
              + keepawake_note(reply_command, not args.no_keepalive))
    else:
        install_service(task_dir.resolve(), result_dir.resolve(), state_dir.resolve(), instance,
                        args.force,
                        "" if args.no_keepalive else keepalive.health_command(reply_command),
                        reply_command)
    if args.no_keepalive:
        print("skipped: the keepalive task (--no-keepalive)")
    else:
        keepalive.install_tasks(read_config(config_path()), args.force)
    if args.no_start:
        rc = 0
        print("skipped: starting and checking Sparrow (--no-start)")
    else:
        rc = keepalive.ensure_sparrow(read_config(config_path()), not args.no_keepalive)
    return rc


def _check(results: list, ok: bool, name: str, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")


def restart_hint() -> str:
    """keepawake restarts Sparrow with REMOTE_HEALTH_COMMAND; a bare ag2-sparrow lacks it."""
    command = _installed_config().get("REPLY_COMMAND") or find_reply_command() or REPLY_COMMAND
    return f"restart it: run {shlex.join([command, 'keepalive', 'keepawake'])}"


def sparrow_status_path(state_dir: Path, instance: str | None = None) -> Path:
    """`instance` None reads GATEWAY_INSTANCE from the environment, as Sparrow does."""
    instance = (os.environ.get("GATEWAY_INSTANCE") if instance is None else instance) or ""
    instance = instance.strip()
    return state_dir / f"gateway-status{'.' + instance if instance else ''}.json"


def check_sparrow(state_dir: Path, now: float | None = None,
                  instance: str | None = None) -> tuple[int, str]:
    """SPARROW_OK, SPARROW_DOWN or SPARROW_NOT_CONNECTED, with a detail, judged by the
    status file Sparrow rewrites each poll."""
    path = sparrow_status_path(state_dir, instance)
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        ts = float(status["ts"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        return SPARROW_DOWN, (f"{path} unreadable ({type(e).__name__}); Sparrow has not run with "
                       f"this state dir; {restart_hint()}")
    age = int((time.time() if now is None else now) - ts)
    if age > SPARROW_STATUS_MAX_AGE_S:
        return SPARROW_DOWN, (f"{path} last written {age}s ago; Sparrow is not running, so no task "
                       f"reaches the hook; {restart_hint()}")
    if not status.get("connected"):
        return SPARROW_NOT_CONNECTED, (
            f"Sparrow is running but not connected: {status.get('error') or 'no error given'}; "
            "if the token was rejected, a restart does not help: put a valid relay token from "
            "the AG2 Space Agent Portal in REMOTE_TASK_TOKEN (the .env file AG2_DEVICE_ENV "
            "names), then restart Sparrow")
    return SPARROW_OK, f"running and connected ({path} written {max(age, 0)}s ago)"


def sparrow(args) -> int:
    code, detail = check_sparrow(Path(args.state_dir) if args.state_dir else _dirs.state_dir(),
                                 instance=args.gateway_instance)
    print(detail)
    return code


def verify(_args) -> int:
    results: list = []
    registered = registered_definition() or {}
    script = Path(os.path.expanduser(str(registered.get("script_path") or
                                         _script_path(scripts_dir() / SCRIPT_NAME))))
    staged = staged_definition_path()
    pending = (f"; update the hook through your hooks tool to the definition in {staged}, as "
               "install's REQUIRED hook: line says" if staged.is_file() else "")
    if not script.is_file():
        _check(results, False, "hook script", f"{script} missing; run ag2-sparrow-hatch install")
    elif not os.access(script, os.X_OK):
        _check(results, False, "hook script", f"{script} is not executable")
    elif script.read_text(encoding="utf-8", errors="replace") != _shipped(SCRIPT_NAME):
        _check(results, False, "hook script",
               f"{script} differs from this ag2-sparrow's copy{pending}")
    else:
        _check(results, True, "hook script", str(script))

    def_path = definition_path()
    definition: dict = {}
    try:
        definition = json.loads(def_path.read_text(encoding="utf-8"))
        bad = any(p in json.dumps(definition) for p in ("<AGENT_MXID>", REPLY_PLACEHOLDER)) or \
            not re.fullmatch(rf"{re.escape(HOOK_ID)}(\.[0-9a-f]{{12}})?\.sh", script.name) or \
            script.parent != scripts_dir()
        detail = (f"{def_path} has a placeholder or a script_path that is not an installed "
                  "hook script" if bad else str(def_path))
        missing = [k for k in HOOK_REQUIRED_KEYS if k not in definition]
        if missing:
            bad, detail = True, (f"{def_path} lacks {', '.join(missing)}: Hatch marks this hook "
                                 "invalid and never runs it; re-add it through your hooks tool "
                                 "with every field of the definition install prints")
        if not bad and staged.is_file():
            want = json.loads(staged.read_text(encoding="utf-8"))
            if not definition_matches(definition, want):
                bad, detail = True, f"{def_path} is not the definition install staged{pending}"
        _check(results, not bad, "hook definition", detail)
    except FileNotFoundError:
        _check(results, False, "hook definition", f"{def_path} missing: the hook is not "
               "registered; add it through your hooks tool as install's REQUIRED hook: line says")
    except (OSError, ValueError, AttributeError) as e:
        _check(results, False, "hook definition", f"{def_path}: {e}")

    cfg_path = config_path()
    cfg: dict = {}
    try:
        cfg = read_config(cfg_path)
        problems = []
        if not valid_mxid(cfg.get("AGENT_MXID", "")):
            problems.append("AGENT_MXID is not an MXID")
        for key in ("TASK_DIR", "RESULT_DIR", "STATE_DIR", "ACTIVITY_FILE", "SPARROW_PYTHON"):
            if not cfg.get(key):
                problems.append(f"{key} missing")
        if cfg.get("SPARROW_PYTHON"):
            probe = subprocess.run([cfg["SPARROW_PYTHON"], "-c",
                                    "import ag2_sparrow.adapters.hatch.task_fields"],
                                   capture_output=True, text=True, timeout=30)
            if probe.returncode != 0:
                problems.append(f"{cfg['SPARROW_PYTHON']} cannot import ag2_sparrow")
        _check(results, not problems, "config",
               "; ".join(problems) if problems else f"{cfg_path} ({cfg['AGENT_MXID']})")
    except (OSError, subprocess.SubprocessError) as e:
        _check(results, False, "config", f"{cfg_path}: {e}")

    task_dir = Path(cfg.get("TASK_DIR") or _dirs.task_dir())
    readable = task_dir.is_dir() and os.access(task_dir, os.R_OK | os.X_OK)
    _check(results, readable, "task_dir",
           str(task_dir) if readable else f"{task_dir} is not a readable directory; start "
           "ag2-sparrow with the same AGENT_CONNECT_TASK_DIR")

    state_dir = Path(cfg.get("STATE_DIR") or _dirs.state_dir())
    code, detail = check_sparrow(state_dir, instance=cfg.get("GATEWAY_INSTANCE"))
    _check(results, code == SPARROW_OK, "sparrow", detail)

    run = hook_last_run(state_dir)
    if run is None:
        ok, detail = False, (f"hook not running: it has never run ({hook_run_path(state_dir)} "
                             "missing); register and enable it through your hooks tool as "
                             "install's REQUIRED hook: line says")
    elif not 0 <= time.time() - run[0] <= HOOK_VERIFY_MAX_AGE_S:
        ok, detail = False, (f"hook not running: last run "
                             f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(run[0]))} "
                             f"({int(time.time() - run[0])}s ago); check that your hooks tool "
                             "lists it enabled, and its hooks.logs")
    elif run[1] != _sha256(_shipped(SCRIPT_NAME)):
        ok, detail = False, f"the hook runs an older script{pending or '; run install again'}"
    else:
        ok, detail = True, f"last run {int(time.time() - run[0])}s ago, this ag2-sparrow's script"
    _check(results, ok, "hook run", detail)

    ok, detail = service_status(cfg.get("REPLY_COMMAND", ""))
    if ok is None:
        print(f"NOTE service: {detail}")
    else:
        _check(results, ok, "service", detail)

    reply_command = cfg.get("REPLY_COMMAND", "")
    problem = reply_command_problem(reply_command) if reply_command else \
        "REPLY_COMMAND missing from the config; run ag2-sparrow-hatch install again"
    if not problem and reply_command not in definition.get("prompt", ""):
        problem = f"the hook prompt does not name {reply_command}; run install again"
    _check(results, not problem, "reply command",
           problem or f"{reply_command} (by absolute path, so the worker's PATH does not matter)")

    missing = [tool for tool in ("jq", "flock", "timeout") if shutil.which(tool) is None]
    _check(results, not missing, "tools",
           "jq, flock, timeout on PATH" if not missing else f"missing on PATH: {', '.join(missing)}")

    activity = Path(cfg.get("ACTIVITY_FILE") or _home() / ".muse" / "last_active")
    interval = definition.get("poll_interval_secs")
    problems = []
    if not isinstance(interval, int) or not 0 < interval < HEARTBEAT_INTERVAL_S:
        problems.append(f"poll_interval_secs {interval!r} must be under {HEARTBEAT_INTERVAL_S}")
    if not (activity.parent.is_dir() and os.access(activity.parent, os.W_OK)):
        problems.append(f"{activity.parent} (activity marker dir) is not writable")
    _check(results, not problems, "heartbeat",
           "; ".join(problems) if problems else
           f"armed: a wake after {HEARTBEAT_INTERVAL_S}s quiet unless {activity} is fresher")

    from . import keepalive
    line, row = keepalive.status_line(cfg)
    never = keepalive.last_keepawake(keepalive.log_path(cfg)) is None
    task, step = keepalive.TASK, keepalive.register_step(cfg)
    if row is None:
        print(f"NOTE keepalive: {line} (no {task} task is installed)")
    elif row["condition"] == "abnormal" and row["reason"] != keepalive.NOT_RUNNING:
        _check(results, False, "keepalive", f"{line}; {keepalive.REASON_HINTS[row['reason']]}")
    elif row["condition"] == "abnormal":
        _check(results, False, "keepalive", f"{line}; " + (
            f"{task} has never run: {step}" if never else
            f"{task} has not run in the last {keepalive.STALE_S // 60} minutes; check that "
            f"your scheduler still lists it, else {step}"))
    elif row["condition"] == "unknown":
        print(f"NOTE keepalive: {line} ({task} has not run yet; unless you already did, {step})")
    else:
        print(f"NOTE keepalive: {line}")
    if row is not None and keepalive.listener_up(cfg, time.time()):
        pid, problem = keepalive.unmanaged_health_problem(cfg)
        if problem == keepalive.NO_HEALTH_COMMAND:
            _check(results, False, "sparrow health", f"sparrow runs without the health command "
                   f"(pid {pid}): its heartbeats carry no health row; run install")
        elif problem == keepalive.UNKNOWN_ENV:
            print("NOTE sparrow health: the running Sparrow's environment cannot be read, so "
                  "whether its heartbeats carry the health row is unknown")

    mxid = cfg.get("AGENT_MXID") or "@your-agent:ag2.space"
    print(f"\nManual test: from another account, mention {mxid} in an AG2 Space room. Within a "
          "few polls a task file appears in the task_dir, the hook wakes the worker, and the "
          "reply lands in that message's thread tagging you. hooks.logs shows the wake.")
    return 0 if all(results) else 1


def uninstall(args) -> int:
    """Remove the ag2-sparrow user service and/or the keepalive tasks install wrote."""
    if not (args.service or args.keepalive):
        print("error: name what to remove: uninstall --service and/or --keepalive",
              file=sys.stderr)
        return 2
    rc = 0
    if args.keepalive:
        from . import keepalive
        rc = keepalive.uninstall_tasks(_installed_config())
    return max(rc, uninstall_service(not args.keepalive)) if args.service else rc


def uninstall_service(keepalive_on: bool = True) -> int:
    """Stop, disable and remove the ag2-sparrow user service install wrote."""
    unit = service_path()
    if not unit.is_file():
        print(f"nothing to remove: {unit} is not installed")
        return 0
    if not unit.read_text(encoding="utf-8", errors="replace").startswith(SERVICE_MARKER):
        print(f"error: {unit} was not written by install; remove it yourself", file=sys.stderr)
        return 1
    if systemd_user_available():
        r = _systemd_run(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
        if not _ok(r):
            print(f"error: systemctl --user disable --now {SERVICE_NAME} failed ({_why(r)}); "
                  f"{unit} was kept", file=sys.stderr)
            return 1
        print(f"stopped and disabled: {SERVICE_NAME}")
    unit.unlink()
    print(f"removed: {unit}")
    if systemd_user_available():
        _systemd_run(["systemctl", "--user", "daemon-reload"])
    print("Linger was left as it is (other user services may need it); "
          + keepawake_note(_installed_config().get("REPLY_COMMAND", ""), keepalive_on) + ".")
    return 0


def _installed_config() -> dict:
    try:
        return read_config(config_path())
    except OSError:
        return {}


def _read_body(args) -> "tuple[str | None, str]":
    """The reply text and, when it cannot be sent as a plain reply, why."""
    if args.no_send:
        return "[no-send]\n", ""
    body = args.body if args.body is not None else sys.stdin.read(REPLY_MAX_BYTES + 1)
    if not body.strip():
        return None, "empty reply; pass the text on stdin, or use --no-send"
    if len(body.encode("utf-8")) > REPLY_MAX_BYTES:
        return None, f"reply over {REPLY_MAX_BYTES} bytes"
    # Parsed exactly as the bridge's delivery path parses it (skip_after_channel=True).
    actions = parse_markers(body, skip_after_channel=True).actions
    if actions:
        kinds = ", ".join(sorted({a.kind for a in actions}))
        return None, (f"the reply carries a delivery marker ({kinds}) such as [REPLIED], "
                      "[channel:], [thread] or [file:]; a reply is plain text to the asker's "
                      "message only. Use --no-send to post nothing")
    return body, ""


def _claims_dir(result_dir: Path) -> Path:
    return result_dir / ".hatch-reply-claims"


def _prune_claims(result_dir: Path, task_dir: Path) -> None:
    """Drop the claims of tasks Sparrow has archived; reply refuses those tasks anyway."""
    try:
        claims = list(_claims_dir(result_dir).iterdir())
    except OSError:
        return
    for claim in claims:
        if not (task_dir / f"{claim.name}.txt").exists():
            claim.unlink(missing_ok=True)


def _claim(result_dir: Path, tid: str) -> bool:
    """One reply per task while it is open: the first caller's O_EXCL claim wins."""
    claims = _claims_dir(result_dir)
    claims.mkdir(mode=0o700, exist_ok=True)
    try:
        os.close(os.open(claims / tid, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    return True


def _publish_once(result_file: Path, body: str) -> bool:
    """Write `body` at `result_file` unless something already holds that name."""
    fd, tmp = tempfile.mkstemp(dir=str(result_file.parent), prefix=f".{result_file.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(tmp, 0o600)
        os.link(tmp, result_file)
        return True
    except FileExistsError:
        return False
    finally:
        Path(tmp).unlink(missing_ok=True)


def reply(args) -> int:
    cfg = _installed_config()
    result_dir = Path(cfg.get("RESULT_DIR") or _dirs.result_dir())
    task_dir = Path(cfg.get("TASK_DIR") or _dirs.task_dir())
    result_file = Path(args.result_file).expanduser()
    tid = result_file.stem
    # The instance the bridge runs as, read as install recorded it (else as the bridge reads it).
    instance = (cfg["GATEWAY_INSTANCE"] if "GATEWAY_INSTANCE" in cfg
                else os.environ.get("GATEWAY_INSTANCE") or "").strip()
    item = item_id(tid, instance)
    if not (result_file.suffix == ".txt" and item is not None
            and result_file.parent.resolve() == result_dir.resolve()):
        print(f"error: {result_file} is not a result_file from the wake payload "
              f"(task-<id>.txt in {result_dir})", file=sys.stderr)
        return 2
    if not (task_dir / f"{tid}.txt").is_file():
        print(f"error: no open task {tid} in {task_dir}", file=sys.stderr)
        return 2
    if not (math.isfinite(args.wait) and 0 <= args.wait <= REPLY_WAIT_MAX_S):
        print(f"error: --wait must be 0 to {REPLY_WAIT_MAX_S:g} seconds", file=sys.stderr)
        return 2
    body, why = _read_body(args)
    if body is None:
        print(f"error: {why}", file=sys.stderr)
        return 2
    root = gateway_instance.outbox_root(result_dir, instance)
    _prune_claims(result_dir, task_dir)
    if (not _claim(result_dir, tid) or outbox.item_status(root, item) is not None
            or not _publish_once(result_file, body)):
        print(f"NOT posted: {result_file.name} already has a reply or result; nothing written",
              file=sys.stderr)
        return REPLY_NOT_POSTED
    if args.no_send:
        print(f"closed: {result_file.name}, nothing posted")
        return REPLY_POSTED
    deadline = time.monotonic() + args.wait
    while True:
        record = outbox.read_item(root, item) or {}
        status = record.get("status")
        if status == "DELIVERED":
            print(f"posted: {result_file.name} (accepted by the AG2 Space gateway)")
            return REPLY_POSTED
        if status == "PARKED":
            print(f"NOT posted: Sparrow gave up on {result_file.name}: "
                  f"{record.get('reason') or 'no reason given'}", file=sys.stderr)
            return REPLY_NOT_POSTED
        if not result_file.exists() and outbox.item_status(root, item) != "DELIVERED":
            print(f"NOT posted: Sparrow withheld {result_file.name} (held for review or "
                  f"quarantined in {result_dir / 'undelivered'})", file=sys.stderr)
            return REPLY_NOT_POSTED
        if time.monotonic() >= deadline:
            _, detail = check_sparrow(Path(cfg.get("STATE_DIR") or _dirs.state_dir()),
                                      instance=cfg.get("GATEWAY_INSTANCE"))
            print(f"NOT confirmed after {args.wait:g}s: {result_file.name} is queued and Sparrow "
                  f"will keep trying; do not report it as posted. Sparrow: {detail}",
                  file=sys.stderr)
            return REPLY_UNCONFIRMED
        time.sleep(0.25)


def _keepalive(args) -> int:
    from . import keepalive
    return keepalive.main(args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ag2-sparrow-hatch",
                                 description="Meta Hatch adapter for ag2-sparrow.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install", help="install the Hatch hook script, definition and config")
    p.add_argument("--mxid", required=True, help="the agent's AG2 Space MXID")
    p.add_argument("--task-dir", help="Sparrow's task dir (default: as ag2-sparrow resolves it)")
    p.add_argument("--result-dir", help="Sparrow's result dir (default: as ag2-sparrow resolves it)")
    p.add_argument("--state-dir", help="Sparrow's state dir (default: as ag2-sparrow resolves it)")
    p.add_argument("--activity-file", help="activity marker (default: ~/.muse/last_active)")
    p.add_argument("--gateway-instance", help="Sparrow's GATEWAY_INSTANCE (default: from the "
                   "environment, as ag2-sparrow reads it)")
    p.add_argument("--force", action="store_true", help="replace a locally modified hook script, "
                   "ag2-sparrow.service, or keepalive tasks install did not write")
    p.add_argument("--no-service", action="store_true", help="do not set up the ag2-sparrow "
                   "systemd user service; the agent's keepawake task still restarts Sparrow")
    p.add_argument("--no-keepalive", action="store_true", help="do not schedule the keepalive "
                   "task")
    p.add_argument("--no-start", action="store_true", help="do not start, replace or check "
                   "Sparrow now; the agent's keepawake task starts it on its next run")
    p.add_argument("--cron-dir", help="where Hatch reads scheduled tasks (default: "
                   "~/workspace/cron.d/minutely)")
    p.add_argument("--keepalive-log", help="the keepalive log (default: "
                   "~/keepalive-probe.log)")
    p.set_defaults(func=install)
    u = sub.add_parser("uninstall", help="remove what install set up")
    u.add_argument("--service", action="store_true", help="stop, disable and remove the "
                   "ag2-sparrow user service")
    u.add_argument("--keepalive", action="store_true", help="remove the keepalive tasks install "
                   "wrote (the keepalive log is kept)")
    u.set_defaults(func=uninstall)
    k = sub.add_parser("keepalive", help="what the keepalive task and Sparrow run")
    k.add_argument("action", choices=("keepawake", "health", "status", "report"),
                   help="keepawake: restart Sparrow if it is down and append a line to the log; "
                   "health: print Sparrow's health row (JSON); status: print the state, writing "
                   "nothing; report: list the log's gaps and restarts")
    k.add_argument("--log", help="the keepalive log (default: as install recorded it, else "
                   "~/keepalive-probe.log)")
    from . import keepalive
    k.add_argument("--gap-minutes", type=float, default=keepalive.GAP_MINUTES,
                   help=f"report: a gap longer than this is a stop (default "
                   f"{keepalive.GAP_MINUTES})")
    k.set_defaults(func=_keepalive)
    v = sub.add_parser("verify", help="check the installed hook")
    v.set_defaults(func=verify)
    s = sub.add_parser("sparrow", help=f"check that Sparrow is running (exit {SPARROW_DOWN}) "
                       f"and connected (exit {SPARROW_NOT_CONNECTED})")
    s.add_argument("--state-dir", help="Sparrow's state dir (default: as ag2-sparrow resolves it)")
    s.add_argument("--gateway-instance", help="Sparrow's GATEWAY_INSTANCE (default: from the "
                   "environment)")
    s.set_defaults(func=sparrow)
    r = sub.add_parser("reply", help=f"answer one wake item: write its result_file and wait until "
                       f"Sparrow posted it (exit {REPLY_POSTED} posted, {REPLY_NOT_POSTED} not "
                       f"posted, {REPLY_UNCONFIRMED} queued but not confirmed)")
    r.add_argument("result_file", help="the item's result_file from the wake payload")
    # Accepted and ignored, so a worker still on an older prompt is not refused.
    r.add_argument("--answering", help=argparse.SUPPRESS)
    r.add_argument("--body", help="the reply text (default: read from stdin)")
    r.add_argument("--no-send", action="store_true", help="close the item without posting")
    r.add_argument("--wait", type=float, default=REPLY_WAIT_S,
                   help=f"seconds to wait for Sparrow to confirm (default {REPLY_WAIT_S:g})")
    r.set_defaults(func=reply)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
