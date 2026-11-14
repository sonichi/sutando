"""Whether each running channel bridge runs from this core's engine checkout.

A bridge resolves its workspace from the checkout it was started from, so a
bridge left running from another install's engine can write tasks this core
never reads. Read-only: the probe takes each live bridge's script path from its
real argv vector, as the Python interpreter's first script operand (trailing
arguments allowed; cwd only for a relative path), and compares it with this
engine directory. A non-Python process naming the script (an editor, a pager) is
ignored; a process whose argv vector cannot be read is unknown.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

CHECK_NAME = "bridge-engine-match"
BRIDGE_SCRIPTS = {
    "telegram-bridge": "telegram-bridge.py",
    "discord-bridge": "discord-bridge.py",
    "slack-bridge": "slack-bridge.py",
}


_VALUE_FLAGS = "XWQ"  # a short flag whose value is attached or is the next token
_MODE_FLAGS = "cm"  # the interpreter runs a string or a module, not a file


def _option(tok: str) -> str:
    """"mode" (-c/-m, attached or combined), "takes-next" or "flag" for a
    single-dash option token."""
    for i, ch in enumerate(tok[1:], start=1):
        if ch in _MODE_FLAGS:
            return "mode"
        if ch in _VALUE_FLAGS:
            return "flag" if i + 1 < len(tok) else "takes-next"
    return "flag"


def _is_interpreter(token: str) -> bool:
    return re.fullmatch(r"[Pp]ython(3(\.\d+)?t?d?)?", os.path.basename(token)) is not None


def _first_operand(tokens: "list[str]") -> Optional[str]:
    """The script operand of `<interpreter> [options] <script> ...`, or None."""
    i = _skip_options(tokens, 1)
    if i is None or i >= len(tokens):
        return None
    return tokens[i]


def _skip_options(tokens: "list[str]", i: int) -> Optional[int]:
    """Index of the first operand at or after `i`, or None for an execution mode."""
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-":
            return None
        if tok == "--":
            return i + 1
        if not tok.startswith("-"):
            return i
        kind = _option(tok)
        if kind == "mode":
            return None
        i += 2 if kind == "takes-next" else 1
    return i


def _argv_vector(pid):
    import watcher_identity  # noqa: PLC0415
    return watcher_identity.proc_argv_vector(pid)


def read_argv(pid: str) -> Optional["list[str]"]:
    """The real argv vector, or None: a flattened `ps` line is never parsed."""
    return _argv_vector(pid) or None


def read_cwd(pid: str, proc_root: str = "/proc") -> str:
    proc = Path(proc_root) / str(pid) / "cwd"
    if os.path.lexists(proc):
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


def _same_dir(a: str, b: str) -> bool:
    """Two spellings of one folder: equal, or a case variant on a case-insensitive volume."""
    if os.path.normcase(a) == os.path.normcase(b):
        return True
    if a.casefold() != b.casefold():
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _judge(name: str, script: str, pid, engine: str, argv_of, cwd_of) -> "tuple[str, str]":
    """("match" | "other" | "unknown" | "ignored", a finished line) for one process."""
    argv = argv_of(pid)
    if not isinstance(argv, (list, tuple)) or not argv or \
            not all(isinstance(a, str) for a in argv):
        return "unknown", (f"{name} pid {pid}: argv vector unreadable "
                           "(a flattened ps command line is ambiguous, so it is not parsed)")
    if not _is_interpreter(argv[0]):
        return "ignored", ""
    token = _first_operand(list(argv))
    if token is None or os.path.basename(token) != script:
        return "unknown", (f"{name} pid {pid}: the interpreter's first script operand "
                           f"is not {script}")
    path = Path(token)
    if not path.is_absolute():
        cwd = cwd_of(pid)
        if not cwd:
            return "unknown", f"{name} pid {pid}: relative script path and cwd unreadable"
        path = Path(cwd) / path
    runs_from = Path(os.path.realpath(path)).parent.parent
    if _same_dir(os.path.realpath(runs_from), engine):
        return "match", f"{name} pid {pid}"
    return "other", (f"{name} pid {pid} runs from {runs_from}, not this app's engine ({engine}); "
                     "its tasks may go to another workspace. Restart it from the desktop app")


def check_bridge_engine_match(engine_dir, list_pids: Callable[[str], "tuple[list, bool]"],
                              argv_of: Optional[Callable] = None,
                              cwd_of: Optional[Callable] = None,
                              platform: Optional[str] = None) -> dict:
    """`list_pids(script)` returns (pids, scanned); a failed scan is unknown, not absence."""
    if (platform or sys.platform).startswith("win"):
        return {"name": CHECK_NAME, "status": "unknown",
                "detail": "not checked on Windows: there is no reader here for a process's "
                          "argv vector or working directory"}
    argv_of = argv_of or read_argv
    cwd_of = cwd_of or read_cwd
    engine = os.path.realpath(engine_dir)
    found = {"match": [], "other": [], "unknown": [], "ignored": []}
    for name, script in BRIDGE_SCRIPTS.items():
        pids, scanned = list_pids(script)
        if not scanned:
            found["unknown"].append(f"{name}: process scan failed")
        for pid in pids:
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
