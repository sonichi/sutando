"""Whether each running channel bridge writes tasks into the inbox the core watches.

A bridge resolves its workspace from the checkout it was started from, so a
bridge still running from another install's engine writes tasks the core never
reads. The probe finds every bridge process on the host, derives its checkout
from argv (or cwd for a relative launch), resolves that checkout's inbox, and
compares it with the core's. Process readers are injected so tests run offline.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sutando_config  # noqa: E402

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


def checkout_inbox(checkout: Path) -> Path:
    # The probe's own embedder env belongs to the core's launcher, not to the bridge.
    return sutando_config.checkout_workspace(checkout) / "tasks"


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


def check_bridge_inbox_match(core_inboxes: Iterable, core_checkout: Path,
                             list_pids: Callable[[str], list],
                             argv_of: Callable[[str], object] = read_argv,
                             cwd_of: Callable[[str], str] = read_cwd,
                             inbox_of: Callable[[Path], Path] = checkout_inbox) -> dict:
    """warn naming each bridge, its inbox and the core's when they differ."""
    core = sorted({canonical(p) for p in core_inboxes if p})
    if not core:
        return {"name": CHECK_NAME, "status": "ok",
                "detail": "core inbox unknown — not comparing bridge inboxes"}
    core_root = canonical(core_checkout)
    seen, mismatched, unknown = [], [], []
    for name, script in BRIDGE_SCRIPTS.items():
        for pid in list_pids(script):
            checkout = bridge_checkout(argv_of(pid), cwd_of(pid), script)
            if checkout is None:
                unknown.append(f"{name} pid {pid}")
                continue
            if canonical(checkout) == core_root:
                seen.append(f"{name} pid {pid}")
                continue
            try:
                inbox = canonical(inbox_of(checkout))
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
    note = f"; checkout unreadable for {', '.join(unknown)}" if unknown else ""
    if not seen:
        return {"name": CHECK_NAME, "status": "ok", "detail": f"no bridge running{note}"}
    return {"name": CHECK_NAME, "status": "ok",
            "detail": f"{len(seen)} bridge(s) write the core inbox{note}"}
