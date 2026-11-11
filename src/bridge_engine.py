"""Whether each running channel bridge runs from this core's engine checkout.

A bridge resolves its workspace from the checkout it was started from, so a
bridge left running from another install's engine can write tasks this core
never reads. Read-only: the probe takes each live bridge's script path from its
argv (cwd only for a relative path) and compares it with this engine directory.
"""
from __future__ import annotations

import os
import re
import subprocess
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
    return re.fullmatch(r"[Pp]ython(\d+(\.\d+)*)?[tw]?", os.path.basename(token)) is not None


def _first_operand(tokens: "list[str]") -> Optional[str]:
    """The script operand of `<interpreter> [options] <script> ...`, or None."""
    if not tokens or not _is_interpreter(tokens[0]):
        return None
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


def _script_operand(argv, script: str) -> Optional[str]:
    """The interpreter's script operand when its basename is `script`, else None.

    Prefers the real argv vector. A flattened command line cannot split a path
    with spaces, so there the interpreter and script are located by name."""
    if isinstance(argv, (list, tuple)):
        if not all(isinstance(a, str) for a in argv):
            return None
        op = _first_operand(list(argv))
    else:
        text = str(argv or "")
        interp = re.search(r"[Pp]ython(\d+(\.\d+)*)?[tw]?(?=\s)", text)
        if not interp:
            return None
        rest = text[interp.end():]
        op = None
        tokens = rest.split()
        i = _skip_options(tokens, 0)
        if i is not None and i < len(tokens):
            # Rejoin from the first operand: it runs to the first ".py" followed by a boundary.
            tail = " ".join(tokens[i:])
            m = re.match(r"(.+?\.py)(?=\s|$)", tail)
            op = m.group(1) if m else None
    if op is None or os.path.basename(op) != script:
        return None
    return op


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


def check_bridge_engine_match(engine_dir, list_pids: Callable[[str], "tuple[list, bool]"],
                              argv_of: Optional[Callable] = None,
                              cwd_of: Optional[Callable] = None) -> dict:
    """`list_pids(script)` returns (pids, scanned); a failed scan is unknown, not absence."""
    argv_of = argv_of or read_argv
    cwd_of = cwd_of or read_cwd
    engine = os.path.realpath(engine_dir)
    found = {"match": [], "other": [], "unknown": []}
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
