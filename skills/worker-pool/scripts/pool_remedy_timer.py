#!/usr/bin/env python3
"""The timer that makes the remedy run on its own: a launchd job, every 5 minutes.

`pool_remedy --sweep` observes, decides and resumes a dead worker — but only
when something calls it. A session cron dies with the session and fires only
while the core is idle, which is exactly when a stalled core cannot help. So the
caller is the OS: a per-user LaunchAgent that needs no session, no quota and no
core at all. It runs the sweep and nothing else; `escalate` decisions are still
returned to whoever reads the log, never acted on.

    python3 pool_remedy_timer.py install   --workspace WS --repo REPO [--interval 300]
    python3 pool_remedy_timer.py uninstall
    python3 pool_remedy_timer.py status

Idempotent: install boots the existing job out before bootstrapping the rendered
plist, so re-running after a path or interval change replaces the job in place.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

LABEL = "com.sutando.pool-remedy"
DEFAULT_INTERVAL_S = 300
_HERE = Path(__file__).resolve().parent


def plist_path(launch_agents: Path | None = None) -> Path:
    base = launch_agents or (Path.home() / "Library" / "LaunchAgents")
    return base / f"{LABEL}.plist"


def service_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def render(workspace, repo, *, interval_s: int = DEFAULT_INTERVAL_S,
           python: str | None = None) -> dict:
    """The job, as launchd reads it. Paths are recorded absolute at install time:
    launchd runs with no shell and no cwd worth trusting."""
    workspace, repo = Path(workspace).resolve(), Path(repo).resolve()
    if interval_s < 60:
        raise ValueError(f"interval must be >= 60s (got {interval_s})")
    log = workspace / "logs" / "pool-remedy.log"
    # launchd's own PATH cannot find tmux; the installer's shell just could, so
    # record that PATH, with tmux's directory first in case the shell found it elsewhere.
    parts = [d for d in (os.environ.get("PATH") or "").split(":") if d]
    tmux = shutil.which("tmux")
    if tmux:
        parts.insert(0, str(Path(tmux).parent))
    path = ":".join(dict.fromkeys(parts)) or os.defpath
    return {
        "Label": LABEL,
        "ProgramArguments": [python or sys.executable, str(_HERE / "pool_remedy.py"),
                             "--workspace", str(workspace), "--repo", str(repo), "--sweep"],
        "StartInterval": int(interval_s),
        "RunAtLoad": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {"PATH": path},
        "ProcessType": "Background",
    }


def _launchctl(argv, runner=None):
    # Resolved at call time, so a test that patches `subprocess.run` is honoured;
    # a def-time default would bind the real one and reach the live launchd domain.
    return (runner or subprocess.run)(["launchctl", *argv], capture_output=True, text=True)


def is_loaded(runner=None) -> bool:
    return _launchctl(["print", service_target()], runner).returncode == 0


def bootout(runner=None, *, sleep=time.sleep) -> None:
    if not is_loaded(runner):
        return
    _launchctl(["bootout", service_target()], runner)
    # bootout is asynchronous; a bootstrap that races it fails as "already loaded".
    for _ in range(10):
        if not is_loaded(runner):
            return
        sleep(0.3)


def install(workspace, repo, *, interval_s: int = DEFAULT_INTERVAL_S, python=None,
            launch_agents: Path | None = None, runner=None,
            sleep=time.sleep) -> dict:
    job = render(workspace, repo, interval_s=interval_s, python=python)
    dest = plist_path(launch_agents)
    dest.parent.mkdir(parents=True, exist_ok=True)
    Path(job["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with open(tmp, "wb") as fh:
        plistlib.dump(job, fh)
    os.replace(tmp, dest)
    bootout(runner, sleep=sleep)
    r = _launchctl(["bootstrap", f"gui/{os.getuid()}", str(dest)], runner)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed rc={r.returncode}: "
                           f"{(r.stderr or r.stdout or '').strip()}")
    return {"plist": str(dest), "label": LABEL, "interval_s": job["StartInterval"],
            "log": job["StandardOutPath"], "loaded": is_loaded(runner)}


def uninstall(*, launch_agents: Path | None = None, runner=None,
              sleep=time.sleep) -> dict:
    bootout(runner, sleep=sleep)
    dest = plist_path(launch_agents)
    removed = dest.exists()
    dest.unlink(missing_ok=True)
    return {"plist": str(dest), "removed": removed, "loaded": is_loaded(runner)}


def status(*, launch_agents: Path | None = None, runner=None) -> dict:
    dest = plist_path(launch_agents)
    out = {"plist": str(dest), "installed": dest.exists(), "loaded": is_loaded(runner)}
    if out["installed"]:
        try:
            with open(dest, "rb") as fh:
                job = plistlib.load(fh)
            out["interval_s"] = job.get("StartInterval")
            out["log"] = job.get("StandardOutPath")
        except (OSError, ValueError) as e:
            out["error"] = f"plist unreadable: {e}"
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", choices=["install", "uninstall", "status"])
    p.add_argument("--workspace")
    p.add_argument("--repo")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_S,
                   help="seconds between sweeps (default 300)")
    p.add_argument("--launch-agents", help="LaunchAgents dir (default ~/Library/LaunchAgents)")
    a = p.parse_args(argv)
    la = Path(a.launch_agents) if a.launch_agents else None
    try:
        if a.command == "install":
            if not (a.workspace and a.repo):
                p.error("install needs --workspace and --repo")
            out = install(a.workspace, a.repo, interval_s=a.interval, launch_agents=la)
        elif a.command == "uninstall":
            out = uninstall(launch_agents=la)
        else:
            out = status(launch_agents=la)
    except (ValueError, RuntimeError, OSError) as e:
        print(f"pool_remedy_timer: {e}", file=sys.stderr)
        return 1
    for k, v in out.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
