#!/usr/bin/env python3
"""Detect a Claude config dir that is not signed in on its own, and tell the owner how to sign it in.

With status line tracking on, sessions always call Anthropic directly. A config dir that only
ever worked through the credential proxy (which injected the credential) may have no login of its
own. This never changes routing: health-check calls `check()` to find such a dir and asks the owner
once per dir per day to sign it in.

The probe runs `claude auth status --json` with the auth environment the direct session gets:
the pane's environment when tmux reports one (else this process's), the local proxy
`ANTHROPIC_BASE_URL` removed exactly as the launch removes it, the environment logins
(`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`) removed so the answer is
the config dir's own login, and `CLAUDE_CONFIG_DIR` set to the dir. Signed in means exit status 0 AND a literal JSON `"loggedIn": true`; anything else (a non-zero
exit even with JSON, a timeout, other output) is not signed in. A missing `claude` binary is
unknown. Environment values are passed to the probe only, never logged or stored, and the probe's
other output (email, org) is not kept.

Records: one atomically replaced file per config dir under `<workspace>/state/claude-sign-in/`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

RECORD_DIR = "claude-sign-in"
TIMEOUT_S = 8.0
NOTIFY_EVERY_S = 24 * 3600
PROBE = ["claude", "auth", "status", "--json"]
_LOCAL_PROXY = re.compile(r"^http://(localhost|127\.0\.0\.1|\[::1\]):7846/?$")
SWITCH = "QUOTA_STATUSLINE_ENABLED"
ENV_LOGINS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
OFF = {"0", "false", "no", "off"}


def direct_auth_env(launcher_env: Mapping[str, str], config_dir: str,
                    pane_env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment a direct session gets: the pane's when known, else the launcher's, without
    the local proxy URL, with this config dir."""
    env = dict(pane_env if pane_env is not None else launcher_env)
    if _LOCAL_PROXY.match(env.get("ANTHROPIC_BASE_URL", "")):
        del env["ANTHROPIC_BASE_URL"]
    for key in ENV_LOGINS:  # `loggedIn` must reflect the config dir's own login only
        env.pop(key, None)
    env["CLAUDE_CONFIG_DIR"] = config_dir
    env.setdefault("PATH", launcher_env.get("PATH", os.defpath))
    return env


def pane_env(socket: Optional[str], runner: Callable = subprocess.run) -> Optional[Dict[str, str]]:
    """tmux's global environment, which a new session's pane starts from; None when no server."""
    if not socket:
        return None
    try:
        out = runner(["tmux", "-S", socket, "show-environment", "-g"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    env: Dict[str, str] = {}
    for line in (out.stdout or "").splitlines():
        if "=" in line and not line.startswith("-"):
            k, _, v = line.partition("=")
            env[k] = v
    return env


def signed_in(env: Mapping[str, str], runner: Callable = subprocess.run,
              timeout: float = TIMEOUT_S) -> Optional[bool]:
    """True only on exit 0 with a literal `"loggedIn": true`; None when `claude` cannot run."""
    cmd = [env.get("CLAUDE_BIN") or PROBE[0], *PROBE[1:]]
    try:
        out = runner(cmd, capture_output=True, text=True, timeout=timeout,
                     stdin=subprocess.DEVNULL, env=dict(env))
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    try:
        data = json.loads(out.stdout)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("loggedIn") is True


def record_path(workspace, config_dir: str) -> Path:
    from workspace_default import status_path
    digest = hashlib.sha256(os.path.realpath(config_dir).encode("utf-8")).hexdigest()[:16]
    return status_path(f"{RECORD_DIR}/{digest}.json", Path(workspace))


def read_record(workspace, config_dir: str) -> dict:
    try:
        data = json.loads(record_path(workspace, config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_record(workspace, config_dir: str, data: dict) -> None:
    path = record_path(workspace, config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def sign_in_command(config_dir: str) -> str:
    import shlex
    return f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude auth login"


def _notify(workspace, config_dir: str, now: float) -> str:
    """Queue one message to the owner's DM (the proactive outbox) naming the dir and the command."""
    from local_record import write_text_whole
    from proactive_routing import proactive_filename
    name = proactive_filename(f"claude-sign-in-{int(now * 1000)}")
    write_text_whole(Path(workspace) / "results" / name, (
        f"Claude is not signed in for {config_dir}, so sessions there cannot reach Anthropic directly.\n"
        f"Sign it in by running this in a terminal on this computer, then follow the link it prints:\n"
        f"{sign_in_command(config_dir)}\n"
        "The next session launch picks the login up; nothing else to do.\n"))
    return name


def direct_routing_on() -> bool:
    """Status line tracking is on (env, else the declaring skill's manifest `config`)."""
    value = os.environ.get(SWITCH)
    if value in (None, ""):
        try:
            import skill_roots
            for manifest in (m for root in skill_roots.skill_roots()
                             for m in sorted(Path(root).glob("*/manifest.json"))):
                cfg = (skill_roots.read_manifest(manifest) or {}).get("config") or {}
                if isinstance(cfg, dict) and SWITCH in cfg:
                    value = str(cfg[SWITCH])
                    break
        except Exception:  # noqa: BLE001 — unknown reads as the shipped default
            value = None
    return value is not None and value.strip().lower() not in OFF


def check(workspace, config_dir: Optional[str], *, socket: Optional[str] = None,
          runner: Callable = subprocess.run, tmux_runner: Callable = subprocess.run,
          now: Optional[float] = None) -> dict:
    """Health-check row `claude-sign-in`: fail for a config dir that is not signed in on its own,
    queuing one owner message for it per NOTIFY_EVERY_S."""
    row = {"name": "claude-sign-in", "status": "ok"}
    if not direct_routing_on():
        row["detail"] = "sessions go through the credential proxy; no own sign-in needed"
        return row
    if not config_dir:
        row["detail"] = "the core's config dir could not be resolved; sign-in not checked"
        return row
    now = time.time() if now is None else now
    env = direct_auth_env(os.environ, config_dir, pane_env(socket, tmux_runner))
    verdict = signed_in(env, runner)
    prev = read_record(workspace, config_dir)
    rec = {"config_dir": os.path.realpath(config_dir), "signed_in": verdict, "checked_at": now}
    if verdict is None:
        row["detail"] = "claude CLI not found; sign-in not checked"
    elif verdict:
        row["detail"] = f"signed in: {config_dir}"
    else:
        notified = prev.get("notified_at")
        if not isinstance(notified, (int, float)) or now - notified >= NOTIFY_EVERY_S:
            try:
                rec["notice"] = _notify(workspace, config_dir, now)
                notified = now
            except (OSError, ValueError):
                pass
        if isinstance(notified, (int, float)):
            rec["notified_at"] = notified
        row["status"] = "fail"
        row["detail"] = (f"Claude is not signed in for {config_dir}: sessions there go direct and cannot "
                         f"reach Anthropic. Sign in: {sign_in_command(config_dir)}")
    try:
        write_record(workspace, config_dir, rec)
    except OSError:
        pass
    return row
