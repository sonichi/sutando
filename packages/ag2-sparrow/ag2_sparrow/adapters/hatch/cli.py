"""`ag2-sparrow-hatch install|uninstall|verify|sparrow|reply`: set up, check and answer through the Hatch hook.

install writes the hook script to ~/hooks/scripts/, the hook definition to
~/hooks/definitions/ and the config the script reads, and where systemd runs a user
manager, the ag2-sparrow user service that restarts Sparrow; verify checks that setup;
sparrow checks only that Sparrow is running (the hook's heartbeat runs it); reply
writes a worker's answer to its result_file and waits until Sparrow has sent it."""
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
import subprocess
import sys
import sysconfig
import tempfile
import time
import unicodedata
from importlib import resources
from pathlib import Path

from ... import _dirs, gateway_instance, outbox, workspace_lock
from ...result_markers import parse_markers
from .task_fields import (item_id, list_open_tasks, message_body, message_text, snippet,
                          valid_mxid)

HOOK_ID = "ag2-sparrow-hatch"
SCRIPT_NAME = f"{HOOK_ID}.sh"
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
# --answering must quote this many consecutive words of the message (all of a shorter one);
# in scripts written without spaces each character is a token, and this many count as the words.
ANSWERING_MIN_WORDS, ANSWERING_MIN_CHARS = 3, 4
_MENTION = re.compile(r"@\S+")
_UNSPACED = ("\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u3040-\u30ff\u31f0-\u31ff"
             "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\U00020000-\U0003ffff")
_UNSPACED_CHAR = re.compile(f"[{_UNSPACED}]")
_TOKEN = re.compile(rf"[{_UNSPACED}]|(?:(?![{_UNSPACED}])\w)+")


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
    """sha256 of the hook script install last wrote, so an upgrade can tell its own
    earlier copy from a local edit."""
    return config_path().with_name("installed-script.sha256")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ours() -> frozenset:
    try:
        recorded = installed_marker_path().read_text(encoding="utf-8").strip()
    except OSError:
        recorded = ""
    return RELEASED_SCRIPT_SHA256 | ({recorded} if recorded else set())


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


def render_config(mxid: str, task_dir: Path, result_dir: Path, activity_file: Path,
                  python: str, state_dir: Path, gateway_instance: str = "",
                  reply_command: str = "") -> str:
    rows = (("AGENT_MXID", mxid), ("TASK_DIR", str(task_dir)), ("RESULT_DIR", str(result_dir)),
            ("ACTIVITY_FILE", str(activity_file)), ("SPARROW_PYTHON", python),
            ("STATE_DIR", str(state_dir)), ("GATEWAY_INSTANCE", gateway_instance),
            ("REPLY_COMMAND", reply_command))
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
    rows = "".join(f"Environment={k}={v}\n" for k, v in env.items())
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


def _outside_sparrow(state_dir: Path, instance: str, main_pid: int, ran: bool) -> bool:
    """Whether a Sparrow this service does not own polls the workspace now."""
    lock = state_dir.parent / "state" / "locks" / \
        f"gateway-bridge{gateway_instance.suffix(instance)}.lock"
    try:
        holder = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        holder = None
    if isinstance(holder, dict) and holder.get("host") == workspace_lock._host_label():
        pid = holder.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid == main_pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False  # its holder crashed; the fresh status file is a corpse
        except OSError:
            pass
        return workspace_lock._is_fresh(holder, workspace_lock.DEFAULT_STALE_SECONDS)
    # No lock to read: a fresh status file is another Sparrow's only if this unit never ran.
    code, _ = check_sparrow(state_dir, instance=instance)
    return code != SPARROW_DOWN and not ran


def install_service(task_dir: Path, result_dir: Path, state_dir: Path, instance: str,
                    force: bool) -> None:
    """Write, enable and start the ag2-sparrow user service. Never fails the install:
    each problem prints a NOTE saying what was not done and how to finish by hand."""
    def note(text: str) -> None:
        print(f"NOTE service: {text}")

    if not systemd_user_available():
        note("systemctl --user is not available here, so no service was set up and nothing "
             "restarts Sparrow after a crash or reboot; keep ag2-sparrow running another way")
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
    outside = state == "inactive" and _outside_sparrow(state_dir, instance, main_pid, ran)
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
             "started, and the running Sparrow was left alone. To switch over, stop that "
             f"ag2-sparrow process, then run: systemctl --user start {SERVICE_NAME}")
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


def service_status() -> "tuple[bool | None, str]":
    """(True, detail) installed and enabled; (False, detail) installed but not enabled;
    (None, detail) when there is nothing to judge (no systemd, or no unit)."""
    unit = service_path()
    if not systemd_user_available():
        return None, "systemctl --user is not available; nothing restarts Sparrow by itself"
    if not unit.is_file():
        return None, (f"{unit} not installed; nothing restarts Sparrow after a crash or reboot. "
                      "Run ag2-sparrow-hatch install to set it up")
    enabled = _systemd_run(["systemctl", "--user", "is-enabled", SERVICE_NAME])
    if not _ok(enabled):
        return False, (f"{unit} installed but not enabled; run systemctl --user enable --now "
                       f"{SERVICE_NAME}")
    active = _ok(_systemd_run(["systemctl", "--user", "is-active", "--quiet", SERVICE_NAME]))
    return True, f"{unit} installed and enabled ({'active' if active else 'not active'})"


def install(args) -> int:
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
    script = scripts_dir() / SCRIPT_NAME
    shipped = _shipped(SCRIPT_NAME)
    if not _install_file(script, shipped, 0o755, refuse_drift=True, force=args.force,
                         ours=_ours()):
        return 1
    _write(installed_marker_path(), _sha256(shipped) + "\n", 0o644)
    definition = (_shipped("hook-definition.json").replace("<AGENT_MXID>", args.mxid)
                  .replace(REPLY_PLACEHOLDER, reply_command))
    _install_file(definitions_dir() / f"{HOOK_ID}.json", definition, 0o644,
                  refuse_drift=False, force=True)
    config = render_config(args.mxid, task_dir.resolve(), result_dir.resolve(),
                           activity, sys.executable, state_dir.resolve(), instance,
                           reply_command)
    _install_file(config_path(), config, 0o600, refuse_drift=False, force=True)
    activity.parent.mkdir(parents=True, exist_ok=True)
    if args.no_service:
        print("skipped: the ag2-sparrow user service (--no-service); nothing restarts Sparrow "
              "after a crash or reboot unless you keep it running another way")
    else:
        install_service(task_dir.resolve(), result_dir.resolve(), state_dir.resolve(), instance,
                        args.force)
    print(f"\nNext: register the hook with hooks.add using {definitions_dir() / f'{HOOK_ID}.json'}, "
          "test it with hooks.dry_run, start it with hooks.enable, then run: ag2-sparrow-hatch verify")
    return 0


def _check(results: list, ok: bool, name: str, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")


RESTART_HINT = ("restart it: systemctl --user reset-failed ag2-sparrow; systemctl --user "
                "restart ag2-sparrow under the user service install sets up, else start "
                "ag2-sparrow again as at install")


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
                       f"this state dir; {RESTART_HINT}")
    age = int((time.time() if now is None else now) - ts)
    if age > SPARROW_STATUS_MAX_AGE_S:
        return SPARROW_DOWN, (f"{path} last written {age}s ago; Sparrow is not running, so no task "
                       f"reaches the hook; {RESTART_HINT}")
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
    script = scripts_dir() / SCRIPT_NAME
    if not script.is_file():
        _check(results, False, "hook script", f"{script} missing; run ag2-sparrow-hatch install")
    elif not os.access(script, os.X_OK):
        _check(results, False, "hook script", f"{script} is not executable")
    elif script.read_text(encoding="utf-8", errors="replace") != _shipped(SCRIPT_NAME):
        _check(results, False, "hook script", f"{script} differs from this ag2-sparrow's copy")
    else:
        _check(results, True, "hook script", str(script))

    definition_path = definitions_dir() / f"{HOOK_ID}.json"
    definition: dict = {}
    try:
        definition = json.loads(definition_path.read_text(encoding="utf-8"))
        bad = any(p in json.dumps(definition) for p in ("<AGENT_MXID>", REPLY_PLACEHOLDER)) or \
            Path(os.path.expanduser(definition.get("script_path", ""))) != script
        _check(results, not bad, "hook definition",
               str(definition_path) if not bad else f"{definition_path} has a placeholder or "
               "a script_path that is not the installed script")
    except (OSError, ValueError) as e:
        _check(results, False, "hook definition", f"{definition_path}: {e}")

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

    ok, detail = service_status()
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

    mxid = cfg.get("AGENT_MXID") or "@your-agent:ag2.space"
    print(f"\nManual test: from another account, mention {mxid} in an AG2 Space room. Within a "
          "few polls a task file appears in the task_dir, the hook wakes the worker, and the "
          "reply lands in that message's thread tagging you. hooks.logs shows the wake.")
    return 0 if all(results) else 1


def uninstall(args) -> int:
    """Stop, disable and remove the ag2-sparrow user service install wrote."""
    if not args.service:
        print("error: name what to remove: uninstall --service", file=sys.stderr)
        return 2
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
    print("Linger was left as it is (other user services may need it); nothing restarts Sparrow "
          "now, so keep ag2-sparrow running another way or run install again.")
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


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", _MENTION.sub(" ", text)).casefold()


def _words(text: str) -> list:
    return _TOKEN.findall(_normalized(text))


def _weight(tokens: list) -> int:
    """How much of a quote `tokens` is, in units where ANSWERING_MIN_WORDS words or
    ANSWERING_MIN_CHARS unspaced characters are each the minimum."""
    chars = sum(1 for t in tokens if _UNSPACED_CHAR.fullmatch(t))
    return (len(tokens) - chars) * ANSWERING_MIN_CHARS + chars * ANSWERING_MIN_WORDS


def answers(body: str, quoted: str) -> bool:
    """Whether `quoted` is ANSWERING_MIN_WORDS or more consecutive words of the message in
    `body` (ANSWERING_MIN_CHARS characters of text written without spaces, such as Chinese,
    Japanese or Thai; all of it when shorter); case, punctuation and @-mentions do not count.
    A message with no words (e.g. only emoji) is named only by all of it, punctuation included."""
    message = message_text(body)
    have, want = _words(message), _words(quoted)
    if not have:  # no words at all, e.g. a lone emoji: only the whole message names it
        squeeze = "".join(_normalized(quoted).split())
        return bool(squeeze) and squeeze == "".join(_normalized(message).split())
    if not want or _weight(want) < min(ANSWERING_MIN_WORDS * ANSWERING_MIN_CHARS,
                                       _weight(have)):
        return False
    return any(have[i:i + len(want)] == want for i in range(len(have) - len(want) + 1))


def _task_body(task_file: Path) -> str:
    try:
        return message_body(task_file.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def _misrouted(tid: str, task_dir: Path, result_dir: Path, instance: str, quoted: str) -> str:
    """Why `quoted` does not bind this reply to task `tid`, naming the items it does fit."""
    body = _task_body(task_dir / f"{tid}.txt")
    fits = [item["result_file"] for item in list_open_tasks(task_dir, result_dir, instance)
            if item["task_id"] != tid and answers(_task_body(Path(item["task_file"])), quoted)]
    where = (f" Those words are from the message of {', '.join(fits)}: answer it with that "
             "result_file." if fits else
             f" Copy {ANSWERING_MIN_WORDS} or more consecutive words (or {ANSWERING_MIN_CHARS} "
             "characters of text written without spaces) of the message you answer from its "
             "snippet.")
    return (f"--answering {quoted!r} is not the message of {tid} "
            f"({snippet(body)[:200]!r}); nothing written.{where}")


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
    if args.answering is None:
        print(f"error: name the message this answers: --answering '<{ANSWERING_MIN_WORDS} or "
              f"more consecutive words ({ANSWERING_MIN_CHARS} characters of text written "
              "without spaces) copied from its snippet>'; nothing written",
              file=sys.stderr)
        return 2
    if not answers(_task_body(task_dir / f"{tid}.txt"), args.answering):
        print(f"error: {_misrouted(tid, task_dir, result_dir, instance, args.answering)}",
              file=sys.stderr)
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
    p.add_argument("--force", action="store_true", help="replace a locally modified hook script "
                   "or ag2-sparrow.service")
    p.add_argument("--no-service", action="store_true", help="do not set up the ag2-sparrow "
                   "systemd user service that restarts Sparrow")
    p.set_defaults(func=install)
    u = sub.add_parser("uninstall", help="remove what install set up")
    u.add_argument("--service", action="store_true", help="stop, disable and remove the "
                   "ag2-sparrow user service")
    u.set_defaults(func=uninstall)
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
    r.add_argument("--answering", metavar="WORDS",
                   help=f"{ANSWERING_MIN_WORDS} or more consecutive words ({ANSWERING_MIN_CHARS} "
                   "characters of text written without spaces) of the message this "
                   "answers, copied from its snippet (required: a reply to another item's "
                   "message is refused)")
    r.add_argument("--body", help="the reply text (default: read from stdin)")
    r.add_argument("--no-send", action="store_true", help="close the item without posting")
    r.add_argument("--wait", type=float, default=REPLY_WAIT_S,
                   help=f"seconds to wait for Sparrow to confirm (default {REPLY_WAIT_S:g})")
    r.set_defaults(func=reply)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
