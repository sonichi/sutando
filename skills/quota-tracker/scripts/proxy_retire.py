#!/usr/bin/env python3
"""Turn the credential-proxy launch agent off once it is verifiably unused, and back on
when the status line recorder is turned off.

Off only when ALL hold: the recorder is on; no live Claude session carries the local
proxy as ANTHROPIC_BASE_URL (process environment, or `env` in its config dir's
settings.json); nothing holds an established connection to the proxy port; and a fresh
status-line record exists for the core's account. Then `launchctl bootout` (the plist is
kept) and, if the job's own verified PID outlives that, that PID alone is stopped. Every
decision is recorded in `<workspace>/state/quota-proxy-retire.json` with its reason.
Re-enabling happens only for a job this module turned off.

Run by install-statusline.py on each session launch; `python3 proxy_retire.py [--dry-run]`
runs it by hand.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import statusline_config as cfg  # noqa: E402  (puts <repo>/src on sys.path)

LABEL = "com.sutando.credential-proxy"
PORT = 7846
RECORD = "quota-proxy-retire.json"
TIMEOUT_S = 5


def _run(argv: List[str]):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None


def service() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def job_pid(runner: Callable = _run) -> Optional[int]:
    """The PID launchd reports for the loaded job, else None (not loaded, or not running)."""
    out = runner(["launchctl", "print", service()])
    if out is None or out.returncode != 0:
        return None
    for line in (out.stdout or "").splitlines():
        key, _, val = line.strip().partition(" = ")
        if key == "pid" and val.strip().isdigit():
            return int(val.strip())
    return None


def job_loaded(runner: Callable = _run) -> bool:
    out = runner(["launchctl", "print", service()])
    return out is not None and out.returncode == 0


def sessions_on_proxy(runner: Callable = _run) -> List[str]:
    """Live Claude processes whose environment, or whose config dir's settings, point at
    the local proxy, as `pid <n>` / `settings <path>` descriptions."""
    from claude_account import core_config_dir
    from quota_availability import _env_pairs, points_at_credential_proxy
    found: List[str] = []
    dirs = {core_config_dir()}
    out = runner(["ps", "-axeww", "-o", "pid=,command="])
    for line in (out.stdout if out is not None and out.returncode == 0 else "").splitlines():
        pid, _, cmd = line.strip().partition(" ")
        argv0 = cmd.split(" ", 1)[0]
        if os.path.basename(argv0) != "claude":
            continue
        env = _env_pairs(cmd)
        if points_at_credential_proxy(env.get("ANTHROPIC_BASE_URL")):
            found.append(f"pid {pid}")
        dirs.add(env.get("CLAUDE_CONFIG_DIR"))
    for d in sorted(x for x in dirs if x):
        try:
            st = json.loads((Path(d) / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        env = st.get("env") if isinstance(st, dict) else None
        if isinstance(env, dict) and points_at_credential_proxy(env.get("ANTHROPIC_BASE_URL")):
            found.append(f"settings {Path(d) / 'settings.json'}")
    return found


def proxy_connections(runner: Callable = _run) -> Optional[List[str]]:
    """Established TCP connections on the proxy port as `command pid`; None when unknown."""
    out = runner(["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:ESTABLISHED"])
    if out is None:
        return None
    if out.returncode != 0 and not (out.stdout or "").strip():
        return []  # lsof exits 1 when nothing matches
    rows = []
    for line in (out.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            rows.append(f"{parts[0]} {parts[1]}")
    return rows


def core_record_fresh(workspace) -> bool:
    from claude_account import account_key, core_config_dir
    from quota_availability import from_statusline, read_quota_record
    rec = read_quota_record(workspace)
    return bool(rec and rec.fresh() and from_statusline(rec.payload)
                and rec.payload.get("account") == account_key(core_config_dir()))


def decide(enabled: bool, sessions: List[str], connections: Optional[List[str]], fresh: bool) -> Optional[str]:
    """None when the proxy may be turned off, else why it stays."""
    if not enabled:
        return "status line tracking is off"
    if sessions:
        return "still used by " + ", ".join(sessions)
    if connections is None:
        return "connections on the proxy port could not be read"
    if connections:
        return "open connections: " + ", ".join(sorted(set(connections)))
    if not fresh:
        return "no fresh status line record for the core's account yet"
    return None


def _record(workspace, **fields) -> None:
    from workspace_default import status_path
    path = status_path(RECORD, Path(workspace))
    prev = {}
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    cfg._write_json(path, {**(prev if isinstance(prev, dict) else {}), **fields,
                           "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})


def _read_record(workspace) -> dict:
    from workspace_default import status_path
    try:
        data = json.loads(status_path(RECORD, Path(workspace)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def retire(workspace, runner: Callable = _run, dry_run: bool = False,
           kill: Callable = os.kill, wait_s: float = 3.0) -> str:
    """Turn the launch agent off if it is verifiably unused; returns what happened."""
    if not job_loaded(runner):
        return "proxy launch agent not loaded"
    why = decide(cfg.enabled(), sessions_on_proxy(runner), proxy_connections(runner),
                 core_record_fresh(workspace))
    if why:
        if not dry_run:
            _record(workspace, status="kept", reason=why)
        return f"kept: {why}"
    if dry_run:
        return "would turn off: unused"
    pid = job_pid(runner)
    out = runner(["launchctl", "bootout", service()])
    if out is None or out.returncode != 0:
        _record(workspace, status="kept", reason="launchctl bootout failed")
        return "kept: launchctl bootout failed"
    deadline = time.monotonic() + wait_s
    while pid is not None and time.monotonic() < deadline:
        try:
            kill(pid, 0)
        except OSError:
            pid = None
            break
        time.sleep(0.1)
    if pid is not None:
        try:
            kill(pid, signal.SIGTERM)  # the job's own PID, read from launchd before bootout
        except OSError:
            pass
    reason = "unused: no session on the proxy, no open connection, fresh core status line record"
    _record(workspace, status="retired", by="quota-tracker", reason=reason)
    return f"turned off: {reason}"


def restore(workspace, runner: Callable = _run) -> str:
    """Turn the launch agent back on, only if this module turned it off."""
    if _read_record(workspace).get("status") != "retired":
        return "nothing to restore"
    if job_loaded(runner):
        _record(workspace, status="restored", reason="already loaded")
        return "already loaded"
    plist = plist_path()
    if not plist.is_file():
        return f"not restored: {plist.name} missing"
    out = runner(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
    if out is None or out.returncode != 0:
        return "not restored: launchctl bootstrap failed"
    _record(workspace, status="restored", reason="status line tracking turned off")
    return "restored"


if __name__ == "__main__":
    print(retire(cfg.workspace(), dry_run="--dry-run" in sys.argv))
