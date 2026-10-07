#!/usr/bin/env python3
"""The timer that makes the remedy run on its own: a launchd job, every 5 minutes.

`pool_remedy --sweep` observes, decides and resumes a dead worker — but only
when something calls it. A session cron dies with the session and fires only
while the core is idle, which is exactly when a stalled core cannot help. So the
caller is the OS: a per-user LaunchAgent that needs no session, no quota and no
core at all. It runs the sweep and nothing else; `escalate` decisions are still
returned to whoever reads the log, never acted on.

    python3 pool_remedy_timer.py install   --workspace WS --repo REPO [--interval 300]
    python3 pool_remedy_timer.py uninstall --workspace WS
    python3 pool_remedy_timer.py status    --workspace WS

Each resolved workspace has its own label. Install migrates a legacy singleton
only when its plist names that workspace, preserving other pools' timers.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows can import spawn_worker even though it has no launchd.
    fcntl = None

LABEL = "com.sutando.pool-remedy"
DEFAULT_INTERVAL_S = 300
_HERE = Path(__file__).resolve().parent


def label_for(workspace) -> str:
    digest = hashlib.sha256(os.fsencode(Path(workspace).resolve())).hexdigest()[:16]
    return f"{LABEL}.{digest}"


def _launch_agents(launch_agents: Path | None = None) -> Path:
    return Path(launch_agents) if launch_agents is not None else Path.home() / "Library" / "LaunchAgents"


def plist_path(workspace, launch_agents: Path | None = None) -> Path:
    return _launch_agents(launch_agents) / f"{label_for(workspace)}.plist"


def legacy_plist_path(launch_agents: Path | None = None) -> Path:
    return _launch_agents(launch_agents) / f"{LABEL}.plist"


@contextmanager
def timer_lock(launch_agents: Path | None = None):
    """Serialize auto-ensure and explicit CLI changes to the host's timer jobs."""
    if fcntl is None:
        raise RuntimeError("launchd timer requires Unix file locking")
    base = _launch_agents(launch_agents)
    base.mkdir(parents=True, exist_ok=True)
    with open(base / f".{LABEL}.lock", "a+b") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def service_target(workspace) -> str:
    return f"gui/{os.getuid()}/{label_for(workspace)}"


def legacy_service_target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def remedy_script_path() -> str:
    return str(_HERE / "pool_remedy.py")


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
        "Label": label_for(workspace),
        "ProgramArguments": [python or sys.executable, remedy_script_path(),
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
    # A caller-supplied runner (e.g. spawn_worker._run) already bakes in its own
    # capture/text behavior -- adding these here duplicated the keywords.
    if runner is not None:
        return runner(["launchctl", *argv])
    return subprocess.run(["launchctl", *argv], capture_output=True, text=True)


def _is_loaded(target, runner=None) -> bool:
    return _launchctl(["print", target], runner).returncode == 0


def is_loaded(workspace, runner=None) -> bool:
    return _is_loaded(service_target(workspace), runner)


def _bootout(target, runner=None, *, sleep=time.sleep) -> None:
    if not _is_loaded(target, runner):
        return
    result = _launchctl(["bootout", target], runner)
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootout failed rc={result.returncode}: "
                           f"{(result.stderr or result.stdout or '').strip()}")
    # bootout is asynchronous; a bootstrap that races it fails as "already loaded".
    for _ in range(10):
        if not _is_loaded(target, runner):
            return
        sleep(0.3)
    raise RuntimeError(f"launchctl service remained loaded after bootout: {target}")


def bootout(workspace, runner=None, *, sleep=time.sleep) -> None:
    _bootout(service_target(workspace), runner, sleep=sleep)


def _status_for(label, dest, runner=None) -> dict:
    out = {"plist": str(dest), "label": label, "installed": dest.exists(),
           "loaded": _is_loaded(f"gui/{os.getuid()}/{label}", runner)}
    if not out["installed"]:
        return out
    try:
        with open(dest, "rb") as fh:
            job = plistlib.load(fh)
        if job.get("Label") != label:
            raise ValueError(f"plist label {job.get('Label')!r} does not match {label!r}")
        out["interval_s"] = job.get("StartInterval")
        out["log"] = job.get("StandardOutPath")
        args = job.get("ProgramArguments") or []
        if len(args) > 1:
            out["script"] = args[1]
        for key in ("workspace", "repo"):
            flag = "--" + key
            if flag in args and args.index(flag) + 1 < len(args):
                out[key] = args[args.index(flag) + 1]
    except (OSError, ValueError, TypeError) as e:
        out["error"] = f"plist unreadable: {e}"
    return out


def status(workspace, *, launch_agents: Path | None = None, runner=None) -> dict:
    return _status_for(label_for(workspace), plist_path(workspace, launch_agents), runner)


def legacy_status(*, launch_agents: Path | None = None, runner=None) -> dict:
    return _status_for(LABEL, legacy_plist_path(launch_agents), runner)


def install(workspace, repo, *, interval_s: int = DEFAULT_INTERVAL_S, python=None,
            launch_agents: Path | None = None, runner=None,
            sleep=time.sleep) -> dict:
    job = render(workspace, repo, interval_s=interval_s, python=python)
    dest = plist_path(workspace, launch_agents)
    dest.parent.mkdir(parents=True, exist_ok=True)
    Path(job["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
    prior = status(workspace, launch_agents=launch_agents, runner=runner)
    old_bytes = dest.read_bytes() if dest.exists() else None
    legacy = legacy_status(launch_agents=launch_agents, runner=runner)
    migrate = (legacy.get("installed") and not legacy.get("error")
               and legacy.get("workspace")
               and Path(legacy["workspace"]).resolve() == Path(workspace).resolve())
    legacy_hold = (Path(legacy["plist"]).with_name(f".{LABEL}.{uuid.uuid4().hex}.migrating")
                   if migrate else None)
    tmp = dest.with_name(dest.name + ".tmp")
    with open(tmp, "wb") as fh:
        plistlib.dump(job, fh)
    os.replace(tmp, dest)
    try:
        bootout(workspace, runner, sleep=sleep)
        if migrate:
            _bootout(legacy_service_target(), runner, sleep=sleep)
            os.replace(legacy["plist"], legacy_hold)
        r = _launchctl(["bootstrap", f"gui/{os.getuid()}", str(dest)], runner)
        if r.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed rc={r.returncode}: "
                               f"{(r.stderr or r.stdout or '').strip()}")
    except (RuntimeError, OSError) as e:
        rollback_errors = []
        try:
            if old_bytes is None:
                dest.unlink(missing_ok=True)
            else:
                tmp.write_bytes(old_bytes)
                os.replace(tmp, dest)
        except OSError as restore_error:
            rollback_errors.append(str(restore_error))
        if migrate and legacy_hold.exists():
            try:
                os.replace(legacy_hold, legacy["plist"])
            except OSError as restore_error:
                rollback_errors.append(str(restore_error))
        for was_loaded, target, path in (
                (prior["loaded"], service_target(workspace), str(dest)),
                (bool(migrate and legacy["loaded"]), legacy_service_target(), legacy["plist"])):
            if was_loaded and not _is_loaded(target, runner):
                if not Path(path).exists():
                    rollback_errors.append(f"could not restore {target}: plist missing at {path}")
                    continue
                restored = _launchctl(["bootstrap", f"gui/{os.getuid()}", path], runner)
                if restored.returncode != 0:
                    rollback_errors.append(f"could not restore {target}: "
                                           f"{(restored.stderr or restored.stdout or '').strip()}")
        if rollback_errors:
            raise RuntimeError(f"{e}; rollback failed: {'; '.join(rollback_errors)}") from e
        raise
    if migrate:
        legacy_hold.unlink(missing_ok=True)
    return {"plist": str(dest), "label": job["Label"], "interval_s": job["StartInterval"],
            "log": job["StandardOutPath"], "loaded": is_loaded(workspace, runner),
            "legacy_migrated": bool(migrate)}


def uninstall(workspace, *, launch_agents: Path | None = None, runner=None,
              sleep=time.sleep) -> dict:
    bootout(workspace, runner, sleep=sleep)
    dest = plist_path(workspace, launch_agents)
    removed = dest.exists()
    dest.unlink(missing_ok=True)
    legacy = legacy_status(launch_agents=launch_agents, runner=runner)
    if (legacy.get("installed") and legacy.get("workspace")
            and Path(legacy["workspace"]).resolve() == Path(workspace).resolve()):
        _bootout(legacy_service_target(), runner, sleep=sleep)
        Path(legacy["plist"]).unlink(missing_ok=True)
        removed = True
    return {"plist": str(dest), "removed": removed, "loaded": is_loaded(workspace, runner)}


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
        if not a.workspace:
            p.error(f"{a.command} needs --workspace")
        if a.command == "install":
            if not a.repo:
                p.error("install needs --repo")
            with timer_lock(la):
                out = install(a.workspace, a.repo, interval_s=a.interval, launch_agents=la)
        elif a.command == "uninstall":
            with timer_lock(la):
                out = uninstall(a.workspace, launch_agents=la)
        else:
            out = status(a.workspace, launch_agents=la)
    except (ValueError, RuntimeError, OSError) as e:
        print(f"pool_remedy_timer: {e}", file=sys.stderr)
        return 1
    for k, v in out.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
