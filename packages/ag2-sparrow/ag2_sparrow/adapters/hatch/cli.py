"""`ag2-sparrow-hatch install|uninstall|verify|sparrow|reply|keepalive|wake-context`: set up, check and answer through the Hatch hook.

install writes the hook script to ~/hooks/scripts/ (a registered hook's script is never
rewritten: a changed one goes beside it), stages the hook definition for the agent's hooks
tool (~/hooks/definitions/ is the tool's own), writes the config the script reads, where systemd runs a user
manager the ag2-sparrow user service that restarts Sparrow, and the keepalive's
install record, then leaves Sparrow running and connected; verify checks that setup;
sparrow checks only that Sparrow is running (the hook's heartbeat runs it); reply
writes a worker's answer to its result_file and waits until Sparrow has sent it;
keepalive runs the scheduled task, prints Sparrow's health row, and reports on its log;
wake-context prints the text a woken worker follows (served by the backend, else built in)."""
from __future__ import annotations

import argparse
import contextlib
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

from ... import (_dirs, agent_identity, bounded_file_read, delivery_destination, diagnostics,
                 gateway_credential_io,
                 gateway_credentials, gateway_instance, outbox, result_ready, task_hold,
                 workspace_lock)
from ...persisted_number import bounded_number
from ...delivery_core import WITHDRAWN
from ...proactive_routing import about_task_filename
from ...result_markers import parse_markers
from ...service_manager import _systemctl, _systemd_run, systemd_user_available  # noqa: F401
from .task_fields import item_id, task_fields, valid_mxid

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
HOLD_NOTICE = "Waiting for my owner's go on this."
REPLY_WAIT_S, REPLY_WAIT_MAX_S, REPLY_MAX_BYTES = 30.0, 300.0, 64 * 1024
SPARROW_COMMAND = "ag2-sparrow"
SERVICE_NAME = "ag2-sparrow.service"
# First line of every unit install writes: a unit without it is the owner's and is kept.
SERVICE_MARKER = "# Written by ag2-sparrow-hatch install; re-run install to update it."


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


# One install's masking registry, seeded on its first diff so each credential source is read once.
_INSTALL_MASK: dict = {}


def _masking_registry() -> "gateway_credentials.SecretRegistry":
    """A registry holding every credential candidate the environment Sparrow gets leads to: this
    one with AG2_DEVICE_ENV as the unit and a detached Sparrow receive it (masking only)."""
    registry = _INSTALL_MASK.get("registry")
    if registry is None:
        registry = gateway_credentials.SecretRegistry()
        gateway_credential_io.register_candidates(
            dict(os.environ, AG2_DEVICE_ENV=str(device_env_path())), registry)
        if _INSTALL_MASK.get("open"):
            _INSTALL_MASK["registry"] = registry
    return registry


def _diff(old: str, new: str, path: Path) -> str:
    """The diff of an installed file against ours, as gateway_credentials.mask_env_assignments
    shows it: no environment line's content, no credential shape, and no value registered from
    either text or from any credential candidate (masking only; the bridge decides)."""
    registry = _masking_registry()
    gateway_credentials.register_assignments(registry, old, new)
    old, new = (gateway_credentials.mask_env_assignments(t, registry) for t in (old, new))
    lines = gateway_credentials.physical_lines
    return "".join(difflib.unified_diff(lines(old), lines(new), f"{path} (installed)", f"{path} (ag2-sparrow)"))


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
                  reply_command: str = "", cron_dir: str = "", keepalive_log: str = "",
                  auto_upgrade: str = "") -> str:
    rows = (("AGENT_MXID", mxid), ("TASK_DIR", str(task_dir)), ("RESULT_DIR", str(result_dir)),
            ("ACTIVITY_FILE", str(activity_file)), ("SPARROW_PYTHON", python),
            ("STATE_DIR", str(state_dir)), ("GATEWAY_INSTANCE", gateway_instance),
            ("REPLY_COMMAND", reply_command), ("CRON_DIR", cron_dir),
            ("KEEPALIVE_LOG", keepalive_log))
    # Written only when set, so an owner's AUTO_UPGRADE=0 survives a re-install.
    rows += (("AUTO_UPGRADE", auto_upgrade),) if auto_upgrade else ()
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
    """The .env file holding REMOTE_TASK_TOKEN that Sparrow reads at startup (the shared owner's)."""
    return _dirs.device_env_path()


def _ok(r) -> bool:
    return r is not None and r.returncode == 0


def _why(r) -> str:
    if r is None:
        return "could not run"
    lines = (r.stderr or r.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit {r.returncode}"


def render_service(exec_start: str, env: dict, unset: "tuple[str, ...]" = ()) -> str:
    """`unset` names variables the user manager may pass that the unit must not: UnsetEnvironment=
    is applied last, after Environment=, so a name set here is never also in `unset`."""
    rows = "".join(f'Environment="{k}={v}"\n' if " " in v else f"Environment={k}={v}\n"
                   for k, v in env.items())
    if unset:
        rows += f"UnsetEnvironment={' '.join(unset)}\n"
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


# Every ActiveState systemd documents; anything else is an unknown state.
UNIT_STATES = frozenset(("active", "reloading", "inactive", "failed", "activating", "deactivating",
                         "maintenance", "refreshing"))
_UNIT_PROPS = ("ActiveState", "MainPID", "ExecMainStartTimestampMonotonic")
_ASCII_DIGITS = re.compile(r"[0-9]+")
# (most digits, largest value): no pid exceeds Linux PID_MAX_LIMIT, a timestamp is a u64 of microseconds.
_UNIT_NUMBERS = {"MainPID": (10, 4194304), "ExecMainStartTimestampMonotonic": (20, 2 ** 64 - 1)}


def _unit_props() -> "dict | None":
    """ActiveState (a documented state), MainPID and ExecMainStartTimestampMonotonic (ints in
    _UNIT_NUMBERS bounds), each exactly once and fully parsed here. Anything else is None."""
    r = _systemctl(("--user", "show", "ag2-sparrow.service", "--property=ActiveState",
                    "--property=MainPID", "--property=ExecMainStartTimestampMonotonic"))
    try:
        return _parse_unit_props(r.stdout) if _ok(r) else None
    except Exception:  # noqa: BLE001 - any surprise in the answer is an unknown state
        return None


def _parse_unit_props(stdout: str) -> "dict | None":
    if not isinstance(stdout, str):
        return None
    props = {}
    for line in stdout.split("\n"):
        if not line:
            continue
        key, eq, value = line.partition("=")
        if not eq or key in props or key not in _UNIT_PROPS:
            return None
        props[key] = value
    if set(props) != set(_UNIT_PROPS) or props["ActiveState"] not in UNIT_STATES:
        return None
    for name, (digits, largest) in _UNIT_NUMBERS.items():
        text = props[name]
        if len(text) > digits or not _ASCII_DIGITS.fullmatch(text) or int(text) > largest:
            return None
        props[name] = int(text)
    return props


_LOCK_MAX_BYTES = 4096


def _lock_role(instance: str) -> str:
    return f"gateway-bridge{gateway_instance.suffix(instance)}"


def _lock_file(state_dir: Path, instance: str) -> Path:
    return state_dir.parent / "state" / "locks" / f"{_lock_role(instance)}.lock"


def _lock_record(state_dir: Path, instance: str) -> "dict | None":
    """This host's Sparrow single-instance lock record, or None when there is none to read. It
    is read without following a symlink and never blocks: a FIFO, a non-regular or an oversized
    file is refused."""
    lock = _lock_file(state_dir, instance)
    text = bounded_file_read.read_regular(str(lock), _LOCK_MAX_BYTES, follow=False)[0]
    try:
        holder = json.loads(text) if text is not None else None
    except ValueError:
        return None
    ok = isinstance(holder, dict) and holder.get("host") == workspace_lock._host_label()
    return holder if ok else None


def _file_id(path: Path) -> "tuple[int, int] | None":
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def lock_snapshot(state_dir: Path, instance: str) -> "tuple[dict | None, tuple[int, int] | None]":
    """(_lock_record, the lock file's (device, inode)), the id None unless the same file was in
    place before and after the read, so the record is that file's content."""
    path = _lock_file(state_dir, instance)
    before = _file_id(path)
    record = _lock_record(state_dir, instance)
    return record, (before if before is not None and _file_id(path) == before else None)


def canonical_lock(lock: "dict | None", state_dir: Path, instance: str) -> bool:
    """`lock` is this workspace's own Sparrow lock as workspace_lock writes it: its role, this
    workspace, its schema, a valid pid, and times that are finite, not ahead of now, with the
    lock taken no later than its last heartbeat. Only such a record may authorize a signal."""
    if not isinstance(lock, dict):
        return False
    try:
        same = os.path.realpath(lock.get("workspace")) == os.path.realpath(Path(state_dir).parent)
    except (TypeError, ValueError):
        return False
    now = time.time()
    acquired = bounded_number(lock.get("acquired_at"), 1, now)
    beat = bounded_number(lock.get("heartbeat_at"), 1, now)
    return (same and lock.get("role") == _lock_role(instance)
            and lock.get("schema_version") == workspace_lock.SCHEMA_VERSION
            and outbox.valid_pid(lock.get("pid")) is not None
            and acquired is not None and beat is not None and acquired <= beat)


def _proc_stat(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return b""


def process_gone(pid: int) -> bool:
    """Whether `pid` has exited, a zombie included; False when that cannot be told. A pid outside
    `outbox.valid_pid` is gone: no process can own it."""
    if outbox.valid_pid(pid) is None:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):
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


NO_SAVED_MXID = ("NOTE sparrow: the install config holds no AGENT_MXID, so this Sparrow runs with no "
                 "agent identity and cannot resolve the owner's DM; run ag2-sparrow-hatch install "
                 "--mxid <mxid>")


def saved_mxid(value) -> str:
    """The AGENT_MXID install saved, '' when absent or not an MXID: Sparrow gets no default."""
    mxid = (value or "").strip() if isinstance(value, str) else ""
    return mxid if valid_mxid(mxid) else ""


def install_service(task_dir: Path, result_dir: Path, state_dir: Path, instance: str,
                    force: bool, health_command: str = "", reply_command: str = "",
                    mxid: str = "", may_start: bool = True) -> bool:
    """Write, enable and (unless not `may_start`) start the ag2-sparrow user service. Each
    problem prints a NOTE saying what was not done and how to finish by hand; True only when this
    install stopped the service and left it stopped, which fails the install."""
    def note(text: str) -> None:
        print(f"NOTE service: {text}")

    if not systemd_user_available():
        note("systemctl --user is not available here, so no service was set up; "
             + keepawake_note(reply_command, bool(health_command)))
        return False
    exec_start = _find_console_script(SPARROW_COMMAND)
    if not exec_start:
        note(f"{SPARROW_COMMAND} was not found beside {sys.executable} or on PATH; no service "
             "was set up. Reinstall ag2-sparrow for this Python and run install again")
        return False
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
    # The unit pins the identity to the saved MXID or none; the manager's identity keys are unset.
    env.update(agent_identity.pinned_environ(saved_mxid(mxid)))
    if not saved_mxid(mxid):
        print(NO_SAVED_MXID, file=sys.stderr)
    unset = tuple(k for k in agent_identity.ENV_KEYS if k not in env)
    if instance:
        env["GATEWAY_INSTANCE"] = instance
    try:
        exec_start = _unit_value(exec_start)
        env = {k: _unit_value(v) for k, v in env.items()}
        if health_command:
            env["REMOTE_HEALTH_COMMAND"] = " ".join(map(_unit_value, shlex.split(health_command)))
    except ValueError as e:
        note(f"{e}; no service was set up")
        return False
    unit, text = service_path(), render_service(exec_start, env, unset)
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
            return False
    stopped = False
    before = _unit_props() if changed else {}
    if before is None:
        note(f"systemctl --user show {SERVICE_NAME} did not give a well-formed state, so nothing was "
             "stopped, written, reloaded or enabled; run ag2-sparrow-hatch install again once the "
             "user manager answers")
        return False
    if before.get("ActiveState") in ("active", "reloading"):
        r = _systemctl(("--user", "stop", "ag2-sparrow.service"))
        stopped = _ok(r)
        print(f"stopped: {SERVICE_NAME} before installing" if stopped else
              f"NOTE service: stop failed ({_why(r)}); "
              + ("it is restarted after the install instead" if may_start else
                 "it keeps running the old unit (--no-start)"))
    if changed:
        _write(unit, text, 0o644)
        print(f"installed: {unit}")
        r = _systemctl(("--user", "daemon-reload"))
        if not _ok(r):
            note(f"systemctl --user daemon-reload failed ({_why(r)}); run it, then "
                 f"systemctl --user enable --now {SERVICE_NAME}")
            return False
    props = _unit_props()
    if props is None and stopped:
        print(f"error: service: this install stopped {SERVICE_NAME} and did not start it again, because "
              f"systemctl --user show {SERVICE_NAME} then did not give a well-formed state; it stays "
              f"stopped until you run: systemctl --user start {SERVICE_NAME}", file=sys.stderr)
        return True
    if props is None:
        note(f"systemctl --user show {SERVICE_NAME} did not give a well-formed state, so whether it runs "
             "is unknown; it was neither enabled, started nor restarted; run ag2-sparrow-hatch install "
             "again once the user manager answers")
        return False
    state = props["ActiveState"]
    active = state in ("active", "reloading")
    main_pid = props["MainPID"]
    ran = props["ExecMainStartTimestampMonotonic"] != 0
    # failed/activating/deactivating: the unit's own Sparrow crashed or is restarting.
    outside = not stopped and state == "inactive" and _outside_sparrow(state_dir, instance, main_pid, ran)
    has_token = device_env.is_file()
    start = may_start and has_token and not outside
    if start and state == "failed":
        _systemctl(("--user", "reset-failed", "ag2-sparrow.service"))
    r = _systemctl(("--user", "enable", "--now", "ag2-sparrow.service")) if start else \
        _systemctl(("--user", "enable", "ag2-sparrow.service"))
    if not _ok(r):
        note(f"systemctl --user enable {SERVICE_NAME} failed ({_why(r)}); fix that and run "
             f"systemctl --user enable --now {SERVICE_NAME}")
        return False
    if active and changed and not may_start:
        note(f"the unit changed but was not restarted (--no-start); run systemctl --user "
             f"restart {SERVICE_NAME} to run it")
    elif active and changed:
        r = _systemctl(("--user", "restart", "ag2-sparrow.service"))
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
    elif not may_start and not active:
        note(f"not started (--no-start); run systemctl --user start {SERVICE_NAME}")
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("linger: skipped (running as root)")
        return False
    r = _systemd_run(["loginctl", "show-user", user, "--property=Linger", "--value"]) \
        if user else None
    if _ok(r) and r.stdout.strip() == "yes":
        print(f"linger: already on for {user}")
        return False
    r = _systemd_run(["loginctl", "enable-linger", user]) if user else None
    if _ok(r):
        print(f"linger: enabled for {user}, so Sparrow runs while you are logged out")
    else:
        note(f"loginctl enable-linger {user or '$USER'} failed ({_why(r)}), so Sparrow stops "
             "when you log out and starts again at your next login. Ask an administrator to "
             f"run: loginctl enable-linger {user or '$USER'}")
    return False


def service_status(reply_command: str = "") -> "tuple[bool | None, str]":
    """(True, detail) installed and enabled; (False, detail) installed but not enabled;
    (None, detail) when there is nothing to judge (no systemd, or no unit)."""
    unit = service_path()
    if not systemd_user_available():
        return None, f"systemctl --user is not available; {keepawake_note(reply_command)}"
    if not unit.is_file():
        return None, (f"{unit} not installed; {keepawake_note(reply_command)}. Run "
                      "ag2-sparrow-hatch install to set the service up")
    enabled = _systemctl(("--user", "is-enabled", "ag2-sparrow.service"))
    if not _ok(enabled):
        return False, (f"{unit} installed but not enabled; run systemctl --user enable --now "
                       f"{SERVICE_NAME}")
    active = _ok(_systemctl(("--user", "is-active", "--quiet", "ag2-sparrow.service")))
    return True, f"{unit} installed and enabled ({'active' if active else 'not active'})"


def install(args) -> int:
    _INSTALL_MASK.clear()
    _INSTALL_MASK["open"] = True
    try:
        return _install(args)
    finally:
        _INSTALL_MASK.clear()


def _install(args) -> int:
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
                           reply_command, cron, keepalive_log,
                           _installed_config().get("AUTO_UPGRADE", ""))
    _install_file(config_path(), config, 0o600, refuse_drift=False, force=True)
    activity.parent.mkdir(parents=True, exist_ok=True)
    owner = keepalive._unit_owner() if args.no_service else None
    left_stopped, held = False, None
    if not args.no_start:
        from . import self_upgrade
        held = self_upgrade.release_hold(state_dir.resolve())
        if held:
            print(f"error: Sparrow is not started: {held}", file=sys.stderr)
    if owner == keepalive.UNIT_UNREACHABLE:
        print(f"skipped: the ag2-sparrow user service (--no-service); {service_path()} is "
              "installed but systemctl --user did not answer, so whether that service runs Sparrow "
              "is unknown; no Sparrow is stopped and no detached one is started beside it")
    elif owner is not None:
        print(f"skipped: the ag2-sparrow user service (--no-service); {service_path()} "
              "is already installed, so that service runs Sparrow and its environment is the live "
              "identity; no detached Sparrow is started beside it")
    elif args.no_service:
        print("skipped: the ag2-sparrow user service (--no-service); "
              + keepawake_note(reply_command, not args.no_keepalive))
    else:
        left_stopped = install_service(task_dir.resolve(), result_dir.resolve(), state_dir.resolve(), instance,
                                       args.force,
                                       "" if args.no_keepalive else keepalive.health_command(reply_command),
                                       reply_command, mxid=args.mxid,
                                       may_start=not (args.no_start or held))
    if args.no_keepalive:
        print("skipped: the keepalive task (--no-keepalive)")
    else:
        keepalive.install_tasks(read_config(config_path()), args.force)
    if held:
        rc = 1
        print("skipped: starting and checking Sparrow (a failed self-upgrade holds it stopped)")
    elif left_stopped:
        rc = 1
        print("skipped: starting and checking Sparrow (the service was left stopped above)")
    elif args.no_start:
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


_STATUS_TS_MAX = 1e11  # a wall-clock second no status file will carry
# A status dated further ahead than this is not Sparrow's current one, so it reads as down.
SPARROW_STATUS_FUTURE_SKEW_S = 5


def check_sparrow(state_dir: Path, now: float | None = None,
                  instance: str | None = None) -> tuple[int, str]:
    """SPARROW_OK, SPARROW_DOWN or SPARROW_NOT_CONNECTED, with a detail, judged by the
    status file Sparrow rewrites each poll."""
    path = sparrow_status_path(state_dir, instance)
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        ts = bounded_number(status["ts"], 0, _STATUS_TS_MAX)
        if ts is None:
            raise ValueError("ts is not a bounded number")
    except (OSError, ValueError, KeyError, TypeError) as e:
        return SPARROW_DOWN, (f"{path} unreadable ({type(e).__name__}); Sparrow has not run with "
                       f"this state dir; {restart_hint()}")
    delta = (time.time() if now is None else now) - ts
    if delta < -SPARROW_STATUS_FUTURE_SKEW_S:
        return SPARROW_DOWN, (f"{path} is dated {-delta:.1f}s in the future, so it is not Sparrow's "
                              f"current status; {restart_hint()}")
    age = int(delta)
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
    wrong = live_identity_mismatch(cfg, (cfg.get("GATEWAY_INSTANCE") or "").strip())
    if wrong is not None:
        _check(results, False, "identity",
               f"the running Sparrow (pid {wrong[0]}) serves {wrong[1]}, not {cfg.get('AGENT_MXID')} "
               "as the config says; run ag2-sparrow-hatch install again to replace it")

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
        r = _systemctl(("--user", "disable", "--now", "ag2-sparrow.service"))
        if not _ok(r):
            print(f"error: systemctl --user disable --now {SERVICE_NAME} failed ({_why(r)}); "
                  f"{unit} was kept", file=sys.stderr)
            return 1
        print(f"stopped and disabled: {SERVICE_NAME}")
    unit.unlink()
    print(f"removed: {unit}")
    if systemd_user_available():
        _systemctl(("--user", "daemon-reload"))
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
        # After a successful link a failed cleanup must not read as an uncommitted answer.
        with contextlib.suppress(OSError):
            Path(tmp).unlink(missing_ok=True)


def _wake_item(cfg: dict, result_file: Path) -> "tuple | None":
    """(result_dir, task_dir, tid, item, instance) for an open wake item's result_file,
    else None after printing why."""
    result_dir = Path(cfg.get("RESULT_DIR") or _dirs.result_dir())
    task_dir = Path(cfg.get("TASK_DIR") or _dirs.task_dir())
    tid = result_file.stem
    # The instance the bridge runs as, read as install recorded it (else as the bridge reads it).
    instance = (cfg["GATEWAY_INSTANCE"] if "GATEWAY_INSTANCE" in cfg
                else os.environ.get("GATEWAY_INSTANCE") or "").strip()
    item = item_id(tid, instance)
    if not (result_file.suffix == ".txt" and item is not None
            and result_file.parent.resolve() == result_dir.resolve()):
        print(f"error: {result_file} is not a result_file from the wake payload "
              f"(task-<id>.txt in {result_dir})", file=sys.stderr)
        return None
    if not (task_dir / f"{tid}.txt").is_file():
        print(f"error: no open task {tid} in {task_dir}", file=sys.stderr)
        return None
    return result_dir, task_dir, tid, item, instance


def _hold_notice(result_dir: Path, tid: str) -> Path:
    """The task's current hold line, spelt as a message about it named with its notice generation
    (0 when none can be read): the outbox withdraws it at its send unless a hold armed it."""
    try:
        record = task_hold.notice_record(result_dir, tid)
    except (OSError, ValueError):
        record = None
    return result_dir / about_task_filename(tid, (record or {}).get("gen") or 0)


def task_settled(result_dir: Path, tid: str) -> bool:
    """The task has a result, or its instance's outbox already has a record of one."""
    instance = tid[len("task-"):].split("~", 1)[0] if "~" in tid else ""
    item = item_id(tid, instance)
    root = gateway_instance.outbox_root(result_dir, instance)
    return ((result_dir / f"{tid}.txt").exists()
            or (item is not None and outbox.item_status(root, item) is not None))


# Bounded rescans for a line the drain moves mid-check (claim, hand-back, delivery).
_LINE_SCANS = 3


def _line_state(result_dir: Path, tid: str, body: str) -> str:
    """'queued' while `body` itself waits ready to send or is being sent, 'delivered' on the
    drain's receipt, 'new' when no line was ever queued, else 'unknown' (no proof)."""
    notice = _hold_notice(result_dir, tid)
    for _ in range(_LINE_SCANS):
        state = _scan_line(result_dir, notice, tid, body.strip())
        if state is not None:
            return state
    return "unknown"


def _line_items(result_dir: Path, notice: Path) -> "list[dict] | None":
    """The proactive outbox's items for the line; None when they cannot be read (no proof)."""
    try:
        return delivery_destination.lane_items(result_dir, notice.stem)
    except Exception:  # noqa: BLE001 — an unreadable outbox proves nothing
        return None


def hold_line_outcome(result_dir: Path, tid: str) -> str:
    """Where `tid`'s hold line stands, by the proactive outbox: 'delivered'; 'withdrawn' (the
    outbox withdrew it at its send, its task answered); 'undelivered' (queued, claimed, due a
    retry after a definite refusal, or parked never sent); else 'unconfirmed' (a send that left
    with no confirmed outcome, or gone with no record)."""
    notice = _hold_notice(result_dir, tid)
    items = _line_items(result_dir, notice)
    if items is None:
        return "unconfirmed"
    if any(i.get("status") == "DELIVERED" for i in items):
        return "delivered"
    if any(outbox.dispatch_unsettled(i) or i.get("reason") == "outcome-unknown"
           or i.get("prior_outcome_unknown") for i in items):
        return "unconfirmed"
    if notice.exists() or _line_claimed(result_dir, notice) or any(
            i.get("status") in ("QUEUED", "READY") for i in items):
        return "undelivered"
    parked = [str(i.get("reason") or "") for i in items if i.get("status") == "PARKED"]
    if parked and all(r.startswith(f"{WITHDRAWN}:") for r in parked):
        return "withdrawn"
    return "undelivered" if parked else "unconfirmed"


def _line_claimed(result_dir: Path, notice: Path) -> bool:
    return any(delivery_destination.claim_base(p.name) == notice.stem
               for p in result_dir.glob(f"{notice.stem}.sending.*"))


def _scan_line(result_dir: Path, notice: Path, tid: str, want: str) -> "str | None":
    """One pass in the order the drain moves a line (queued, claimed, delivered); None when
    a file vanished under it and nothing later explains where it went."""
    moved = seen = False

    def matches(path: Path) -> bool:
        nonlocal moved, seen
        # The drain's own readiness predicate: an empty or partial file is not a line.
        text = result_ready.read_ready_result(path)
        if text is None and not path.exists():
            moved = True
        else:
            seen = True
        return text == want

    if notice.exists() and matches(notice):
        return "queued"
    claims = sorted(p for p in result_dir.glob(f"{notice.stem}.sending.*")
                    if delivery_destination.claim_base(p.name) == notice.stem)
    if any(matches(p) for p in claims):
        return "queued"
    if seen and not moved:
        return "unknown"  # some other or unready file holds the line's name
    items = _line_items(result_dir, notice)
    if items is None:
        return "unknown"
    if items:
        return "delivered" if any(i.get("status") == "DELIVERED" for i in items) else "unknown"
    if moved:
        return None
    return "unknown" if task_hold.notice_marked(result_dir, tid) else "new"


def _lane_snapshot(cfg: dict, instance: str) -> "tuple[int, str, bool] | None":
    """One read of the live drain's lane: (pid, homeserver, declares the send-lock protocol hold
    and reply rely on), or None when there is no such drain: no fresh record, its publisher gone,
    the lock names another live process, or the record is not that process's own (another one
    wrote it under a reused pid). A drain from an older package lacks the protocol, and
    keepawake replaces it."""
    rec = _live_lane_record(cfg, instance)
    if rec is None:
        return None
    return (rec["pid"], delivery_destination.homeserver_of(rec["agent_id"]),
            rec.get("send_lock") == delivery_destination.SEND_LOCK_PROTOCOL)


def _live_lane_record(cfg: dict, instance: str) -> "dict | None":
    """The live drain's own fresh lane record, under the conditions _lane_snapshot names; else None."""
    state = Path(cfg.get("STATE_DIR") or _dirs.state_dir())
    rec = agent_identity.published_lane_record(state, instance)
    if rec is None or process_gone(rec["pid"]):
        return None
    holder = _lock_record(state, instance)
    if holder is not None and live_lock_holder(holder) != rec["pid"]:
        return None  # a lock names the live drain, and it is not the publisher
    if not agent_identity.owned_by(rec, rec["pid"], (holder or {}).get("acquired_at")):
        return None
    return rec


def live_identity_mismatch(cfg: dict, instance: str) -> "tuple[int, str] | None":
    """(pid, agent_id) of the live drain when the id it serves is not the AGENT_MXID install saved;
    None when they match, or when there is no saved MXID or no live drain record to compare."""
    want = saved_mxid(cfg.get("AGENT_MXID"))
    rec = _live_lane_record(cfg, instance) if want else None
    if rec is None or rec["agent_id"].lower() == want.lower():
        return None
    return rec["pid"], rec["agent_id"]


def _lane_homeserver(cfg: dict, instance: str) -> str:
    """The homeserver the live drain of `instance` published; "" when there is none, it is another
    instance's, or its publisher is not that live drain, which `hold` refuses rather than guess."""
    snap = _lane_snapshot(cfg, instance)
    return snap[1] if snap else ""


def _held_line(cfg: dict, instance: str) -> str:
    """The served held line when the setup cache wake-context reads has a usable one, else
    HOLD_NOTICE."""
    from . import runtime_text
    _register_device_secrets()
    state = Path(cfg.get("STATE_DIR") or _dirs.state_dir())
    return runtime_text.held_line(runtime_text.current_cache(state, instance)) or HOLD_NOTICE


def hold(args) -> int:
    """Park one wake item for the owner's go: queue its held line once, then mark it held."""
    cfg = _installed_config()
    found = _wake_item(cfg, Path(args.result_file).expanduser())
    if found is None:
        return 2
    result_dir, task_dir, tid, _item, instance = found
    try:
        task_hold.prune(result_dir, task_dir)
        held = task_hold.read(result_dir, tid)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if task_settled(result_dir, tid):
        print(f"NOT held: {tid}.txt already has a reply or result", file=sys.stderr)
        return REPLY_NOT_POSTED
    if held is not None:
        print(f"held: {tid} was already held; nothing posted again")
        return REPLY_POSTED
    fields = task_fields(task_dir / f"{tid}.txt", result_dir, instance) or {}
    room, sender = fields.get("room_id") or "", fields.get("sender") or ""
    lines = [f"[channel: {room}]"]
    if fields.get("thread_root"):
        lines.append(f"[thread: {fields['thread_root']}]")
    lines.append((f"{sender} " if valid_mxid(sender) else "") + _held_line(cfg, instance))
    body = "\n".join(lines) + "\n"
    snap = _lane_snapshot(cfg, instance)
    lane = snap[1] if snap else ""
    if not lane:
        print(f"NOT confirmed: can't confirm the room is deliverable yet, as no live Sparrow drain "
              f"has published its lane identity; no hold recorded and {tid} still reads "
              "task-not-picked-up", file=sys.stderr)
        return REPLY_UNCONFIRMED
    if not room or delivery_destination.drain_room(body, lane) != room:
        print(f"error: task {tid} names no AG2 Space room Sparrow would deliver to",
              file=sys.stderr)
        return 2
    if not snap[2]:
        print(f"NOT confirmed: the running Sparrow predates the hold-line send lock, so a hold line "
              f"could not be withdrawn once answered; no hold recorded and {tid} still reads "
              f"task-not-picked-up. {restart_hint()}", file=sys.stderr)
        return REPLY_UNCONFIRMED
    try:
        with delivery_destination.send_lock(result_dir):
            return _hold_under_lock(result_dir, tid, body,
                                    lambda: _lane_snapshot(cfg, instance) == snap)
    except delivery_destination.SendLockUnavailable as e:
        print(f"NOT confirmed: the hold-line send lock was not taken ({diagnostics.describe(e)}); "
              f"no hold recorded and {tid} still reads task-not-picked-up. Run hold again",
              file=sys.stderr)
        return REPLY_UNCONFIRMED


def _hold_under_lock(result_dir: Path, tid: str, body: str, lane_unchanged) -> int:
    """hold's commit, run under the send lock that reply's commit and the drain's send also take:
    no answer can land between its settled check and the line's publication, and no drain sends
    the line before the commit ends. `lane_unchanged()` re-reads the drain the checks before the
    lock were made against, and a commit needs it still true. The drain sends only the generation
    a hold armed, so a refused commit leaves a line no drain sends; the next hold queues a new one."""
    try:
        if task_settled(result_dir, tid):
            print(f"NOT held: {tid}.txt already has a reply or result", file=sys.stderr)
            return REPLY_NOT_POSTED
        if task_hold.read(result_dir, tid) is not None:
            print(f"held: {tid} was already held; nothing posted again")
            return REPLY_POSTED
        record = task_hold.notice_record(result_dir, tid)
        if record is not None and record["gen"] is not None and record["armed"] \
                and hold_line_outcome(result_dir, tid) == "withdrawn":
            # Armed by a hold whose marker never landed, and withdrawn for it: the next line replaces it.
            task_hold.disarm_notice(result_dir, tid, record["token"])
            record = {**record, "armed": False}
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    unarmed = record is not None and record["gen"] is not None and not record["armed"]
    try:
        line = "new" if unarmed else _line_state(result_dir, tid, body)
        if line == "unknown":
            print(f"NOT confirmed: the hold line for {tid} was queued before and nothing proves "
                  "it was delivered; it is not queued again and the task still reads "
                  "task-not-picked-up", file=sys.stderr)
            return REPLY_UNCONFIRMED
        if not lane_unchanged():
            return _drain_changed(tid)
        owner = None
        if unarmed:
            owner = task_hold.advance_notice(result_dir, tid, record["token"])[0]
        elif line == "new":
            owner = task_hold.mark_notice(result_dir, tid)
        if owner is not None:
            _publish_once(_hold_notice(result_dir, tid), body)
        # A hold stands only on a line proven queued or delivered, whoever queued it.
        if _line_state(result_dir, tid, body) not in ("queued", "delivered"):
            print(f"NOT confirmed: the hold line for {tid} is not proven queued; the task "
                  "still reads task-not-picked-up", file=sys.stderr)
            return REPLY_UNCONFIRMED
        if not lane_unchanged():
            return _drain_changed(tid, "; its line is never sent, since no hold armed it")
        if owner is not None:
            task_hold.arm_notice(result_dir, tid, owner)
        token = task_hold.place(result_dir, tid, task_hold.OWNER_GO)
    except (OSError, ValueError) as e:
        print(f"NOT confirmed: the hold was not recorded ({e}); the task still reads "
              "task-not-picked-up. Run hold again; its line is sent only once a hold stands",
              file=sys.stderr)
        return REPLY_UNCONFIRMED
    if token is None:
        if task_hold.read(result_dir, tid) is not None:
            print(f"held: {tid} was already held; nothing posted again")
            return REPLY_POSTED
        print(f"NOT confirmed: an unreadable hold marker for {tid} is in the way; the task "
              "still reads task-not-picked-up", file=sys.stderr)
        return REPLY_UNCONFIRMED
    if task_settled(result_dir, tid):
        _note_withdrawal(tid, _line_report(result_dir, tid))
        _release_quietly(result_dir, tid, token)
        print(f"NOT held: {tid} was answered meanwhile", file=sys.stderr)
        return REPLY_NOT_POSTED
    print(f"held: {tid} marked waiting for your owner's go; Sparrow posts the one notice")
    return REPLY_POSTED


def _drain_changed(tid: str, line_note: str = "") -> int:
    print(f"NOT confirmed: the Sparrow drain changed while hold ran{line_note}; no hold recorded "
          f"and {tid} still reads task-not-picked-up. Run hold again", file=sys.stderr)
    return REPLY_UNCONFIRMED


# What reply reports of the hold line once its answer is queued; "none" says nothing.
WITHDRAW_NOTES = {
    "withdrawn": "the hold line of {tid} will not post: the drain withdraws it now that the task "
                 "is answered",
    "posted": "the hold line of {tid} was already posted before this answer",
    "unsent": "the hold line of {tid} was parked unsent; it will not post",
    "unconfirmed": "the hold line of {tid} left the queue before this answer with an unconfirmed "
                   "outcome (the room may already show it); it will not be sent again",
}


def _line_report(result_dir: Path, tid: str) -> str:
    """Where `tid`'s hold line stands for reply's report, read under the send lock once the answer
    is queued: no send of it can start or be under way, so the outbox's state is its outcome."""
    if not task_hold.notice_marked(result_dir, tid):
        return "none"
    outcome = hold_line_outcome(result_dir, tid)
    if outcome == "delivered":
        return "posted"
    if outcome == "unconfirmed":
        return "unconfirmed"
    notice = _hold_notice(result_dir, tid)
    pending = notice.exists() or _line_claimed(result_dir, notice) or any(
        i.get("status") in ("QUEUED", "READY") for i in _line_items(result_dir, notice) or [])
    return "withdrawn" if outcome == "withdrawn" or pending else "unsent"


def _commit_answer_locked(result_dir: Path, task_dir: Path, tid: str, root: Path, item: str,
                          result_file: Path, body: str) -> "tuple[bool, str]":
    """reply's commit under the send lock: claim the task and queue the answer, which withdraws
    its hold line at the outbox's dispatch-time recheck. A failed publication releases the claim
    and raises OSError, so a retry finds the task, and its line, as they were."""
    _prune_claims(result_dir, task_dir)
    if not _claim(result_dir, tid):
        return False, "none"
    if outbox.item_status(root, item) is not None:
        return False, "none"
    try:
        committed = _publish_once(result_file, body)
    except BaseException:
        with contextlib.suppress(OSError):
            (_claims_dir(result_dir) / tid).unlink(missing_ok=True)
        raise
    # False: another result holds the name, so the task is answered either way.
    return committed, _line_report(result_dir, tid)


def _note_withdrawal(tid: str, where: str) -> None:
    if where in WITHDRAW_NOTES:
        print("note: " + WITHDRAW_NOTES[where].format(tid=tid), file=sys.stderr)


def _release_quietly(result_dir: Path, tid: str, token: "str | None" = None) -> None:
    """Release a hold once the task is answered, never failing the command over it."""
    try:
        task_hold.release(result_dir, tid, token)
    except (OSError, ValueError) as e:
        # A hold left behind is harmless: health ignores holds on answered tasks.
        print(f"note: could not release the hold on {tid} ({e}); it reads as answered",
              file=sys.stderr)


def reply(args) -> int:
    cfg = _installed_config()
    result_file = Path(args.result_file).expanduser()
    found = _wake_item(cfg, result_file)
    if found is None:
        return 2
    result_dir, task_dir, tid, item, instance = found
    if not (math.isfinite(args.wait) and 0 <= args.wait <= REPLY_WAIT_MAX_S):
        print(f"error: --wait must be 0 to {REPLY_WAIT_MAX_S:g} seconds", file=sys.stderr)
        return 2
    body, why = _read_body(args)
    if body is None:
        print(f"error: {why}", file=sys.stderr)
        return 2
    root = gateway_instance.outbox_root(result_dir, instance)
    # One transaction with hold's commit and the drain's send: the answer is queued under the send
    # lock, so the hold line is sent before it or withdrawn at its send, never after it.
    try:
        with delivery_destination.send_lock(result_dir):
            committed, where = _commit_answer_locked(result_dir, task_dir, tid, root, item,
                                                     result_file, body)
    except OSError as e:  # SendLockUnavailable included: every step taken was undone
        print(f"error: the answer was not committed ({diagnostics.describe(e)}); nothing "
              "written and its hold line left as it was, run reply again", file=sys.stderr)
        return 2
    _note_withdrawal(tid, where)
    if not committed:
        print(f"NOT posted: {result_file.name} already has a reply or result; nothing written",
              file=sys.stderr)
        return REPLY_NOT_POSTED
    _release_quietly(result_dir, tid)  # the answer ends any hold
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


def _register_device_secrets() -> None:
    """Withhold every credential candidate this host could hand Sparrow (the process env and the
    device file, each on its own): whichever one precedence picks, the others stay withheld."""
    gateway_credential_io.register_candidates(
        dict(os.environ, AG2_DEVICE_ENV=str(device_env_path())), diagnostics.REGISTRY)


def wake_context(_args) -> int:
    from . import runtime_text
    _register_device_secrets()
    cfg = _installed_config()
    values = {"<AGENT_MXID>": cfg.get("AGENT_MXID") or "<AGENT_MXID>",
              "<REPLY_COMMAND>": cfg.get("REPLY_COMMAND") or find_reply_command()
              or REPLY_COMMAND,
              "<ACTIVITY_FILE>": cfg.get("ACTIVITY_FILE")
              or str(_home() / ".muse" / "last_active")}
    state = Path(cfg.get("STATE_DIR") or _dirs.state_dir())
    instance = (cfg["GATEWAY_INSTANCE"] if "GATEWAY_INSTANCE" in cfg
                else os.environ.get("GATEWAY_INSTANCE") or "").strip()
    text, source = runtime_text.wake_context(
        runtime_text.current_cache(state, instance), values,
        runtime_text.owner_dm_room(state, instance, cfg.get("AGENT_MXID") or ""))
    print(text, end="")
    print(f"wake-context: {source}", file=sys.stderr)
    return 0


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
    h = sub.add_parser("hold", help="park one wake item for your owner's go: mark it held and "
                       "post one line saying so in its room, once")
    h.add_argument("result_file", help="the item's result_file from the wake payload")
    h.set_defaults(func=hold)
    w = sub.add_parser("wake-context", help="print the prompt and worker note a woken worker "
                       "follows (the backend's served copy when valid, else the built-in one)")
    w.set_defaults(func=wake_context)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
