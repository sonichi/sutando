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
exit even with JSON, other output) is not signed in. A missing `claude` binary or a timed-out
probe is unknown and sends no notice. Dirs are probed concurrently, at most POOL at a time. Environment values are passed to the probe only, never logged or stored, and the probe's
other output (email, org) is not kept.

Records: one atomically replaced file per config dir under `<workspace>/state/claude-sign-in/`;
daily notice markers there older than NOTICE_KEEP_DAYS are pruned on each check.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

RECORD_DIR = "claude-sign-in"
TIMEOUT_S = 8.0
POOL = 4
NOTICE_KEEP_DAYS = 7
PROBE = ["claude", "auth", "status", "--json"]
_LOCAL_PROXY = re.compile(r"^http://(localhost|127\.0\.0\.1|\[::1\]):7846/?$")
ENV_LOGINS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


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
    """True only on exit 0 with a literal `"loggedIn": true`; None when `claude` is missing or times out."""
    cmd = [env.get("CLAUDE_BIN") or PROBE[0], *PROBE[1:]]
    try:
        out = runner(cmd, capture_output=True, text=True, timeout=timeout,
                     stdin=subprocess.DEVNULL, env=dict(env))
    except (FileNotFoundError, subprocess.TimeoutExpired):
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


REPO = Path(__file__).resolve().parent.parent
# The launchers' own predicate (src/claude_routing.sh), with PY resolved as they resolve it.
_ROUTING_SH = (
    '. "$1/scripts/python-binary.sh" && PY="$(resolve_python "$1")" && REPO="$1" '
    '&& . "$1/src/skill-manifest-config.sh" && . "$1/src/claude_routing.sh" '
    '&& { if claude_direct_routing; then echo direct; else echo proxy; fi; }')


def direct_routing_on(runner: Callable = subprocess.run, repo: Path = REPO) -> Optional[bool]:
    """Whether the launchers route Claude sessions direct: True, False, or None when unknown."""
    try:
        out = runner(["/bin/bash", "-c", _ROUTING_SH, "bash", str(repo)], capture_output=True,
                     text=True, timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    verdict = (out.stdout or "").strip().splitlines()[-1:] if out.returncode == 0 else []
    return {"direct": True, "proxy": False}.get(verdict[0]) if verdict else None


def _live_sessions(ps_runner: Callable = subprocess.run) -> Optional[Dict[str, Dict[str, str]]]:
    """live_sessions, but None when the process list could not be read."""
    from quota_availability import _env_pairs
    try:
        out = ps_runner(["ps", "-xeww", "-U", str(os.getuid()), "-o", "command="],
                        capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    found: Dict[str, Dict[str, str]] = {}
    for cmd in (out.stdout or "").splitlines():
        cmd = cmd.strip()
        argv = cmd.split(" ")
        if os.path.basename(argv[0]) != "claude" or "--name" not in argv:
            continue
        env = _env_pairs(cmd)
        ccd = env.get("CLAUDE_CONFIG_DIR")
        if ccd:
            found.setdefault(os.path.realpath(ccd), env)
    return found


def live_sessions(ps_runner: Callable = subprocess.run) -> Dict[str, Dict[str, str]]:
    """{config dir: that session's own environment} for each live Sutando Claude session (a
    `claude` process the launchers started with `--name`), the core's and every worker's, deduped."""
    return _live_sessions(ps_runner) or {}


def proxy_in_use(ps_runner: Callable = subprocess.run, lsof_runner: Callable = subprocess.run) -> Optional[bool]:
    """Whether anything still uses the local proxy: an established connection on its port, or a
    live Sutando session whose environment carries the proxy URL. None when either is unreadable."""
    sessions = _live_sessions(ps_runner)
    if sessions is None:
        return None
    if any(_LOCAL_PROXY.match(env.get("ANTHROPIC_BASE_URL", "")) for env in sessions.values()):
        return True
    try:
        out = lsof_runner(["/usr/sbin/lsof", "-nP", "-iTCP:7846", "-sTCP:ESTABLISHED", "-t"],
                          capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode == 0 and (out.stdout or "").strip():
        return True
    # lsof exits 1 with no output when nothing matches; anything else is unknown.
    return False if out.returncode == 1 and not (out.stdout or "").strip() and not (out.stderr or "").strip() else None


def _claim_notice(workspace, config_dir: str, now: float) -> bool:
    """True for the one caller that claims today's notice for this dir (exclusive create)."""
    day = time.strftime("%Y%m%d", time.gmtime(now))
    marker = record_path(workspace, config_dir).with_name(f"{record_path(workspace, config_dir).stem}-{day}.notice")
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    return True


def prune_notices(workspace, now: float, keep_days: int = NOTICE_KEEP_DAYS) -> None:
    """Remove daily notice markers whose day is more than `keep_days` before `now`."""
    from workspace_default import status_path
    cutoff = time.strftime("%Y%m%d", time.gmtime(now - keep_days * 86400))
    for marker in status_path(RECORD_DIR, Path(workspace)).glob("*-*.notice"):
        day = marker.stem.rpartition("-")[2]
        if len(day) == 8 and day.isdigit() and day < cutoff:
            try:
                marker.unlink()
            except OSError:
                pass


def check_dir(workspace, config_dir: str, env: Mapping[str, str], runner: Callable, now: float) -> Optional[bool]:
    """Probe one config dir, record it, and send today's notice once when it is not signed in."""
    verdict = signed_in(env, runner)
    rec = {"config_dir": os.path.realpath(config_dir), "signed_in": verdict, "checked_at": now}
    if verdict is False:
        try:
            if _claim_notice(workspace, config_dir, now):
                rec["notice"] = _notify(workspace, config_dir, now)
        except (OSError, ValueError):
            pass
    try:
        write_record(workspace, config_dir, rec)
    except OSError:
        pass
    return verdict


def check(workspace, config_dir: Optional[str], *, socket: Optional[str] = None,
          runner: Callable = subprocess.run, tmux_runner: Callable = subprocess.run,
          ps_runner: Callable = subprocess.run, now: Optional[float] = None) -> dict:
    """Health-check row `claude-sign-in`: fail naming every config dir a live Sutando session uses
    (the core's and each worker's) that is not signed in on its own, with today's notice sent once."""
    row = {"name": "claude-sign-in", "status": "ok"}
    if direct_routing_on() is not True:
        row["detail"] = "sessions go through the credential proxy; no own sign-in needed"
        return row
    now = time.time() if now is None else now
    dirs: Dict[str, Dict[str, str]] = {}
    if config_dir:
        dirs[os.path.realpath(config_dir)] = direct_auth_env(os.environ, config_dir, pane_env(socket, tmux_runner))
    for ccd, env in live_sessions(ps_runner).items():
        dirs.setdefault(ccd, direct_auth_env(os.environ, ccd, env))
    if not dirs:
        row["detail"] = "no config dir to check (the core's could not be resolved and no session is live)"
        return row
    try:
        prune_notices(workspace, now)
    except OSError:
        pass
    with ThreadPoolExecutor(max_workers=min(POOL, len(dirs))) as pool:
        futures = {d: pool.submit(check_dir, workspace, d, env, runner, now) for d, env in dirs.items()}
    verdicts = {d: f.result() for d, f in futures.items()}
    unsigned = [d for d, v in verdicts.items() if v is False]
    unknown = [d for d, v in verdicts.items() if v is None]
    if unsigned:
        row["status"] = "fail"
        row["detail"] = ("Claude is not signed in for " + ", ".join(unsigned) + ": sessions there go direct and "
                         "cannot reach Anthropic. Sign in: " + "; ".join(sign_in_command(d) for d in unsigned))
    elif unknown:
        row["detail"] = ("sign-in unknown (claude CLI not found or the probe timed out) for "
                         + ", ".join(unknown))
    else:
        row["detail"] = "signed in: " + ", ".join(verdicts)
    return row
