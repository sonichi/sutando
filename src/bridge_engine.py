"""Whether each running channel bridge runs from this core's engine checkout.

A bridge resolves its workspace from the checkout it was started from, so a
bridge left running from another install's engine can write tasks this core
never reads. Read-only: the probe takes each live bridge's script path from its
argv (cwd only for a relative path) and compares it with this engine directory.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

CHECK_NAME = "bridge-engine-match"
BRIDGE_SCRIPTS = {
    "telegram-bridge": "telegram-bridge.py",
    "discord-bridge": "discord-bridge.py",
    "slack-bridge": "slack-bridge.py",
}


def _script_operand(argv, script: str) -> Optional[str]:
    """The operand naming `script`: from a real argv list, or else recovered from a
    flattened command line whose path may contain spaces."""
    if isinstance(argv, (list, tuple)):
        return next((a for a in argv
                     if isinstance(a, str) and (a == script or a.endswith("/" + script))), None)
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


def _judge(name: str, script: str, pid, engine: str, argv_of, cwd_of) -> "tuple[str, str]":
    """("match" | "other" | "unknown", a finished line) for one bridge process."""
    token = _script_operand(argv_of(pid), script)
    if token is None:
        return "unknown", f"{name} pid {pid}: script path unreadable"
    path = Path(token)
    if not path.is_absolute():
        cwd = cwd_of(pid)
        if not cwd:
            return "unknown", f"{name} pid {pid}: relative script path and cwd unreadable"
        path = Path(cwd) / path
    runs_from = Path(os.path.realpath(path)).parent.parent
    if os.path.realpath(runs_from) == engine:
        return "match", f"{name} pid {pid}"
    return "other", (f"{name} pid {pid} runs from {runs_from}, not this app's engine ({engine}); "
                     "its tasks may go to another workspace. Restart it from the desktop app")


def check_bridge_engine_match(engine_dir, list_pids: Callable[[str], list],
                              argv_of: Optional[Callable] = None,
                              cwd_of: Optional[Callable] = None) -> dict:
    argv_of = argv_of or read_argv
    cwd_of = cwd_of or read_cwd
    engine = os.path.realpath(engine_dir)
    found = {"match": [], "other": [], "unknown": []}
    for name, script in BRIDGE_SCRIPTS.items():
        for pid in list_pids(script):
            try:
                kind, line = _judge(name, script, pid, engine, argv_of, cwd_of)
            except Exception as exc:  # noqa: BLE001 — one process never fails the probe
                kind, line = "unknown", f"{name} pid {pid}: unreadable ({type(exc).__name__})"
            found[kind].append(line)
    unknown = f"engine unknown: {'; '.join(found['unknown'])}" if found["unknown"] else ""
    if found["other"]:
        return {"name": CHECK_NAME, "status": "warn",
                "detail": "; ".join(found["other"]) + (f". Also {unknown}" if unknown else "")}
    if unknown:
        lead = f"{len(found['match'])} bridge(s) run from this engine; " if found["match"] else ""
        return {"name": CHECK_NAME, "status": "unknown", "detail": lead + unknown}
    if not found["match"]:
        return {"name": CHECK_NAME, "status": "ok", "detail": "no bridge running"}
    return {"name": CHECK_NAME, "status": "ok",
            "detail": f"{len(found['match'])} bridge(s) run from this engine"}
