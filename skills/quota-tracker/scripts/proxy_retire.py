#!/usr/bin/env python3
"""Turn the credential-proxy launch agent off once it is verifiably unused, and back on
when the status line recorder is turned off.

Off only on complete positive evidence: the recorder is on; every process this user owns was
enumerated (no diagnostics, no ambiguous row) and none carries the local proxy as
ANTHROPIC_BASE_URL, nor does `env` in the settings a live Claude process runs under (its config
dir's, its project's); a clean read of the proxy port shows no established connection; and a
fresh status-line record exists for the core's account. Anything unread or ambiguous keeps it.

The job is per user, so the intent is too: `intent_path()` records `retiring` before
`launchctl bootout` (the plist is kept) and `retired` once the port stops listening. Nothing is
signalled by PID; a run that finds `retiring` reconciles it against the job and the port. Each
decision is also logged in `<workspace>/state/quota-proxy-retire.json` with its reason.
With tracking off, `ensure_running` brings the agent up (bootstrap, or install if never
installed) unless the owner set CREDENTIAL_PROXY_ENABLED off.

Run by install-statusline.py on each session launch; `python3 proxy_retire.py [--dry-run]`
runs it by hand.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import statusline_config as cfg  # noqa: E402  (puts <repo>/src on sys.path)

LABEL = "com.sutando.credential-proxy"
PORT = 7846
RECORD = "quota-proxy-retire.json"
TIMEOUT_S = 5
INSTALL_TIMEOUT_S = 30


def _run(argv: List[str]):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None


def service() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def intent_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "Sutando" / "credential-proxy-retire.json"


def read_intent() -> dict:
    try:
        data = json.loads(intent_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_intent(status: str, **fields) -> bool:
    try:
        cfg._write_json(intent_path(), {"status": status, "by": "quota-tracker", **fields,
                                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    except OSError:
        return False
    return True


def _diagnostics(out) -> bool:
    return bool((getattr(out, "stderr", "") or "").strip())


def listening(runner: Callable = _run) -> Optional[bool]:
    """Whether the proxy port has a listener; None when that cannot be read cleanly."""
    out = runner(["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN"])
    if out is None or _diagnostics(out):
        return None
    if out.returncode == 0:
        return True
    return False if not (out.stdout or "").strip() else None


def job_loaded(runner: Callable = _run) -> bool:
    out = runner(["launchctl", "print", service()])
    return out is not None and out.returncode == 0


def _settings_points_at_proxy(path: Path, unknown: List[str]) -> bool:
    """True when this settings file sets the proxy under `env`; an unreadable one is unknown."""
    from quota_availability import points_at_credential_proxy
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        unknown.append(f"unreadable {path}")
        return False
    env = st.get("env") if isinstance(st, dict) else None
    return isinstance(env, dict) and points_at_credential_proxy(env.get("ANTHROPIC_BASE_URL"))


def _cwd(pid: str, runner: Callable) -> Optional[str]:
    out = runner(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"])
    if out is None or out.returncode != 0:
        return None
    for line in (out.stdout or "").splitlines():
        if line.startswith("n/"):
            return line[1:]
    return None


def sessions_on_proxy(runner: Callable = _run) -> Tuple[List[str], List[str]]:
    """(uses, unknown): this user's processes whose environment points at the local proxy, and
    settings live Claude processes run under (config dir, project) that do; plus whatever could
    not be verified (a process list or environment, a working folder, a settings file), which keeps it."""
    from claude_account import core_config_dir
    from quota_availability import _env_pairs, points_at_credential_proxy
    found: List[str] = []
    unknown: List[str] = []
    files = set()
    core = core_config_dir()
    if core:
        files.add(Path(core) / "settings.json")
    # Every process this user owns, not only `claude`: an SDK or node client reaches the proxy too.
    out = runner(["ps", "-xeww", "-U", str(os.getuid()), "-o", "pid=,command="])
    if out is None or out.returncode != 0:
        return found, ["process list unreadable"]
    if _diagnostics(out):
        return found, ["process list returned diagnostics"]
    if not (out.stdout or "").strip():
        return found, ["process list empty"]
    for line in (out.stdout or "").splitlines():
        if not line.strip():
            continue
        pid, _, cmd = line.strip().partition(" ")
        cmd = cmd.strip()
        if not pid.isdigit() or not cmd:
            unknown.append(f"ambiguous process row {line.strip()[:60]!r}")
            continue
        name = os.path.basename(cmd.split(" ", 1)[0])
        env = _env_pairs(cmd)
        if not env:
            unknown.append(f"pid {pid} {name} (environment unreadable)")
            continue
        if points_at_credential_proxy(env.get("ANTHROPIC_BASE_URL")):
            found.append(f"pid {pid} {name}")
        if name != "claude":
            continue
        ccd = env.get("CLAUDE_CONFIG_DIR")
        files.add((Path(ccd) if ccd else Path.home() / ".claude") / "settings.json")
        cwd = _cwd(pid, runner)
        if cwd is None:
            unknown.append(f"pid {pid} (working folder unreadable)")
            continue
        files.update(Path(cwd) / ".claude" / n for n in ("settings.json", "settings.local.json"))
    for path in sorted(files):
        if _settings_points_at_proxy(path, unknown):
            found.append(f"settings {path}")
    return found, unknown


def proxy_connections(runner: Callable = _run) -> Optional[List[str]]:
    """Established TCP connections on the proxy port as `command pid`; None when not cleanly read."""
    out = runner(["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:ESTABLISHED"])
    if out is None or _diagnostics(out):
        return None
    lines = (out.stdout or "").splitlines()
    if out.returncode != 0:
        return [] if not lines else None  # lsof exits 1, silently, when nothing matches
    if not lines or not lines[0].startswith("COMMAND"):
        return None
    rows = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return None
        rows.append(f"{parts[0]} {parts[1]}")
    return rows


def core_record_fresh(workspace) -> bool:
    from quota_availability import from_statusline, read_quota_record
    rec = read_quota_record(workspace)
    return bool(rec and rec.fresh() and from_statusline(rec.payload))


def decide(enabled: bool, sessions: List[str], connections: Optional[List[str]], fresh: bool,
           unknown: Tuple[str, ...] = ()) -> Optional[str]:
    """None when the proxy may be turned off, else why it stays. Anything unverified keeps it."""
    if not enabled:
        return "status line tracking is off"
    if sessions:
        return "still used by " + ", ".join(sessions)
    if unknown:
        return "could not verify every session: " + ", ".join(unknown)
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


def reconcile(workspace, runner: Callable = _run) -> Optional[str]:
    """Settle a `retiring` intent left by a run that died or could not confirm; None if none."""
    if read_intent().get("status") != "retiring":
        return None
    if job_loaded(runner):
        write_intent("kept", reason="the job is loaded again; the bootout did not take")
        return "kept"
    if listening(runner) is False:
        write_intent("retired")
        _record(workspace, status="retired", by="quota-tracker", reason="confirmed: the port stopped listening")
        return "retired"
    return "unconfirmed"


def retire(workspace, runner: Callable = _run, dry_run: bool = False, wait_s: float = 3.0,
           sleep: Callable = time.sleep) -> str:
    """Turn the launch agent off if it is verifiably unused; returns what happened."""
    settled = None if dry_run else reconcile(workspace, runner)
    if not job_loaded(runner):
        if settled == "unconfirmed":
            return "turned off, unconfirmed: the proxy port is still served"
        return "proxy launch agent not loaded"
    uses, unknown = sessions_on_proxy(runner)
    why = decide(cfg.enabled(), uses, proxy_connections(runner), core_record_fresh(workspace), tuple(unknown))
    if why:
        if not dry_run:
            _record(workspace, status="kept", reason=why)
        return f"kept: {why}"
    if dry_run:
        return "would turn off: unused"
    if not write_intent("retiring"):
        _record(workspace, status="kept", reason="could not record the retirement intent")
        return "kept: could not record the retirement intent"
    out = runner(["launchctl", "bootout", service()])
    if out is None or out.returncode != 0:
        write_intent("kept", reason="launchctl bootout failed")
        _record(workspace, status="kept", reason="launchctl bootout failed")
        return "kept: launchctl bootout failed"
    deadline = time.monotonic() + wait_s
    while listening(runner) is not False:
        if time.monotonic() >= deadline:
            reason = "booted out, but the proxy port is still served; left unconfirmed"
            _record(workspace, status="unconfirmed", by="quota-tracker", reason=reason)
            return f"turned off, unconfirmed: {reason}"
        sleep(0.1)
    reason = "unused: no session on the proxy, no open connection, fresh core status line record"
    write_intent("retired", reason=reason)
    _record(workspace, status="retired", by="quota-tracker", reason=reason)
    return f"turned off: {reason}"


def _install() -> bool:
    script = cfg.REPO / "src" / "install-credential-proxy-launchd.sh"
    try:
        out = subprocess.run(["bash", str(script), "install"], capture_output=True, text=True,
                             timeout=INSTALL_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def ensure_running(workspace, runner: Callable = _run, install: Callable[[], bool] = _install) -> str:
    """With tracking off: the launch agent runs unless the owner turned it off explicitly."""
    if not cfg.proxy_allowed():
        return "left off (CREDENTIAL_PROXY_ENABLED is off)"
    if sys.platform != "darwin":
        return "not started: launchd only"
    retired = read_intent().get("status") in ("retiring", "retired")
    if job_loaded(runner):
        said = "already loaded"
    elif plist_path().is_file():
        out = runner(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path())])
        if out is None or out.returncode != 0:
            return "not started: launchctl bootstrap failed"
        said = "restored" if retired else "started"
    elif not install():
        return "not installed: the installer failed"
    else:
        said = "installed"
    if retired:
        write_intent("restored", reason="status line tracking turned off")
        _record(workspace, status="restored", reason="status line tracking turned off")
    return said

if __name__ == "__main__":
    print(retire(cfg.workspace(), dry_run="--dry-run" in sys.argv))
