"""Whether each running channel bridge writes tasks into the inbox the core watches.

A bridge resolves its workspace from the checkout it was started from, so a
bridge still running from another install's engine writes tasks the core never
reads. Each bridge REPORTS the inbox it actually uses (`report_inbox`, called
at startup) into a per-user directory that does not depend on any workspace;
health-check reads those reports for the live bridge pids and compares them
with the core's inbox. A live bridge with no report predates reporting and is
named as unknown, never guessed.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

try:
    import pwd
except ImportError:  # Windows: no passwd database
    pwd = None

CHECK_NAME = "bridge-inbox-match"
BRIDGE_SCRIPTS = {
    "telegram-bridge": "telegram-bridge.py",
    "discord-bridge": "discord-bridge.py",
    "slack-bridge": "slack-bridge.py",
}


SCHEMA_VERSION = 1
_MAX_PATH = 4096
_MAX_START = 64


def sentinel_dir() -> Path:
    """Host-local, per-uid, never synced: the same place for every install of this user.

    No workspace path can serve, because the workspace is the thing that differs
    between installs; see docs/workspace-design.md.
    """
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir) if pwd is not None else Path.home()
    return home / ".local" / "state" / "sutando" / "bridges"


def canonical(path) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def process_start(pid) -> Optional[str]:
    """The start identity of `pid`, so a record cannot validate a reused pid."""
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
        return "stat:" + stat.rsplit(")", 1)[1].split()[19]  # field 22: starttime
    except (OSError, ValueError, IndexError):
        pass
    try:
        out = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=5)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    text = " ".join(out.stdout.split())
    return f"lstart:{text}" if out.returncode == 0 and text else None


def _record_path(directory: Path, bridge: str, pid) -> Path:
    return directory / f"{bridge}-{int(pid)}.json"


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _text_ok(v, cap: int, path: bool) -> bool:
    if not (isinstance(v, str) and 0 < len(v) <= cap and "\x00" not in v):
        return False
    return os.path.isabs(v) if path else True


def schema_error(data) -> Optional[str]:
    """The first field that breaks schema v1 and its bounds, or None. Types are
    checked before any membership or comparison, so no value can raise here."""
    if not isinstance(data, dict):
        return "not an object"
    if not (_is_int(data.get("version")) and data["version"] == SCHEMA_VERSION):
        return "version"
    if not (_is_int(data.get("pid")) and data["pid"] > 0):
        return "pid"
    if not (isinstance(data.get("bridge"), str) and data["bridge"] in BRIDGE_SCRIPTS):
        return "bridge"
    if not (_is_int(data.get("started_at")) and data["started_at"] > 0):
        return "started_at"
    for key, cap, path in (("tasks_dir", _MAX_PATH, True), ("engine_dir", _MAX_PATH, True),
                           ("start", _MAX_START, False)):
        if not _text_ok(data.get(key), cap, path):
            return key
    return None


def validate(data) -> Optional[dict]:
    """The record if it matches schema v1, else None."""
    return None if schema_error(data) else data


def report_inbox(bridge: str, tasks_dir, directory: Optional[Path] = None,
                 pid: Optional[int] = None) -> Optional[Path]:
    """Record the inbox this bridge process writes. Fail-open: logs, never raises."""
    try:
        d = Path(directory) if directory is not None else sentinel_dir()
        pid = int(pid) if pid is not None else os.getpid()
        record = validate({
            "version": SCHEMA_VERSION, "pid": pid, "bridge": bridge,
            "tasks_dir": canonical(tasks_dir),
            "engine_dir": canonical(Path(__file__).resolve().parent.parent),  # lint-workspace-resolution: allow-repo-root — names the engine CODE checkout
            "start": process_start(pid) or "", "started_at": int(time.time()),
        })
        if record is None:
            raise ValueError("record outside schema (unknown bridge, path or start identity)")
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob(f"{bridge}-*.json"):
            tail = old.stem.rsplit("-", 1)[-1]
            if tail.isdigit() and int(tail) != pid and process_start(int(tail)) is None:
                old.unlink(missing_ok=True)
        target = _record_path(d, bridge, pid)
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(d))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return target
    except Exception as exc:  # noqa: BLE001 — reporting must never stop a bridge
        print(f"[bridge-inbox] could not record inbox: {type(exc).__name__}: {exc}", flush=True)
        return None


def read_report(bridge: str, pid, directory: Optional[Path] = None,
                start_of: Optional[Callable[[object], Optional[str]]] = None) -> "tuple[Optional[dict], str]":
    """(record, why) for live `pid`; record is None with a reason when it cannot be trusted."""
    start_of = start_of or process_start
    d = Path(directory) if directory is not None else sentinel_dir()
    try:
        raw = _record_path(d, bridge, pid).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "unreadable"
    try:
        data = json.loads(raw)
    except ValueError:
        return None, "malformed (not JSON)"
    bad = schema_error(data)
    if bad:
        return None, f"malformed ({bad})"
    if data["bridge"] != bridge or str(data["pid"]) != str(pid):
        return None, "malformed (names another bridge or pid)"
    live = start_of(pid)
    if live is None:
        return None, "start unreadable"
    if live != data["start"]:
        return None, "stale (pid reused)"
    return data, "ok"


def _script_operand(argv, script: str) -> Optional[str]:
    """The operand naming `script`: from a real argv list, or else recovered from a
    flattened command line whose path may contain spaces."""
    if isinstance(argv, (list, tuple)):
        return next((a for a in argv if a == script or a.endswith("/" + script)), None)
    text = str(argv or "")
    end = text.rfind(script)
    if end < 0 or (end + len(script) < len(text) and not text[end + len(script)].isspace()):
        return None
    if end > 0 and text[end - 1] not in "/ \t":
        return None
    stop = end + len(script)
    if text.rfind("/", 0, end) < 0:
        return script
    start = text.rfind(" /", 0, end)
    if start >= 0:
        return text[start + 1:stop]
    if text.startswith("/"):
        return text[:stop]
    return text[text.rfind(" ", 0, end) + 1:stop]


def bridge_engine(argv, cwd: str, script: str) -> Optional[Path]:
    """The engine checkout whose `src/<script>` this argv executes, or None."""
    token = _script_operand(argv, script)
    if token is None:
        return None
    path = Path(token)
    if not path.is_absolute():
        if not cwd:
            return None
        path = Path(cwd) / path
    return Path(canonical(path)).parent.parent


def read_argv(pid: str):
    import watcher_identity  # noqa: PLC0415
    vec = watcher_identity.proc_argv_vector(pid)
    if vec:
        return vec
    try:
        return subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read_cwd(pid: str) -> str:
    proc = Path(f"/proc/{pid}/cwd")
    if proc.exists():
        try:
            return os.readlink(proc)
        except OSError:
            return ""
    try:
        out = subprocess.run(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return next((ln[1:] for ln in out.splitlines() if ln.startswith("n")), "")


def _judge_record(name, script, pid, core, directory, start_of, argv_of, cwd_of) -> "tuple[str, str]":
    """All work on one bridge's record, message formatting included:
    ("match" | "mismatch" | "unknown", a finished line). The probe only joins lines."""
    report, why = read_report(name, pid, directory, start_of)
    if report is None:
        engine = bridge_engine(argv_of(pid), cwd_of(pid), script)
        return "unknown", (f"{name} pid {pid} from {engine or 'an unreadable engine'}: "
                           f"inbox record {why} — it may predate inbox reporting, so its "
                           "destination is unverified; restart it from the desktop engine")
    inbox = canonical(report["tasks_dir"])
    if inbox in core:
        return "match", f"{name} pid {pid}"
    return "mismatch", f"{name} pid {pid} (from {report['engine_dir']}) writes {inbox}"


def check_bridge_inbox_match(core_inboxes: Iterable,
                             list_pids: Callable[[str], list],
                             directory: Optional[Path] = None,
                             argv_of: Optional[Callable[[str], object]] = None,
                             cwd_of: Optional[Callable[[str], str]] = None,
                             start_of: Optional[Callable[[object], Optional[str]]] = None) -> dict:
    """warn naming each live bridge whose reported inbox is not the core's."""
    argv_of = argv_of or read_argv
    cwd_of = cwd_of or read_cwd
    core = sorted({canonical(p) for p in core_inboxes if p})
    if not core:
        return {"name": CHECK_NAME, "status": "ok",
                "detail": "core inbox unknown — not comparing bridge inboxes"}
    seen, mismatched, unknown = [], [], []
    verdicts = {"match": seen, "mismatch": mismatched, "unknown": unknown}
    for name, script in BRIDGE_SCRIPTS.items():
        for pid in list_pids(script):
            try:
                kind, text = _judge_record(name, script, pid, core, directory,
                                           start_of, argv_of, cwd_of)
            except Exception as exc:  # noqa: BLE001 — one record never fails the probe
                kind, text = "unknown", f"{name} pid {pid}: record invalid ({type(exc).__name__})"
            verdicts[kind].append(text)
    if mismatched:
        return {"name": CHECK_NAME, "status": "warn",
                "detail": (f"{'; '.join(mismatched)} — but the core watches "
                           f"{' / '.join(core)}, so those messages are never answered. "
                           "Stop that bridge and relaunch it from this engine "
                           "(startup.sh or the desktop app)"
                           + (f". Also inbox unknown: {'; '.join(unknown)}" if unknown else ""))}
    if unknown:
        lead = f"{len(seen)} bridge(s) write the core inbox; " if seen else ""
        return {"name": CHECK_NAME, "status": "ok",
                "detail": f"{lead}inbox unknown: {'; '.join(unknown)}"}
    if not seen:
        return {"name": CHECK_NAME, "status": "ok", "detail": "no bridge running"}
    return {"name": CHECK_NAME, "status": "ok",
            "detail": f"{len(seen)} bridge(s) write the core inbox"}
