"""Whether each running channel bridge writes tasks into the inbox the core watches.

A bridge resolves its workspace from the checkout it was started from, so a
bridge still running from another install's engine writes tasks the core never
reads. The probe finds every bridge process on the host, derives its checkout
from argv (or cwd for a relative launch), resolves that checkout's inbox with
the process's OWN environment and cwd, and compares it with the core's; an unreadable
checkout or environment is reported unknown, never guessed. Process readers are injected so tests run offline.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workspace_default  # noqa: E402

CHECK_NAME = "bridge-inbox-match"
BRIDGE_SCRIPTS = {
    "telegram-bridge": "telegram-bridge.py",
    "discord-bridge": "discord-bridge.py",
    "slack-bridge": "slack-bridge.py",
}


def canonical(path) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


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
    slash = text.rfind("/", 0, end)
    if slash < 0:
        return script
    start = text.rfind(" /", 0, end)
    if start >= 0:
        return text[start + 1:stop]
    if text.startswith("/"):
        return text[:stop]
    return text[text.rfind(" ", 0, end) + 1:stop]  # relative, no spaces


def bridge_checkout(argv, cwd: str, script: str) -> Optional[Path]:
    """The checkout whose `src/<script>` this argv executes, or None."""
    token = _script_operand(argv, script)
    if token is None:
        return None
    path = Path(token)
    if not path.is_absolute():
        if not cwd:
            return None
        path = Path(cwd) / path
    return Path(canonical(path)).parent.parent


def checkout_inbox(checkout: Path, env: dict, cwd: str) -> Path:
    """The bridges' own `resolve_workspace() / "tasks"`, as that process resolves it."""
    return workspace_default.resolve_workspace(
        env=env, module_dir=Path(checkout) / "src", cwd=Path(cwd)) / "tasks"


def read_env(pid: str) -> Optional[dict]:
    """The environment `pid` was started with, or None when it cannot be read."""
    raw = None
    try:  # linux
        raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        raw = None
    if raw is None:
        raw = _darwin_env(pid)
    if raw is None:
        return None
    env = {}
    for item in raw:
        key, sep, val = item.decode("utf8", "replace").partition("=")
        if sep and key:
            env[key] = val
    return env


def _darwin_env(pid: str) -> Optional[list]:
    """KERN_PROCARGS2 holds argc, the exec path, argv, then the environment."""
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, int(pid))
        size = ctypes.c_size_t(262144)
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        data = buf.raw[:size.value]
        argc = int.from_bytes(data[:4], sys.byteorder)
        parts = data[4:].split(b"\0")
        i = 0
        while i < len(parts) and parts[i] == b"":
            i += 1
        i += 1  # the exec path
        while i < len(parts) and parts[i] == b"":
            i += 1
        i += argc
        out = []
        while i < len(parts) and parts[i]:
            out.append(parts[i])
            i += 1
        return out
    except Exception:  # noqa: BLE001 -- not darwin, gone, or not ours
        return None


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


def check_bridge_inbox_match(core_inboxes: Iterable,
                             list_pids: Callable[[str], list],
                             argv_of: Optional[Callable[[str], object]] = None,
                             cwd_of: Optional[Callable[[str], str]] = None,
                             env_of: Optional[Callable[[str], Optional[dict]]] = None,
                             inbox_of: Optional[Callable[[Path, dict, str], Path]] = None) -> dict:
    """warn naming each bridge, its inbox and the core's when they differ."""
    argv_of = argv_of or read_argv
    cwd_of = cwd_of or read_cwd
    env_of = env_of or read_env
    inbox_of = inbox_of or checkout_inbox
    core = sorted({canonical(p) for p in core_inboxes if p})
    if not core:
        return {"name": CHECK_NAME, "status": "ok",
                "detail": "core inbox unknown — not comparing bridge inboxes"}
    seen, mismatched, unknown = [], [], []
    for name, script in BRIDGE_SCRIPTS.items():
        for pid in list_pids(script):
            cwd = cwd_of(pid)
            checkout = bridge_checkout(argv_of(pid), cwd, script)
            env = env_of(pid)
            missing = ("checkout" if checkout is None else "environment" if env is None
                       else "cwd" if not cwd else None)
            if missing:
                unknown.append(f"{name} pid {pid} ({missing} unreadable)")
                continue
            try:
                inbox = canonical(inbox_of(checkout, env, cwd))
            except Exception as exc:  # noqa: BLE001 — a foreign config must not break the check
                unknown.append(f"{name} pid {pid} ({checkout}: {type(exc).__name__})")
                continue
            if inbox in core:
                seen.append(f"{name} pid {pid}")
            else:
                mismatched.append(f"{name} pid {pid} (from {checkout}) writes {inbox}")
    if mismatched:
        return {"name": CHECK_NAME, "status": "warn",
                "detail": (f"{'; '.join(mismatched)} — but the core watches "
                           f"{' / '.join(core)}, so those messages are never answered. "
                           "Stop that bridge and relaunch it from this engine "
                           "(startup.sh or the desktop app)")}
    note = f"; inbox unknown for {', '.join(unknown)}" if unknown else ""
    if not seen and unknown:
        return {"name": CHECK_NAME, "status": "ok",
                "detail": f"inbox unknown for {', '.join(unknown)} — not asserting a match"}
    if not seen:
        return {"name": CHECK_NAME, "status": "ok", "detail": "no bridge running"}
    return {"name": CHECK_NAME, "status": "ok",
            "detail": f"{len(seen)} bridge(s) write the core inbox{note}"}
