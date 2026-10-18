"""The supervised-session lifecycle every `ag2-sparrow-<shape>-local` command shares, as one owner.

The service manager (systemd --user, launchd) is the only supervisor; the work-root flock is occupancy
and the holder record attribution, never readiness. `install` is a quiescent replacement under one
per-shape operation lock: stop the job and prove it absent, prove the old root free, write the config
and the unit (tmp + replace), register and start, then require the root held by a fresh holder of this
shape; anything less is one line and the files as written, so an identical install is the retry.
`uninstall` stops, proves absent, proves the root free, removes the unit, reloads, and the config last.
An unknown probe authorises nothing. The launcher takes the lock, leases its descriptor to the child,
runs the child against `tasks` under the held root inode, and releases only after a confirmed reap.

Package level, parametrised by a Shape the adapter passes in: this module names no adapter and imports
none, so each adapter owns its console script and the boundary rule holds. Stdlib only.
"""
from __future__ import annotations

import fcntl
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
import time
from dataclasses import dataclass
from pathlib import Path

from . import _dirs
from .task_io import (LockRefused, acquire_lock, install_id, lease_fd, lock_held, lock_holder, lock_path,
                      release_after_reap, release_lock)

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
# The small parent between the launcher and the session: it runs the session with cwd = the held
# tasks inode and the lease fd shared (the root stays held while either of them lives), reaps it on any signal.
TRAMPOLINE = """
import os, signal, subprocess, sys
lease, tasks = int(sys.argv[1]), int(sys.argv[2])
os.fchdir(tasks); os.close(tasks)
env = dict(os.environ, AG2_INBOX_DIR=".", AG2_RESULTS_DIR=os.path.join("..", "results"))
child = subprocess.Popen(sys.argv[3:], env=env, close_fds=True, pass_fds=(lease,))
for sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(sig, lambda signum, frame: child.send_signal(signum))
while True:
    try:
        sys.exit(child.wait())
    except KeyboardInterrupt:
        child.terminate()
"""


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

    @property
    def marker(self) -> str:
        return f"# Written by {self.command} install; re-run install to update it."

    @property
    def plist_marker(self) -> str:
        return f"<!-- Written by {self.command} install; re-run install to update it. -->"

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
    path = config_path(shape)
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
    a symlink is refused; else ('', why)."""
    if work:
        root = str(Path(work).expanduser().resolve())
        return root, tasks_link_problem(root)
    path = Path(os.path.normpath(os.path.expanduser(inbox)))
    if path.name != "tasks":
        return "", f"the inbox must be <work>/tasks, not {path}; pass --work <dir>"
    if path.is_symlink():
        return "", f"{path} is a symlink; tasks must be a real directory under the work root (point --work at it)"
    root = str(path.parent.resolve())
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


def render_service(shape: Shape, exec_start: str) -> str:
    return (f"{shape.marker}\n[Unit]\nDescription=ag2-sparrow {shape.name} ({shape.description})\n"
            "# A session that dies 5 times in 5 minutes (e.g. a bad login) is left failed.\n"
            "StartLimitIntervalSec=300\nStartLimitBurst=5\n\n"
            f"[Service]\nExecStart={exec_start} run\nRestart=always\nRestartSec=10\n"
            f"# {EXIT_HELD}: another session already serves this work root; do not relaunch over it.\n"
            f"RestartPreventExitStatus={EXIT_HELD}\n\n[Install]\nWantedBy=default.target\n")


def render_plist(shape: Shape, exec_start: str, path_env: str, log: str) -> str:
    body = {"Label": shape.agent_label, "ProgramArguments": [exec_start, "run"], "RunAtLoad": True,
            "KeepAlive": True, "ThrottleInterval": 10, "EnvironmentVariables": {"PATH": path_env},
            "StandardOutPath": log, "StandardErrorPath": log}
    text = plistlib.dumps(body, sort_keys=True).decode("utf-8")
    head, rest = text.split("\n", 1)
    return f"{head}\n{shape.plist_marker}\n{rest}"


def render_supervisor(shape: Shape) -> "tuple[str, str]":
    """(text, '') or ('', why) when this package's console script cannot be found."""
    exec_start = console_script(shape)
    if not exec_start:
        return "", (f"{shape.command} was not found beside {sys.executable} or on PATH; reinstall ag2-sparrow for "
                    "this Python, then run install again")
    if is_macos():
        return render_plist(shape, exec_start, os.environ.get("PATH", "/usr/bin:/bin"), str(log_path(shape))), ""
    return render_service(shape, exec_start), ""


# ── the manager: explicit outputs only ───────────────────────────────────────

def _run(cmd: list) -> "subprocess.CompletedProcess | None":
    """A manager command; None when it could not run or did not return in time. Never prompts."""
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None


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


def _systemctl(*args) -> "subprocess.CompletedProcess | None":
    return _run(["systemctl", "--user", "--no-ask-password", *args])


def unit_state(shape: Shape) -> dict:
    """{load, active, enabled} from one `systemctl show --value` (its own words only); every field
    'unknown' when it cannot be read."""
    r = _systemctl("show", "-p", "LoadState,ActiveState,UnitFileState", "--value", shape.service_name)
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
    r = _run(["launchctl", "print", launchd_service(shape)])
    if r is None:
        return "unknown"
    if r.returncode == 0:
        return "active"
    return "absent" if "could not find service" in ((r.stderr or "") + (r.stdout or "")).lower() else "unknown"


def manager_state(shape: Shape) -> str:
    """active | registered | absent | unknown, probed from the manager, never from the files."""
    return agent_loaded(shape) if is_macos() else unit_active(shape)


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
        r = _run(["launchctl", "bootstrap", _launchd_target(), str(path)])
        return "" if _ok(r) else f"launchctl bootstrap failed ({_why(r)})"
    if not _ok(_systemctl("show-environment")):
        return "systemctl --user is not available here"
    for verb in (("daemon-reload",), ("enable", shape.service_name), ("restart", shape.service_name)):
        r = _systemctl(*verb)
        if not _ok(r):
            return f"systemctl --user {' '.join(verb)} failed ({_why(r)})"
    return ""


def stop_supervisor(shape: Shape) -> str:
    """Stop the job through the manager (launchd: a bounded `bootout --wait` on the service target;
    systemd: stop + disable) and require its own word for it: '' once launchd reports it absent or
    systemd reports it inactive and disabled, else why not."""
    if is_macos():
        r = _run(["launchctl", "bootout", "--wait", launchd_service(shape)])
        if r is None:
            return f"the stop command for {job_name(shape)} could not run"
        state = manager_state(shape)
        if state == "absent":
            return ""
        return (f"{job_name(shape)} is still active after the stop ({_why(r)})" if state == "active" else
                f"{job_name(shape)}'s state after the stop is unknown ({_why(r)})")
    r = _systemctl("stop", shape.service_name)
    if r is None:
        return f"the stop command for {job_name(shape)} could not run"
    _systemctl("disable", shape.service_name)
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
    r = _run(["loginctl", "--no-ask-password", "enable-linger", user]) if user else None
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
    """held by launcher pid N | held, launcher gone (child alive) | free | unknown (why)."""
    state = lock_held(root)
    if state == "free":
        return "free"
    if state == "unknown":
        return f"unknown (the lock of {root} could not be probed)"
    rec = lock_holder(root)
    return f"held by launcher pid {rec['pid']}" if rec.get("live") else "held, launcher gone (child alive)"


def recovery_lines(shape: Shape, root: str) -> list:
    rec, lock = lock_holder(root), lock_path(root)
    who = (f"kill {rec['pid']} (the launcher pid in the lock record)" if rec.get("live") else
           f"the launcher is gone; find the child with: lsof {shlex.quote(lock or '<lock file>')}")
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

def install(shape: Shape, values: dict, root: str) -> int:
    try:
        with operation_lock(shape):
            return _install_locked(shape, values, root)
    except Busy as e:
        return _refuse(str(e))


def _install_locked(shape: Shape, values: dict, root: str) -> int:
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
    unit, problem = render_supervisor(shape)
    if problem:
        return _refuse(problem)
    state, manager = lock_held(root), manager_state(shape)
    if state == "unknown" or manager == "unknown":
        return _refuse(f"{root} is {holder_line(root)} and {job_name(shape)} is {manager}; nothing was applied")
    if state == "held" and manager != "active":
        rec = lock_holder(root)
        who = f"launcher pid {rec['pid']}" if rec.get("live") else f"launcher pid {rec.get('pid')} gone, the child alive"
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
                       f"run {shape.command} uninstall, then {shape.command} install")
    linger(shape)
    print(f"the managed launcher acquired the expected root: {root} (launcher pid {rec['pid']})")
    return 0


# ── run and debug-run ────────────────────────────────────────────────────────

def _reap(child, grace: float) -> bool:
    """terminate, wait `grace`, kill, wait REAP_BOUND_S: True once the child is reaped; False when that
    cannot be confirmed within the bound, so the caller exits without releasing."""
    if child.poll() is not None:
        _sweep(child)
        return True
    child.terminate()
    try:
        child.wait(timeout=grace)
        _sweep(child)
        return True
    except subprocess.TimeoutExpired:
        pass
    return _kill_bounded(child)


def _kill_bounded(child) -> bool:
    _kill_group(child)
    try:
        child.wait(timeout=REAP_BOUND_S)
    except subprocess.TimeoutExpired:
        return False
    _sweep(child)
    return True


def _kill_group(child) -> None:
    """SIGKILL the trampoline's whole process group, the session in it: a forced stop never reaches the
    lease holder alone. A group gone or not ours falls back to the one process."""
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        child.kill()


def _sweep(child) -> None:
    """Once the trampoline is reaped, nothing it ran outlives the lease it held."""
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _is_link(name: str, dir_fd: int) -> bool:
    """O_NOFOLLOW on a link reads ELOOP on Linux and ENOTDIR with O_DIRECTORY on macOS: ask lstat instead."""
    try:
        return stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _group_gone(child) -> bool:
    """True once no process of the trampoline's group is left within the bound: the session shares the
    lease, so the launcher may unlock only when it is gone too."""
    deadline = time.monotonic() + REAP_BOUND_S
    while True:
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:       # a member mid-teardown reads EPERM for a moment: still there
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)


def stop_state() -> dict:
    return {"child": None, "stop_at": None, "sig": None, "installed": None}


def stop_handlers(state: dict) -> dict:
    """SIGTERM and SIGINT as one explicit stop path (no KeyboardInterrupt anywhere after this): the first
    one terminates the child, if any, and starts the grace; the exit code is 128 + that signal."""
    def on_stop(signum, frame):
        state["sig"] = state["sig"] or signum
        child = state["child"]
        if child is not None and child.poll() is None and state["stop_at"] is None:
            child.terminate()
        state["stop_at"] = state["stop_at"] or time.monotonic() + TERM_GRACE_S
    before = {sig: signal.signal(sig, on_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    state["installed"] = True
    return before


def restore_handlers(state: dict, before: dict) -> None:
    for sig, handler in before.items():
        signal.signal(sig, handler)
    state["installed"] = None


def supervise(shape: Shape, argv: list, env: dict, popen=subprocess.Popen, pass_fds=(), map_exit=None,
              state=None) -> int:
    """Run the child until it ends; every launcher exit path reaps it first: SIGTERM -> 143, SIGINT ->
    130 (both through `stop_handlers`, installed here unless the caller already did), a post-spawn
    exception re-raised after the reap. `map_exit` renames a child's exit codes, and a child's own 75
    becomes 1. The spawned child is left in `supervise.child` for the lease."""
    state = state if state is not None else stop_state()
    before = stop_handlers(state) if state.get("installed") is None else None
    supervise.child = None
    child = None
    try:
        if state["stop_at"] is not None:
            print(f"{shape.name}: stopped before the session started", file=sys.stderr)
            return 128 + state["sig"]
        child = state["child"] = supervise.child = popen(argv, stdin=subprocess.PIPE, env=env, text=True,
                                                         pass_fds=pass_fds, start_new_session=True)
        if state["stop_at"] is not None:
            child.terminate()
        rc = None
        while rc is None:
            try:
                rc = child.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                if state["stop_at"] is not None and time.monotonic() > state["stop_at"]:
                    if not _kill_bounded(child):
                        print(f"{shape.name}: the child did not die within {REAP_BOUND_S:.0f} s of the kill",
                              file=sys.stderr)
                        return 128 + state["sig"]
                    rc = child.returncode
        _sweep(child)
        print(f"{shape.name}: child exited {rc}", file=sys.stderr)
        if state["stop_at"] is not None:
            if state["sig"] == signal.SIGINT:
                print(f"{shape.name}: interrupted; child reaped", file=sys.stderr)
            return 128 + state["sig"]
        if map_exit and rc in map_exit:
            print(f"{shape.name}: reported as {map_exit[rc]}", file=sys.stderr)
            return map_exit[rc]
        if rc == EXIT_HELD:
            print(f"{shape.name}: reported as {EXIT_CHILD_HELD}: {EXIT_HELD} is reserved for another holder of the "
                  "work root", file=sys.stderr)
            return EXIT_CHILD_HELD
        return rc
    except BaseException:
        if child is not None:
            _reap(child, TERM_GRACE_S)
        raise
    finally:
        if before is not None:
            restore_handlers(state, before)


def serve(shape: Shape, root: str, argv: list, env: dict, popen=subprocess.Popen, map_exit=None,
          op_lock=None) -> int:
    """The launcher: the root's lock taken and recorded before the child (an `op_lock` released right
    after), its descriptor leased to the child, the child run against `tasks` under the held root inode
    by a trampoline, and the lock released only after a confirmed reap; else the launcher exits and the
    child stays fenced under the flock."""
    state = stop_state()
    before = stop_handlers(state)
    try:
        return _serve(shape, root, argv, env, popen, map_exit, op_lock, state)
    finally:
        restore_handlers(state, before)


def _serve(shape, root, argv, env, popen, map_exit, op_lock, state) -> int:
    try:
        owner = acquire_lock(root, shape.name, retry_s=ACQUIRE_RETRY_S)
    except LockRefused as e:
        print(f"refusing to start: {root} is {holder_line(root)}" if e.code == 3 else e.line, file=sys.stderr)
        return EXIT_HELD if e.code == 3 else EXIT_UNUSABLE
    if op_lock is not None:
        op_lock.release()
    try:
        inbox_fd = os.open("tasks", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=owner.root_fd)
    except OSError as e:
        what = "tasks/ under the held root is a symlink, not a directory" if _is_link("tasks", owner.root_fd) \
            else f"no tasks/ under the held root {root} ({e.strerror or e})"
        print(f"refusing to start: {what}", file=sys.stderr)
        release_lock(owner)
        return EXIT_UNUSABLE
    try:
        lease = lease_fd(owner)
    except Exception as e:  # noqa: BLE001 - any failure to lease is a refusal, never a child without the fence
        print(f"refusing to start: {root}: {e}", file=sys.stderr)
        os.close(inbox_fd)
        release_lock(owner)
        return EXIT_UNUSABLE
    # Nothing is written to stdin: a plugin arms itself from the env. The pipe stays open, since a
    # stream-json session ends when its input ends.
    try:
        rc = supervise(shape, [sys.executable, "-c", TRAMPOLINE, str(lease), str(inbox_fd), *argv], env, popen,
                       pass_fds=(lease, inbox_fd), map_exit=map_exit, state=state)
    finally:
        os.close(inbox_fd)
        child = supervise.child
        if not (child is not None and child.poll() is not None and _group_gone(child)
                and release_after_reap(owner, child.pid)):
            print(f"{shape.name}: the child or its session was not confirmed gone; exiting without releasing "
                  f"{root}, so a survivor keeps it fenced", file=sys.stderr)
    return rc


def debug_run_problem(shape: Shape, root: str) -> str:
    """'' when a foreground session may start: no registered job and the root free; else why not."""
    manager = manager_state(shape)
    if manager != "absent":
        return (f"{job_name(shape)} is {'registered' if manager in ('active', 'registered') else 'in an unknown state'}; "
                f"run {shape.command} uninstall first, or watch its log instead")
    state = lock_held(root)
    if state != "free":
        return f"{root} is {holder_line(root)}; stop that session first"
    return ""


# ── verify and uninstall ─────────────────────────────────────────────────────

def check(results: list, ok: bool, name: str, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")


def verify_tail(shape: Shape, cfg: dict, results: list) -> None:
    """The rows every shape ends with: the relay token's mode (never its content), the marked unit, the
    manager's word, and who holds the work root."""
    relay = _dirs.device_env_path()
    try:
        mode = stat.S_IMODE(relay.stat().st_mode)
        check(results, not mode & 0o077, "relay token", f"{relay} mode {oct(mode)[2:]} (Sparrow's; never copied here)")
    except OSError:
        check(results, False, "relay token", f"{relay} missing: Sparrow has no token to poll with")
    sup = supervisor_path(shape)
    text, problem = supervisor_text(sup)
    check(results, text is not None and marked(text, supervisor_marker(shape)), "supervisor",
          problem or (f"{sup} missing; run {shape.command} install" if text is None else str(sup)))
    print(f"INFO job: {job_name(shape)} {manager_state(shape)}")
    print(f"INFO session: {work_root(cfg)} {holder_line(work_root(cfg))}")


def uninstall(shape: Shape) -> int:
    try:
        with operation_lock(shape):
            return _uninstall_locked(shape)
    except Busy as e:
        return _refuse(str(e))


def _uninstall_locked(shape: Shape) -> int:
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
    manager = manager_state(shape)
    if manager == "unknown":
        return _refuse(f"{job_name(shape)}'s state is unknown; nothing was removed", recovery_lines(shape, root) if root else ())
    if unit is None and cfg is None and manager == "absent":
        print(f"nothing to remove: {shape.name} is not installed")
        print(f"kept: {_dirs.device_env_path()} (Sparrow's token) and {shape.kept}")
        return 0
    kept = (f"{sup} and {conf} were" if unit is not None and cfg else f"{conf} was" if cfg else
            f"{sup} was" if unit is not None else "nothing was") + " kept so a rerun knows the work root"
    recovery = recovery_lines(shape, root) if root else ()
    if unit is not None or manager != "absent":
        problem = stop_supervisor(shape)
        if problem:
            return _refuse(f"{problem}; {kept}", recovery)
        print(f"stopped: {job_name(shape)}")
    if root:
        problem = _await_free(root)
        if problem:
            return _refuse(f"{problem}; {kept}", recovery)
    if unit is not None:
        sup.unlink()
        print(f"removed: {sup}")
    if not is_macos():
        r = _systemctl("daemon-reload")
        if not _ok(r):
            return _refuse(f"systemctl --user daemon-reload failed ({_why(r)}); {kept}; run uninstall again", recovery)
        if unit_load_state(shape) != "not-found":
            return _refuse(f"{shape.service_name} is still known to systemd (LoadState {unit_load_state(shape)}); {kept}; "
                           "run uninstall again", recovery)
    if cfg is not None:
        conf.unlink()
        print(f"removed: {conf}")
    print(f"kept: {_dirs.device_env_path()} (Sparrow's token) and {shape.kept}")
    return 0
