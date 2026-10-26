"""The supervised-session lifecycle every `ag2-sparrow-<shape>-local` command shares, as one owner.

The service manager (systemd --user, launchd) is the only supervisor; the work-root flock is occupancy
and the holder record attribution, never readiness. `install` is a quiescent replacement under one
per-shape operation lock: stop the job and prove it absent, prove the old root free, write the config
and the unit (tmp + replace), register and start, then require the root held by a fresh holder of this
shape; anything less is one line and the files as written, so an identical install is the retry.
`uninstall` stops, proves absent, proves the root free, removes the unit, reloads, and the config last.
An unknown probe authorises nothing. The launcher holds the lock for its own life only, spawns the
session as its direct child against `tasks` under the held root inode, signals only that child while
it is unreaped, and leaves the rest of the process tree to the manager (systemd's control group,
launchd's process group): nothing a descendant does can hold the root.

Package level, parametrised by a Shape the adapter passes in: this module names no adapter and imports
none, so each adapter owns its console script and the boundary rule holds. Stdlib only.
"""
from __future__ import annotations

import fcntl
import getpass
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import _dirs, diagnostics, gateway_credential_sources as _sources, gateway_instance, service_manager as _sm
from . import setup_fetch, task_io
from .bounded_file_read import read_regular
from .task_io import LockRefused, acquire_lock, install_id, lock_held, lock_holder, lock_path, release_lock  # noqa: F401

EXIT_HELD = 75          # another session holds this work root: a supervisor must not relaunch over it
EXIT_UNUSABLE = 2
EXIT_CHILD_HELD = 1     # a child's own 75 is reported as this, so 75 means only "another holder"
TERM_GRACE_S = 15.0
RUN_TIMEOUT_S = 30
CONFIRM_TIMEOUT_S = 30.0
OP_WAIT_S = 30.0
ACQUIRE_RETRY_S = 1.0
REAP_BOUND_S = 10.0
POLL_S = 0.2
STOP_TIMEOUT_S = 30     # the manager's own SIGTERM-to-SIGKILL bound: past the launcher's grace + reap bound
THROTTLE_S = 30         # launchd's respawn delay: behind a normal SIGTERM shutdown of the old session

# A hand launcher (debug-run) publishes its holder record under this label: the managed launcher is the
# shape's own name, and install confirms only that one, never a hand holder that won the root first.
DEBUG_SUFFIX = ".debug"
# Set by the unit and the plist in the launcher's environment; `run` refuses without it, so a hand
# start never takes the root inside the manager's teardown window. A guard, not a security boundary.
MANAGED_VAR = "AG2_SPARROW_MANAGED"


@dataclass(frozen=True)
class Shape:
    """One adapter's names: `name` (file names, the launchd label, the holder record's adapter), `command`
    (the console script the manager runs with `run`), the unit `description`, the pinned `cli`'s name,
    `min_version`, the `version_anchor` regex a line must match to carry the version, the `version_note`
    for the refusal line, the config vocabulary and what `uninstall` reports as kept."""
    name: str
    command: str
    description: str
    cli: str
    min_version: tuple
    version_anchor: str
    version_note: str
    required_keys: tuple
    config_keys: tuple
    env_keep: tuple
    kept: str
    # Teardown: KillMode, the outer stop bound, the run argv, launchd's keep-alive on a clean exit
    # (None: always), and the installer's manager-command bound, which outlasts that stop.
    kill_mode: str = "control-group"
    stop_timeout_s: int = STOP_TIMEOUT_S
    run_args: tuple = ("run",)
    keep_alive_on_success: "bool | None" = None
    manager_timeout_s: int = RUN_TIMEOUT_S
    service_args: tuple = ("run",)      # systemd's ExecStart arguments
    writer: str = ""        # the install that writes the unit, when it is not `command`'s own

    @property
    def marker(self) -> str:
        return f"# Written by {self.writer or self.command} install; re-run install to update it."

    @property
    def plist_marker(self) -> str:
        return f"<!-- Written by {self.writer or self.command} install; re-run install to update it. -->"

    @property
    def service_name(self) -> str:
        return f"ag2-sparrow-{self.name}.service"

    @property
    def agent_label(self) -> str:
        return f"ai.ag2.sparrow.{self.name}"


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def config_path(shape: Shape) -> Path:
    return _home() / ".config" / "ag2-sparrow" / f"{shape.name}.conf"


def oplock_path(shape: Shape) -> Path:
    """The per-shape operation mutex: a stable file, never unlinked."""
    return _home() / ".config" / "ag2-sparrow" / f"{shape.name}.oplock"


def service_path(shape: Shape) -> Path:
    return _home() / ".config" / "systemd" / "user" / shape.service_name


def plist_path(shape: Shape) -> Path:
    return _home() / "Library" / "LaunchAgents" / f"{shape.agent_label}.plist"


def log_path(shape: Shape) -> Path:
    return _home() / "Library" / "Logs" / "ag2-sparrow" / f"{shape.name}.log"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def supervisor_path(shape: Shape) -> Path:
    return plist_path(shape) if is_macos() else service_path(shape)


def supervisor_marker(shape: Shape) -> str:
    return shape.plist_marker if is_macos() else shape.marker


def job_name(shape: Shape) -> str:
    return shape.agent_label if is_macos() else shape.service_name


# ── the version pin ──────────────────────────────────────────────────────────

def parse_version(text: str, anchor: str) -> "tuple[int, int, int] | None":
    """The X.Y.Z on the line matching `anchor`, so an update notice is never read as the version."""
    for line in (text or "").splitlines():
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", line) if re.search(anchor, line) else None
        if m:
            return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def cli_version(exe: str, anchor: str, run=subprocess.run) -> "tuple[int, int, int] | None":
    """The installed CLI's version, or None when it cannot be read (no answer, or a failing --version)."""
    try:
        r = run([exe, "--version"], capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version((r.stdout or "") + (r.stderr or ""), anchor) if r.returncode == 0 else None


def version_problem(exe: str, shape: Shape, run=subprocess.run) -> str:
    """One line when the CLI is missing or older than the pin, else ''."""
    found = cli_version(exe, shape.version_anchor, run)
    if found is None:
        return f"{exe} --version did not answer; install the {shape.cli} ({_v(shape.min_version)} or newer)"
    if found < shape.min_version:
        return (f"{exe.rsplit('/', 1)[-1]} {_v(found)} is older than the {_v(shape.min_version)} this adapter "
                f"needs ({shape.version_note}); upgrade the {shape.cli}")
    return ""


def _v(t) -> str:
    return ".".join(map(str, t))


# ── the config: written by install, read by run, verify and uninstall ────────

def render_config(shape: Shape, values: dict) -> str:
    """The shape's keys plus ROOT, the canonical work root persisted once at install."""
    rows = "".join(f"{k}={shlex.quote(str(values.get(k, '')))}\n" for k in shape.config_keys + ("ROOT",))
    return f"{shape.marker}\n{rows}"


def read_config(path: Path) -> dict:
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            cfg[k] = "".join(shlex.split(v)) if v else ""
    return cfg


def load_config(shape: Shape) -> "tuple[dict, str]":
    """(config, '') or ({}, why): a missing, dangling, directory, unreadable, unparsable or key-less
    config is one line, never a traceback. The one loader every command reads through."""
    return load_config_at(shape, config_path(shape))


def load_config_at(shape: Shape, path: Path) -> "tuple[dict, str]":
    """load_config for the config at `path`: what a pane's verb reads when its config dir is not the CLI's."""
    if path.is_symlink() and not path.exists():
        return {}, f"{path} is a dangling symlink; remove it by hand (rm {shlex.quote(str(path))}), then run {shape.command} install"
    try:
        cfg = read_config(path)
    except IsADirectoryError:
        return {}, f"{path} is a directory, not a config; remove it yourself"
    except OSError as e:
        return {}, f"{path} unreadable ({e.strerror or e}); run {shape.command} install"
    except ValueError as e:
        return {}, f"{path} is not parsable ({e}); run {shape.command} install"
    missing = [k for k in shape.required_keys + ("ROOT",) if not cfg.get(k)]
    if missing:
        return {}, f"{path} lacks {', '.join(missing)}; run {shape.command} install"
    if not os.path.isabs(cfg["ROOT"]):
        return {}, f"{path} names a relative ROOT {cfg['ROOT']!r}; run {shape.command} install"
    return cfg, ""


def work_root(cfg: dict) -> str:
    """The canonical root install persisted; never re-derived from the inbox path."""
    return cfg["ROOT"]


def canonical_root(work: str, inbox: str) -> "tuple[str, str]":
    """(root, '') from --work, else from the inbox whose LEXICAL basename must be `tasks`: its lexical
    parent, canonicalized once here and persisted, is what the session locks; a `tasks` that is itself
    a symlink is refused; a root with a newline in it is refused before anything is written; else ('', why)."""
    if work:
        root = str(Path(work).expanduser().resolve())
        return _root_checked(root)
    path = Path(os.path.normpath(os.path.expanduser(inbox)))
    if path.name != "tasks":
        return "", f"the inbox must be <work>/tasks, not {path}; pass --work <dir>"
    if path.is_symlink():
        return "", f"{path} is a symlink; tasks must be a real directory under the work root (point --work at it)"
    return _root_checked(str(path.parent.resolve()))


def _root_checked(root: str) -> "tuple[str, str]":
    # The config, unit and plist are line-oriented: a newline in the root cannot be stored or run.
    if "\n" in root or "\r" in root:
        return "", f"the work root {root!r} contains a line break; use a directory whose path has none"
    return root, tasks_link_problem(root)


def tasks_link_problem(root: str) -> str:
    """'' unless `tasks` under the root is a symlink: the session would list the link's target while
    the lock sits on this root, two locks for one inbox."""
    tasks = os.path.join(root, "tasks")
    return f"{tasks} is a symlink; tasks must be a real directory under the work root" if os.path.islink(tasks) else ""


def _write(path: Path, text: str, mode: int) -> None:
    """tmp + os.replace, so a reader never sees a torn file."""
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


def marked(text: str, marker: str) -> bool:
    return marker in text.splitlines()[:2]


def file_text(path: Path, what: str) -> "tuple[str | None, str]":
    """(text, '') for a regular file, (None, '') when nothing is there, (None, why) for anything else."""
    if path.is_dir():
        return None, f"{path} is a directory, not a {what}; remove it yourself"
    if path.is_symlink() and not path.exists():
        return None, f"{path} is a dangling symlink; remove it by hand (rm {shlex.quote(str(path))})"
    try:
        return path.read_text(encoding="utf-8", errors="replace"), ""
    except FileNotFoundError:
        return None, ""
    except OSError as e:
        return None, f"{path} unreadable ({e.strerror or e})"


def supervisor_text(path: Path) -> "tuple[str | None, str]":
    return file_text(path, "supervisor file")


def owned_config(shape: Shape) -> "tuple[dict | None, str]":
    """(config, '') for a fully parsable marked config with a trustworthy ROOT, (None, '') for none, else
    (None, why): a malformed owned config stops install and uninstall before any mutation."""
    conf = config_path(shape)
    text, problem = owned_text(conf, shape.marker, "config")
    if problem:
        return None, problem
    if text is None:
        return None, ""
    cfg, problem = load_config(shape)
    if problem:
        return None, f"{problem.split('; run ')[0]}; fix or remove it by hand (rm {shlex.quote(str(conf))}), then run " \
                     f"{shape.command} install"
    return cfg, ""


def owned_text(path: Path, marker: str, what: str) -> "tuple[str | None, str]":
    """(text, '') for a marked file or nothing, else (None, why): ownership refusals are final and name
    what to remove by hand."""
    text, problem = file_text(path, what)
    if problem:
        return None, problem
    if text is not None and not marked(text, marker):
        return None, f"{path} was not written by install; remove it by hand (rm {shlex.quote(str(path))}) if it is yours to remove"
    return text, ""


def child_env_from(env_keep: tuple, environ=None) -> dict:
    """The allowlisted part of the environment: nothing else reaches a child."""
    base = os.environ if environ is None else environ
    return {k: base[k] for k in env_keep if base.get(k)}


# ── the unit ─────────────────────────────────────────────────────────────────

def console_script(shape: Shape) -> str:
    """This package's console script, beside the running Python first, else on PATH, else ''."""
    for d in (sysconfig.get_path("scripts"), os.path.dirname(sys.executable)):
        p = os.path.join(d, shape.command) if d else ""
        if p and os.access(p, os.X_OK):
            return os.path.abspath(p)
    return shutil.which(shape.command) or ""


def _unit_env(key: str, value: str) -> str:
    """One quoted Environment= line; systemd expands % specifiers and backslash escapes inside quotes."""
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'Environment="{key}={value}"\n'


def render_service(shape: Shape, exec_start: str, env=None) -> str:
    """KillMode=control-group tears the whole cgroup down on stop; KillMode=process signals only the
    launcher and leaves a tmux-hosted session for the launcher's own stop path (then SIGKILL at
    TimeoutStopSec). `env` adds the job's own variables, one quoted line each."""
    args = "".join(f" {a}" for a in shape.service_args)
    extra = "".join(_unit_env(k, v) for k, v in sorted((env or {}).items()))
    return (f"{shape.marker}\n[Unit]\nDescription=ag2-sparrow {shape.name} ({shape.description})\n"
            "# A session that dies 5 times in 5 minutes (e.g. a bad login) is left failed.\n"
            "StartLimitIntervalSec=300\nStartLimitBurst=5\n\n"
            f"[Service]\nExecStart={exec_start}{args}\nEnvironment={MANAGED_VAR}=1\n{extra}Restart=always\nRestartSec=10\n"
            f"KillMode={shape.kill_mode}\nTimeoutStopSec={shape.stop_timeout_s}\n"
            f"# {EXIT_HELD}: another session already serves this work root; do not relaunch over it.\n"
            f"RestartPreventExitStatus={EXIT_HELD}\n\n[Install]\nWantedBy=default.target\n")


def render_plist(shape: Shape, exec_start: str, path_env: str, log: str, env=None) -> str:
    """AbandonProcessGroup=false: when the launcher dies launchd SIGTERMs the rest of its process group
    once (never SIGKILL), then respawns after ThrottleInterval; ExitTimeOut bounds a bootout."""
    keep_alive = True if shape.keep_alive_on_success is None else {"SuccessfulExit": shape.keep_alive_on_success}
    body = {"Label": shape.agent_label, "ProgramArguments": [exec_start, *shape.run_args], "RunAtLoad": True,
            "KeepAlive": keep_alive, "ThrottleInterval": THROTTLE_S, "AbandonProcessGroup": False,
            "ExitTimeOut": shape.stop_timeout_s,
            "EnvironmentVariables": dict(env or {}, PATH=path_env, **{MANAGED_VAR: "1"}),
            "StandardOutPath": log, "StandardErrorPath": log}
    text = plistlib.dumps(body, sort_keys=True).decode("utf-8")
    head, rest = text.split("\n", 1)
    return f"{head}\n{shape.plist_marker}\n{rest}"


def render_supervisor(shape: Shape, env=None) -> "tuple[str, str]":
    """(text, '') or ('', why) when this package's console script cannot be found."""
    exec_start = console_script(shape)
    if not exec_start:
        return "", (f"{shape.command} was not found beside {sys.executable} or on PATH; reinstall ag2-sparrow for "
                    "this Python, then run install again")
    if is_macos():
        return render_plist(shape, exec_start, os.environ.get("PATH", "/usr/bin:/bin"), str(log_path(shape)), env), ""
    return render_service(shape, exec_start, env), ""


# ── the manager: explicit outputs only ───────────────────────────────────────

def _ok(r) -> bool:
    return r is not None and r.returncode == 0


def _why(r) -> str:
    if r is None:
        return "could not run"
    lines = (r.stderr or r.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit {r.returncode}"


def _word(r) -> str:
    return (r.stdout or "").strip().split("\n")[0] if r is not None else ""


def _launchd_target() -> str:
    return f"gui/{os.getuid()}"


def unit_state(shape: Shape) -> dict:
    """{load, active, enabled} from one `systemctl show --value` (its own words only); every field
    'unknown' when it cannot be read."""
    r = _sm._systemctl(("--user", "show", "-p", "LoadState,ActiveState,UnitFileState", "--value"),
                       unit=shape.service_name)
    words = (r.stdout or "").split("\n") if r is not None and r.returncode == 0 else []
    words = [w.strip() for w in words] + ["", "", ""]
    load, active, enabled = words[0], words[1], words[2]
    return {"load": load if load in ("loaded", "not-found", "masked", "bad-setting", "error") else "unknown",
            "active": "active" if active in ("active", "activating", "reloading", "deactivating") else
                      "inactive" if active in ("inactive", "failed") else "unknown",
            "enabled": "enabled" if enabled in ("enabled", "enabled-runtime", "static", "linked", "indirect") else
                       "disabled" if enabled in ("disabled", "not-found", "") and load != "unknown" else "unknown"}


def unit_active(shape: Shape) -> str:
    """active | registered | absent | unknown: a loaded or enabled unit counts as registered."""
    st = unit_state(shape)
    if "unknown" in st.values():
        return "unknown"
    if st["active"] == "active":
        return "active"
    return "absent" if st["load"] == "not-found" and st["enabled"] == "disabled" else "registered"


def unit_load_state(shape: Shape) -> str:
    return unit_state(shape)["load"]


def launchd_service(shape: Shape) -> str:
    """The single service target `gui/<uid>/<label>`: what `bootout --wait` and `print` take."""
    return f"{_launchd_target()}/{shape.agent_label}"


def agent_loaded(shape: Shape) -> str:
    """active | absent | unknown, from `launchctl print`'s exit and its documented not-found error only;
    a registered launchd job is a running one (KeepAlive)."""
    r = _sm._run(["launchctl", "print", launchd_service(shape)])
    if r is None:
        return "unknown"
    if r.returncode == 0:
        return "active"
    return "absent" if "could not find service" in ((r.stderr or "") + (r.stdout or "")).lower() else "unknown"


def manager_state(shape: Shape) -> str:
    """active | registered | absent | unknown, probed from the manager, never from the files."""
    return agent_loaded(shape) if is_macos() else unit_active(shape)


def manager_detail(shape: Shape) -> str:
    """The manager's own words for the job's life, beside the lock in `verify`: systemd's
    `ActiveState/SubState` (`active/running`, `activating/auto-restart`); launchd's `pid = N` and
    `state = …` lines from `launchctl print`; 'absent' or 'unknown' from the same probes."""
    if is_macos():
        r = _sm._run(["launchctl", "print", launchd_service(shape)])
        if r is None:
            return "unknown"
        if r.returncode != 0:
            return "absent" if "could not find service" in ((r.stderr or "") + (r.stdout or "")).lower() else "unknown"
        found = dict(re.findall(r"^\s*(pid|state) = (\S+)", r.stdout or "", re.M))
        words = [f"{k} {found[k]}" for k in ("pid", "state") if k in found]
        return ", ".join(words) if words else "loaded"
    r = _sm._systemctl(("--user", "show", "-p", "ActiveState,SubState", "--value"), unit=shape.service_name)
    if r is None or r.returncode != 0:
        return "unknown"
    active, sub = ((r.stdout or "").split("\n") + ["", ""])[:2]
    if not active.strip():
        return "unknown"
    return "absent" if unit_active(shape) == "absent" else f"{active.strip()}/{sub.strip() or '-'}"


def stop_command(shape: Shape) -> str:
    """What stops the job by hand, verbatim for a refusal."""
    if is_macos():
        return f"launchctl bootout --wait {launchd_service(shape)}"
    return f"systemctl --user stop {shape.service_name}"


def start_supervisor(shape: Shape) -> str:
    """Register and start the unit on disk: '' when the manager accepted, else why not. Never readiness."""
    path = supervisor_path(shape)
    if is_macos():
        log_path(shape).parent.mkdir(parents=True, exist_ok=True)
        r = _sm._run(["launchctl", "bootstrap", _launchd_target(), str(path)], shape.manager_timeout_s)
        return "" if _ok(r) else f"launchctl bootstrap failed ({_why(r)})"
    if not _sm.systemd_user_available():
        return "systemctl --user is not available here"
    unit, bound = shape.service_name, shape.manager_timeout_s
    for verb, run in (("daemon-reload", lambda: _sm._systemctl(("--user", "daemon-reload"), timeout=bound)),
                      (f"enable {unit}", lambda: _sm._systemctl(("--user", "enable"), unit=unit, timeout=bound)),
                      (f"restart {unit}", lambda: _sm._systemctl(("--user", "restart"), unit=unit, timeout=bound))):
        r = run()
        if not _ok(r):
            return f"systemctl --user {verb} failed ({_why(r)})"
    return ""


def stop_supervisor(shape: Shape) -> str:
    """Stop the job through the manager (launchd: a bounded `bootout --wait` on the service target;
    systemd: stop + disable) and require its own word for it: '' once launchd reports it absent or
    systemd reports it inactive and disabled, else why not."""
    if is_macos():
        r = _sm._run(["launchctl", "bootout", "--wait", launchd_service(shape)], shape.manager_timeout_s)
        if r is None:
            return f"the stop command for {job_name(shape)} could not run"
        state = manager_state(shape)
        if state == "absent":
            return ""
        return (f"{job_name(shape)} is still active after the stop ({_why(r)})" if state == "active" else
                f"{job_name(shape)}'s state after the stop is unknown ({_why(r)})")
    r = _sm._systemctl(("--user", "stop"), unit=shape.service_name, timeout=shape.manager_timeout_s)
    if r is None:
        return f"the stop command for {job_name(shape)} could not run"
    _sm._systemctl(("--user", "disable"), unit=shape.service_name)
    st = unit_state(shape)
    if st["active"] == "inactive" and st["enabled"] == "disabled":
        return ""
    if "unknown" in (st["active"], st["enabled"]):
        return f"{job_name(shape)}'s state after the stop is unknown ({_why(r)})"
    return f"{job_name(shape)} is still {st['active']} and {st['enabled']} after the stop ({_why(r)})"


def linger(shape: Shape) -> None:
    if is_macos():
        print(f"loaded: {shape.agent_label} (it stops at logout and starts again at your next login)")
        return
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    r = _sm._run(["loginctl", "--no-ask-password", "enable-linger", user]) if user else None
    print(f"linger: enabled for {user}, so the session runs while you are logged out" if _ok(r) else
          f"NOTE: loginctl enable-linger {user or '$USER'} failed ({_why(r)}); the session stops at logout until "
          "an administrator runs it")


# ── the operation lock and the holder ────────────────────────────────────────

class Busy(Exception):
    pass


class operation_lock:
    """One cheap per-shape mutex across install, uninstall and debug-run's check-then-acquire: a flock
    on a stable file, waited for briefly, then Busy with one line. `release()` is idempotent."""

    def __init__(self, shape: Shape):
        self.shape, self.fd = shape, -1

    def __enter__(self):
        path = oplock_path(self.shape)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        deadline = time.monotonic() + OP_WAIT_S
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.release()
                    raise Busy(f"another {self.shape.command} operation is in progress (lock {path}); wait for it, "
                               "then try again")
                time.sleep(0.1)

    def release(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __exit__(self, *exc):
        self.release()
        return False


def holder_line(root: str) -> str:
    """held by launcher pid N | held by an unrecorded holder | free | unknown (why). The flock is held
    by a launcher for its own life only, so a held lock names a live launcher or a holder between its
    flock and its record; a dead pid in the record under a held lock is the latter, never a child."""
    state = lock_held(root)
    if state == "free":
        return "free"
    if state == "unknown":
        return f"unknown (the lock of {root} could not be probed)"
    rec = lock_holder(root)
    return f"held by launcher pid {rec['pid']}" if rec.get("live") else "held by an unrecorded holder"


def holder_words(root: str, labels: tuple, _rec=None) -> str:
    """Who holds the root in the shape's nouns, `labels` = (holder, consumer): the holder and, once
    attached, its live consumer, as verify and install's closing line both say it."""
    holder, consumer_noun = labels
    rec, consumer = task_io.lock_holder(root), task_io.holder_consumer(root)
    return f"{holder} pid {rec.get('owner_pid')}" + (f", its {consumer_noun} pid {consumer['pid']}"
                                                     if consumer and consumer.get("live") else f", no live {consumer_noun} attached")


def occupancy_line(shape: Shape, root: str, labels=None) -> str:
    """The lock beside the manager's state: a free lock under a live or respawning job is 'restarting
    (manager …, lock free)', never 'free'; held is 'held by launcher pid N (manager …)'; absent and free
    is 'free (no job)'; a stopped or unprobeable job with a free lock says so as 'free (manager …)'.
    With `labels` (holder, consumer) the shared tri-state is read first: held names the holder and,
    once attached, the consumer whose liveness fences the root; unobserved never reads free."""
    if labels is not None:
        occ, manager = task_io.occupancy(root), manager_detail(shape)
        if occ == "held":
            return f"held by {holder_words(root, labels)} (manager {manager})"
        if occ == "unobserved":
            return f"unobserved (the root's holder or consumer cannot be verified; manager {manager})"
    lock, manager = holder_line(root), manager_detail(shape)
    if lock != "free":
        return f"{lock} (manager {manager})"
    if manager == "absent":
        return "free (no job)"
    if manager == "unknown" or manager.split("/")[0] in ("inactive", "failed"):
        return f"free (manager {manager})"
    return f"restarting (manager {manager}, lock free)"


def recovery_lines(shape: Shape, root: str) -> list:
    rec, lock = lock_holder(root), lock_path(root)
    who = (f"kill {rec['pid']} (the launcher pid in the lock record)" if rec.get("live") else
           f"no live launcher is recorded; find the holder with: lsof {shlex.quote(lock or '<lock file>')}")
    return [f"Manual recovery: 1. stop the job by hand: {stop_command(shape)}",
            f"2. stop what holds the work root {root}: {who}",
            f"3. re-run {shape.command} uninstall"]


def _refuse(line: str, more=()) -> int:
    print(f"error: {line}", file=sys.stderr)
    for extra in more:
        print(f"  {extra}", file=sys.stderr)
    return 1


def _await_free(root: str) -> str:
    """'' once the root's flock is free within the bound; else who still holds it (unknown refuses)."""
    deadline = time.monotonic() + CONFIRM_TIMEOUT_S
    while True:
        state = lock_held(root)
        if state == "free":
            return ""
        if state == "unknown" or time.monotonic() >= deadline:
            return f"{root} is {holder_line(root)}"
        time.sleep(POLL_S)


def _fresh_holder(shape: Shape, root: str, before: dict) -> "dict | None":
    """The record once the root is held by a live launcher of this shape from this very install, other
    than `before`'s (pid and start), within the bound. The flock is occupancy, the record attribution:
    never the session's readiness."""
    deadline = time.monotonic() + CONFIRM_TIMEOUT_S
    mine = install_id()
    while True:
        rec = lock_holder(root) if lock_held(root) == "held" else {}
        if rec.get("live") and rec.get("adapter") == shape.name and rec.get("install_id") == mine \
                and (rec["pid"], rec["start"]) != (before.get("pid"), before.get("start")):
            return rec
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_S)


# ── install ──────────────────────────────────────────────────────────────────

def install(shape: Shape, values: dict, root: str, revise=None, precheck=None, holder=None) -> int:
    """`revise(old_config, values) -> values` runs once the job is stopped and the root proven free,
    so a value that may only change over a quiescent root (a session id) is decided there.
    `precheck(root) -> problem` is the launcher's own refusal of the root, asked before anything moves.
    `holder(root, record)` names who holds the root in the closing line, in the shape's own words."""
    try:
        with operation_lock(shape):
            return _install_locked(shape, values, root, revise, precheck, holder)
    except Busy as e:
        return _refuse(str(e))


def launcher_log(shape: Shape) -> str:
    """Where the launcher's own lines go: the plist's log file, or the unit's journal."""
    return str(log_path(shape)) if is_macos() else f"journalctl --user -u {shape.service_name}"


def _install_locked(shape: Shape, values: dict, root: str, revise=None, precheck=None, holder=None) -> int:
    """A quiescent replacement: both files owned, the same root as before, no orphan on it, the job
    stopped and absent, the root free; only then the files, the start, and the fresh holder."""
    conf, sup, marker = config_path(shape), supervisor_path(shape), supervisor_marker(shape)
    old, problem = owned_config(shape)
    if problem:
        return _refuse(problem)
    old = old or {}
    unit_text, problem = owned_text(sup, marker, "supervisor file")
    if problem:
        return _refuse(problem)
    if old.get("ROOT") and old["ROOT"] != root:
        return _refuse(f"the work root is {old['ROOT']}, not {root}; run {shape.command} uninstall, then "
                       f"{shape.command} install")
    problem = precheck(root) if precheck is not None else ""
    if problem:
        return _refuse(f"{problem}; nothing was applied")
    unit, problem = render_supervisor(shape)
    if problem:
        return _refuse(problem)
    state, manager = lock_held(root), manager_state(shape)
    if state == "unknown" or manager == "unknown":
        return _refuse(f"{root} is {holder_line(root)} and {job_name(shape)} is {manager}; nothing was applied")
    if state == "held" and manager != "active":
        rec = lock_holder(root)
        who = f"launcher pid {rec['pid']}" if rec.get("live") else "an unrecorded holder; lsof the lock file"
        return _refuse(f"{root} is held by a session no job of {job_name(shape)} manages ({who}); stop it, then run "
                       "install again; nothing was applied")
    before = lock_holder(root)
    if unit_text is not None or manager != "absent":
        problem = stop_supervisor(shape)
        if problem:
            return _refuse(f"{problem}; nothing was applied")
        print(f"stopped: {job_name(shape)}")
    problem = _await_free(root)
    if problem:
        return _refuse(f"{problem} after the stop; nothing was applied")
    if revise is not None:
        values = revise(old, values)
    for key in ("INBOX_DIR", "RESULTS_DIR"):
        Path(values[key]).mkdir(parents=True, exist_ok=True)
    _write(conf, render_config(shape, dict(values, ROOT=root)), 0o600)
    _write(sup, unit, 0o644)
    print(f"{'installed' if not old else 'changed' if old != dict(values, ROOT=root) else 'rewritten'}: {conf}")
    print(f"installed: {sup}")
    problem = start_supervisor(shape)
    if problem:
        return _refuse(f"{problem}; the managed launcher did not acquire {root}; run {shape.command} uninstall, "
                       f"then {shape.command} install")
    rec = _fresh_holder(shape, root, before)
    if rec is None:
        return _refuse(f"the managed launcher did not acquire {root} within {CONFIRM_TIMEOUT_S:.0f} s ({holder_line(root)}); "
                       f"its refusal is in {launcher_log(shape)}; run {shape.command} uninstall, then {shape.command} install")
    linger(shape)
    print(f"the managed launcher acquired the expected root: {root} "
          f"({holder(root, rec) if holder is not None else 'launcher pid %s' % rec['pid']})")
    return 0


# ── run and debug-run ────────────────────────────────────────────────────────

def _is_link(name: str, dir_fd: int) -> bool:
    """O_NOFOLLOW on a link reads ELOOP on Linux and ENOTDIR with O_DIRECTORY on macOS: ask lstat instead."""
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def managed_problem(shape: Shape, environ=None) -> str:
    """'' when the manager's marker is in the environment, else the one line `run` refuses with. The
    gate guards against an accidental hand start inside the manager's teardown window; it is not a
    security boundary."""
    base = os.environ if environ is None else environ
    if base.get(MANAGED_VAR) == "1":
        return ""
    return (f"{shape.command} run is the manager's entrypoint ({MANAGED_VAR}=1 in the unit's environment); "
            f"for a session by hand run {shape.command} debug-run")


def stop_state() -> dict:
    return {"child": None, "pidfd": None, "sig": None, "installed": None}


def stop_handlers(state: dict) -> dict:
    """SIGTERM and SIGINT as one stop path, installed directly over the inherited dispositions (an
    inherited SIG_IGN is replaced; no SIG_DFL step, so no window with a default action). The handler
    only records the first signal: terminate, kill, wait and release are the main loop's. An inherited
    SIGCHLD=SIG_IGN would let the kernel reap the child before wait() could: reset to SIG_DFL."""
    def on_stop(signum, frame):
        state["sig"] = state["sig"] or signum
    before = {sig: signal.signal(sig, on_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        before[signal.SIGCHLD] = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    state["installed"] = True
    return before


def restore_handlers(state: dict, before: dict) -> None:
    for sig, handler in before.items():
        signal.signal(sig, handler)
    state["installed"] = None


def _signal(child, pidfd, sig) -> None:
    """Only the unreaped direct child: through the pidfd when one is open, else the Popen handle (which
    guards a reaped child itself). Never a bare number, never a group, nothing once wait() returned."""
    if child.returncode is not None:
        return
    if pidfd is None:
        child.send_signal(sig)
        return
    try:
        signal.pidfd_send_signal(pidfd, sig)
    except ProcessLookupError:
        pass


def _run_until_end(child, pidfd, state: dict) -> "int | None":
    """Poll until the child ends; the first recorded stop terminates it, the grace expiring kills it;
    None when the kill is not confirmed within the bound."""
    stop_at = None
    while True:
        if state["sig"] is not None and stop_at is None:
            _signal(child, pidfd, signal.SIGTERM)
            stop_at = time.monotonic() + TERM_GRACE_S
        try:
            return child.wait(timeout=POLL_S)
        except subprocess.TimeoutExpired:
            if stop_at is not None and time.monotonic() > stop_at:
                _signal(child, pidfd, signal.SIGKILL)
                try:
                    return child.wait(timeout=REAP_BOUND_S)
                except subprocess.TimeoutExpired:
                    return None


def debug_adapter(shape: Shape) -> str:
    return shape.name + DEBUG_SUFFIX


def hold_and_supervise(shape: Shape, root: str, state: dict, prepare, spawn, supervise, reap, label: str, noun: str,
                       child_noun: str = "", started=None, adapter: str = "", acquired=None) -> int:
    """The holder's one sequence, in `label`'s words: take the root as `adapter` (the shape's name unless a
    hand launcher says otherwise; another holder is EXIT_HELD, any other refusal EXIT_UNUSABLE), then
    `acquired()` (a hand launcher releases its operation lock there, the check-then-acquire done),
    refuse a `tasks` symlink, `prepare(owner, root) -> (ctx, problem)` (it may raise LockRefused), stop
    before start as 128+signal, read the record, `spawn(ctx) -> child` (OSError is the refusal), attach
    the child as the consumer by its pid and start time, `started(child)` once attached, then
    `supervise(child) -> int`; any failure between spawn and attach is `reap(child)` and "no consumer".
    The lock is released with the holder; a release refused for a live consumer is said in one line.
    Every `task_io` step is looked up at call time."""
    child_noun = child_noun or noun

    def refuse(line: str) -> int:
        print(f"{label}: refusing to start: {line}", file=sys.stderr)
        return EXIT_UNUSABLE
    try:
        owner = task_io.acquire_lock(root, adapter or shape.name, retry_s=ACQUIRE_RETRY_S)
    except LockRefused as e:
        if acquired is not None:
            acquired()
        print(e.line, file=sys.stderr)
        return EXIT_HELD if e.code == 3 else EXIT_UNUSABLE
    if acquired is not None:
        acquired()
    try:
        if _is_link("tasks", owner.root_fd):
            return refuse("tasks/ under the held root is a symlink, not a directory")
        try:
            ctx, problem = prepare(owner, root)
        except LockRefused as e:
            print(e.line, file=sys.stderr)
            return EXIT_HELD if e.code == 3 else EXIT_UNUSABLE
        if problem:
            return refuse(problem)
        if state["sig"] is not None:
            print(f"{label}: stopped before the {noun} started", file=sys.stderr)
            return 128 + state["sig"]
        expected = task_io.holder_record_bytes(owner)
        try:
            child = spawn(ctx)
        except OSError as e:
            return refuse(f"the {noun} could not be spawned ({e.strerror or e})")
        try:
            start = task_io.start_time(child.pid)
            if start is None:
                raise LockRefused(2, f"the {child_noun}'s start time is unreadable")
            task_io.attach_consumer(owner, expected, child.pid, start)
            if started is not None:
                started(child)
        except Exception as e:     # holder_lock.LockError arrives as Exception here
            reap(child)
            return refuse(f"no consumer: {e}")
        return supervise(child)
    finally:
        if not task_io.release_lock(owner):
            print(f"{label}: the {noun} may still be alive; the root stays held by it and this flock until exit",
                  file=sys.stderr)


def supervise_child(child, state: dict, label: str, noun: str) -> int:
    """Wait for the direct child; a recorded stop terminates it, bounded, then kills; its exit code."""
    state["child"] = child
    rc = _run_until_end(child, None, state)
    if rc is None:
        print(f"{label}: the {noun} did not die within the bound; the manager's to clean", file=sys.stderr)
        return 128 + state["sig"]
    if state["sig"] is not None:
        return 128 + state["sig"]
    return rc


def kill_and_reap(child) -> None:
    """A child that may never be attached: kill, then reap within the bound."""
    try:
        child.kill()
    except OSError:
        pass
    try:
        child.wait(timeout=REAP_BOUND_S)
    except subprocess.TimeoutExpired:
        pass


# ── the launchd entry shim ───────────────────────────────────────────────────

def launchd_exit(rc: int) -> int:
    """The entry shim's table: logical 75 is terminal (exit 0, launchd stops respawning); every other
    return, and any signal death, is non-zero so launchd respawns after the throttle."""
    if rc == EXIT_HELD:
        return 0
    if rc <= 0:
        return 1
    return rc


def run_launchd(child_argv: list, popen=subprocess.Popen) -> int:
    """`run --launchd`: the launcher as a child, the stop signals forwarded, its outcome mapped."""
    child = popen(child_argv)
    forwarded = []

    def forward(signum, frame):
        if child.poll() is None:
            child.send_signal(signum)
            forwarded.append(signum)
    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        rc = child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return launchd_exit(rc)


def run_child_argv(shape: Shape, module: str) -> list:
    """The launcher the shim runs: the console script with `run`, else this interpreter on `module`."""
    script = console_script(shape)
    return [script, "run"] if script else [sys.executable, "-c", f"from {module} import main; raise SystemExit(main(['run']))"]


# ── verify and uninstall ─────────────────────────────────────────────────────

def check(results: list, ok: bool, name: str, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")


def relay_check(results: list, relay: Path) -> None:
    """The token file's mode, never its content."""
    try:
        mode = stat.S_IMODE(relay.stat().st_mode)
        check(results, not mode & 0o077, "relay token", f"{relay} mode {oct(mode)[2:]} (Sparrow's; never copied here)")
    except OSError:
        check(results, False, "relay token", f"{relay} missing: Sparrow has no token to poll with")


def verify_tail(shape: Shape, cfg: dict, results: list, occupancy=None, relay=None) -> None:
    """The rows every shape ends with: its bridge's token rows (`relay(results)`: only the shape knows
    which bridge and token serve its root), the marked unit, the manager's word, and who holds the work
    root (`occupancy(root)`: a shape's own wording of it)."""
    if relay is not None:
        relay(results)
    sup = supervisor_path(shape)
    text, problem = supervisor_text(sup)
    check(results, text is not None and marked(text, supervisor_marker(shape)), "supervisor",
          problem or (f"{sup} missing; run {shape.command} install" if text is None else str(sup)))
    print(f"INFO job: {job_name(shape)} {manager_state(shape)}")
    root = work_root(cfg)
    print(f"INFO session: {root} {occupancy(root) if occupancy is not None else occupancy_line(shape, root)}")
    if not is_macos():
        print(f"INFO systemd ordering is asserted from KillMode={shape.kill_mode} and not witnessed")
    elif shape.kill_mode == "control-group":
        print("INFO launchd SIGTERMs the group once after a launcher death and respawns after "
              f"{THROTTLE_S} s; a session that ignores SIGTERM overlaps the new one (bootout, then stop the stray)")
    else:
        print(f"INFO launchd respawns the launcher after {THROTTLE_S} s; the session in its tmux pane is not in "
              "the launcher's process group and survives a launcher death until the next launcher adopts it")


def kept_line(shape: Shape, relay=None) -> str:
    """What uninstall leaves in place, naming the token file only when it exists; `relay` is the one
    the install configured ('' for none), else Sparrow's device env."""
    relay = _dirs.device_env_path() if relay is None else relay
    return f"kept: {relay} (Sparrow's token) and {shape.kept}" if relay and Path(relay).exists() else f"kept: {shape.kept}"


def uninstall(shape: Shape, before_free=None, token_file=None) -> int:
    """`before_free(config) -> problem` runs after the manager stop and before the root must read free:
    a shape whose session outlives its launcher stops it there. `token_file(config)` names the token
    file the install configured, for the kept line."""
    try:
        with operation_lock(shape):
            return _uninstall_locked(shape, before_free, token_file)
    except Busy as e:
        return _refuse(str(e))


def _uninstall_locked(shape: Shape, before_free=None, token_file=None) -> int:
    """any -> absent, config last: stop through the manager, its explicit absent (systemd: inactive and
    disabled), the lock free, the marked unit removed, systemd reloaded with LoadState=not-found, then the
    config. Any unknown or failure keeps the config, so a rerun still knows the root; every step re-runs."""
    conf, sup, marker = config_path(shape), supervisor_path(shape), supervisor_marker(shape)
    unit, problem = owned_text(sup, marker, "supervisor file")
    if problem:
        return _refuse(problem)
    cfg, problem = owned_config(shape)
    if problem:
        return _refuse(problem)
    root = cfg["ROOT"] if cfg else ""
    relay = token_file(cfg) if token_file is not None and cfg else None
    manager = manager_state(shape)
    if manager == "unknown":
        return _refuse(f"{job_name(shape)}'s state is unknown; nothing was removed", recovery_lines(shape, root) if root else ())
    if unit is None and cfg is None and manager == "absent":
        print(f"nothing to remove: {shape.name} is not installed")
        print(kept_line(shape, relay))
        return 0
    kept = (f"{sup} and {conf} were" if unit is not None and cfg else f"{conf} was" if cfg else
            f"{sup} was" if unit is not None else "nothing was") + " kept so a rerun knows the work root"
    recovery = recovery_lines(shape, root) if root else ()
    if unit is not None or manager != "absent":
        problem = stop_supervisor(shape)
        if problem:
            return _refuse(f"{problem}; {kept}", recovery)
        print(f"stopped: {job_name(shape)}")
    if root and before_free is not None:
        problem = before_free(cfg)
        if problem:
            return _refuse(f"{problem}; {kept}", recovery)
    if root:
        problem = _await_free(root)
        if problem:
            return _refuse(f"{problem}; {kept}", recovery)
    if unit is not None:
        sup.unlink()
        print(f"removed: {sup}")
    if not is_macos():
        r = _sm._systemctl(("--user", "daemon-reload"))
        if not _ok(r):
            return _refuse(f"systemctl --user daemon-reload failed ({_why(r)}); {kept}; run uninstall again", recovery)
        if unit_load_state(shape) != "not-found":
            return _refuse(f"{shape.service_name} is still known to systemd (LoadState {unit_load_state(shape)}); {kept}; "
                           "run uninstall again", recovery)
    if cfg is not None:
        conf.unlink()
        print(f"removed: {conf}")
    print(kept_line(shape, relay))
    return 0


# ── connect --code: redeem a one-time code for the agent's relay credential ─────

CODE_PREFIX = "acp."        # what AG2 Space issues for an agent run by a local CLI
REDEEM_PATH = "/connect/poll"
REDEEM_TIMEOUT_S = 15
REDEEM_BACKOFF_S = (1, 2, 4, 8, 16)     # one wait per 503 or 202 before the next attempt
RETRY_AFTER_MAX_S = 30
STANDALONE_TIER = "owner"   # no core on the host to cap it (plan 9.2); the key is required
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
GONE = {"agent_revoked": "the agent was removed in AG2 Space; add it again there for a new code"}


def endpoint_problem(url: str, what: str) -> str:
    """'' when `url` is https (plain http only on a loopback host, for tests), else why not."""
    why = _sources.gateway_url_problem(url or "")
    if why:
        return f"{what} {why}"
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https" and parts.hostname not in LOOPBACK_HOSTS:
        return f"{what} is not https; only a loopback host may use plain http"
    return ""


def _relay_host_ok(relay_host: str, base_host: str) -> bool:
    """The relay is on the base's own host or under it. A shared parent domain is not enough: without the
    public suffix list, two hosts under one suffix (a.co.uk, b.co.uk) cannot be told apart from one owner's."""
    relay_host, base_host = relay_host.rstrip(".").lower(), base_host.rstrip(".").lower()
    return bool(base_host) and (relay_host == base_host or relay_host.endswith("." + base_host))


def relay_problem(base: str, relay_url: str, trusted_host: str = "") -> str:
    """'' when the relay URL the backend returned may be written: https, and on the base's host (or under it)
    unless the owner named its host with --trust-relay-host; else why not."""
    why = endpoint_problem(relay_url, "the relay URL AG2 Space returned")
    if why:
        return why
    host = (urllib.parse.urlsplit(relay_url).hostname or "").lower()
    base_host = urllib.parse.urlsplit(base).hostname or ""
    if not _relay_host_ok(host, base_host) and host != trusted_host.lower():
        return (f"the relay URL AG2 Space returned is on {host}, not {base_host} or a host under it; if that relay "
                f"is expected, connect again with a new code and --trust-relay-host {host}")
    return ""


def code_problem(base: str, code: str) -> str:
    """'' when `base` is a usable backend URL and `code` an agent code, else why not, without the code."""
    why = endpoint_problem(base, "--base")
    if why:
        return why
    if not (code or "").startswith(CODE_PREFIX) or len(code) <= len(CODE_PREFIX) or any(c.isspace() for c in code):
        return f"the code is not an agent code (it starts with {CODE_PREFIX}); copy it from AG2 Space again"
    return ""


def read_code(arg, stdin=None, prompt=None) -> "tuple[str, str]":
    """(code, '') or ('', why): `--code -` reads one line from stdin, no --code prompts on a terminal, and a
    code on the command line still works with a warning, since ps and shell history keep it."""
    stdin = stdin or sys.stdin
    if arg == "-":
        return stdin.readline(4096).strip(), ""
    if arg is None:
        if not stdin.isatty():
            return "", "no code: pass --code - and pipe the code in, or run connect in a terminal to be asked for it"
        return (prompt or getpass.getpass)("AG2 Space code: ").strip(), ""
    print("warning: a code on the command line is kept by ps and shell history; next time use --code - or the "
          "prompt", file=sys.stderr)
    return arg.strip(), ""


def _post_json(url: str, body: dict, opener) -> "tuple[int, bytes]":
    with gateway_instance.gateway_call(url):
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with (opener or urllib.request.urlopen)(req, timeout=REDEEM_TIMEOUT_S) as resp:
                return resp.status, resp.read(64 * 1024)
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read(64 * 1024) or b""
            except Exception:  # noqa: BLE001 - an unreadable error body is only an empty one
                return e.code, b""


def _answer(raw: bytes) -> dict:
    try:
        got = json.loads((raw or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _retry_after(answer: dict, default: float) -> float:
    value = answer.get("retry_after")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return default
    return min(max(float(value), 1.0), RETRY_AFTER_MAX_S)


def redeem_code(base: str, code: str, opener=None, sleep=None) -> "tuple[dict | None, str]":
    """({relay_url, bearer, agent_id}, '') or (None, why). The bearer and URL are registered as secrets
    before this returns, so no later line can print them. A 503 or a 202 (still redeeming) is retried on
    REDEEM_BACKOFF_S, a 202's retry_after honoured up to RETRY_AFTER_MAX_S."""
    sleep = sleep or time.sleep
    url = base.rstrip("/") + REDEEM_PATH
    for wait in (*REDEEM_BACKOFF_S, None):
        try:
            status, raw = _post_json(url, {"device_code": code}, opener)
        except (urllib.error.URLError, OSError, ValueError) as e:
            return None, (f"could not reach {base} ({diagnostics.describe(e)}); the code is still valid until it "
                          "expires, try again")
        answer = _answer(raw)
        if status in (202, 503) and wait is not None:
            sleep(_retry_after(answer, wait) if status == 202 else wait)
            continue
        if status == 202:
            return None, "AG2 Space is still redeeming the code; run connect again with the same code in a minute"
        if status == 503:
            return None, "AG2 Space is busy (HTTP 503 on every try); the code is still valid, try again in a minute"
        if status == 404:
            reason = next((answer[k] for k in ("error", "reason", "state") if isinstance(answer.get(k), str)), "")
            return None, GONE.get(reason, "the code was already used or has expired; make a new one in AG2 Space")
        if status != 200:
            return None, f"AG2 Space refused the code (HTTP {status})"
        fields = {k: answer.get(k) for k in ("relay_url", "bearer", "agent_id")}
        for value in (fields["bearer"], fields["relay_url"]):
            if isinstance(value, str) and value:
                diagnostics.register_secret(value)
        if not all(isinstance(v, str) and v.strip() for v in fields.values()):
            return None, "AG2 Space's answer had no relay_url, bearer or agent_id; the code is spent, make a new one"
        diagnostics.register_gateway_url(fields["relay_url"])
        return {k: v.strip() for k, v in fields.items()}, ""
    return None, "unreachable"  # pragma: no cover - the loop always returns


def seat_instance(seat) -> str:
    """The seat's GATEWAY_INSTANCE when its .env already holds a valid one (kept across reconnects), else
    a fresh one."""
    path = os.path.join(_sources.seat_channel_dir(seat), ".env")
    text, _why = read_regular(path)
    kept = _sources.parse_env_text(text or "").get("GATEWAY_INSTANCE", "")
    return kept if _sources.SEAT_INSTANCE_RE.fullmatch(kept) else _sources.mint_gateway_instance()


def seat_env_path(seat) -> str:
    """The seat's .env, where write_seat puts it."""
    return os.path.join(_sources.seat_channel_dir(seat), ".env")


def write_seat(seat, redeemed: dict) -> "tuple[str, str]":
    """(the seat's .env path, '') or ('', why): the redeemed credential written by its owner, standalone
    tier, the instance kept or minted once."""
    problem = _sources.write_seat_credentials(seat, agent_id=redeemed["agent_id"], instance=seat_instance(seat),
                                              tier=STANDALONE_TIER, relay_url=redeemed["relay_url"],
                                              bearer=redeemed["bearer"])
    return ("", problem) if problem else (seat_env_path(seat), "")


def served_setup_line(vendor: str) -> str:
    """Which setup this connect follows: the served one when the backend gives one, else the built-in."""
    setup = setup_fetch.fetch_setup(kind="acp", vendor=vendor)
    revision = diagnostics.revision_field((setup or {}).get("revision"))
    return f"setup: served revision {revision}" if revision is not None else "setup: built-in (no served setup)"
