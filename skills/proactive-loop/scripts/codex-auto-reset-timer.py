#!/usr/bin/env python3
"""Install one per-Codex-home launchd job for the earned-reset check."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:
    fcntl = None


LABEL = "com.sutando.codex-auto-reset"
INTERVAL_S = 300
_HERE = Path(__file__).resolve().parent
_FLAG = "SUTANDO_CODEX_AUTO_RESET_ENABLED"


def label_for(codex_home: str | Path) -> str:
    digest = hashlib.sha256(os.fsencode(Path(codex_home).expanduser().resolve())).hexdigest()[:16]
    return f"{LABEL}.{digest}"


def _launch_agents(launch_agents: str | Path | None = None) -> Path:
    return Path(launch_agents) if launch_agents is not None else Path.home() / "Library/LaunchAgents"


def plist_path(codex_home: str | Path, launch_agents: str | Path | None = None) -> Path:
    return _launch_agents(launch_agents) / f"{label_for(codex_home)}.plist"


def service_target(codex_home: str | Path) -> str:
    return f"gui/{os.getuid()}/{label_for(codex_home)}"


@contextmanager
def timer_lock(launch_agents: str | Path | None = None):
    if fcntl is None:
        raise RuntimeError("launchd timer requires Unix file locking")
    base = _launch_agents(launch_agents)
    base.mkdir(parents=True, exist_ok=True)
    with open(base / f".{LABEL}.lock", "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def render(workspace: str | Path, codex_home: str | Path, *,
           python: str | Path | None = None, codex_bin: str | Path | None = None,
           enabled_override: str | None = None) -> dict:
    workspace = Path(workspace).expanduser().resolve()
    codex_home = Path(codex_home).expanduser().resolve()
    codex = codex_bin or shutil.which("codex")
    if not codex:
        raise ValueError("codex executable was not found in PATH")
    codex = Path(codex).expanduser().absolute()
    if not codex.is_file() or not os.access(codex, os.X_OK):
        raise ValueError(f"codex executable is not runnable: {codex}")
    interpreter = Path(python or sys.executable).expanduser().absolute()
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise ValueError(f"python executable is not runnable: {interpreter}")
    path_parts = [str(codex.parent), *(part for part in os.get_exec_path()
                                      if part and Path(part).is_absolute())]
    path = os.pathsep.join(dict.fromkeys(path_parts))
    env = {"CODEX_HOME": str(codex_home), "PATH": path}
    override = enabled_override if enabled_override is not None else os.environ.get(_FLAG)
    if override is not None:
        env[_FLAG] = override
    log = workspace / "logs/codex-auto-reset.log"
    return {
        "Label": label_for(codex_home),
        "ProgramArguments": [str(interpreter), str(_HERE / "codex-auto-reset.py"),
                             "--workspace", str(workspace), "--codex-home", str(codex_home),
                             "--codex-bin", str(codex), "--json"],
        "StartInterval": INTERVAL_S,
        "RunAtLoad": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": env,
        "ProcessType": "Background",
    }


def _launchctl(argv: list[str], runner=None):
    if runner is not None:
        return runner(["launchctl", *argv])
    return subprocess.run(["launchctl", *argv], capture_output=True, text=True)


def _loaded(codex_home: str | Path, runner=None) -> bool:
    return _launchctl(["print", service_target(codex_home)], runner).returncode == 0


def _bootout(codex_home: str | Path, runner=None, *, sleep=time.sleep) -> None:
    target = service_target(codex_home)
    if not _loaded(codex_home, runner):
        return
    result = _launchctl(["bootout", target], runner)
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootout failed rc={result.returncode}: "
                           f"{(result.stderr or result.stdout or '').strip()}")
    for _ in range(10):
        if not _loaded(codex_home, runner):
            return
        sleep(0.3)
    raise RuntimeError(f"launchctl service remained loaded after bootout: {target}")


def _bootstrap(dest: Path, runner=None) -> None:
    result = _launchctl(["bootstrap", f"gui/{os.getuid()}", str(dest)], runner)
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed rc={result.returncode}: "
                           f"{(result.stderr or result.stdout or '').strip()}")


def _write_atomic(dest: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _job_bytes(job: dict) -> bytes:
    return plistlib.dumps(job, sort_keys=True)


def status(codex_home: str | Path, *, launch_agents: str | Path | None = None,
           runner=None) -> dict:
    dest = plist_path(codex_home, launch_agents)
    result = {"label": label_for(codex_home), "plist": str(dest),
              "installed": dest.exists(), "loaded": _loaded(codex_home, runner)}
    if not result["installed"]:
        return result
    try:
        with open(dest, "rb") as stream:
            job = plistlib.load(stream)
        if job.get("Label") != result["label"]:
            raise ValueError("plist label does not match its filename")
        result["interval_s"] = job.get("StartInterval")
        result["workspace"] = job.get("ProgramArguments", [None] * 4)[3]
        result["codex_home"] = job.get("EnvironmentVariables", {}).get("CODEX_HOME")
        result["log"] = job.get("StandardOutPath")
    except (IndexError, OSError, TypeError, ValueError) as exc:
        result["error"] = f"plist unreadable: {exc}"
    return result


def _install_locked(job: dict, codex_home: str | Path, *,
                    launch_agents: str | Path | None = None,
                    runner=None, sleep=time.sleep) -> dict:
    dest = plist_path(codex_home, launch_agents)
    dest.parent.mkdir(parents=True, exist_ok=True)
    Path(job["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
    prior_bytes = dest.read_bytes() if dest.exists() else None
    was_loaded = _loaded(codex_home, runner)
    desired = _job_bytes(job)
    if prior_bytes == desired and was_loaded:
        return {**status(codex_home, launch_agents=launch_agents, runner=runner), "changed": False}
    _write_atomic(dest, desired)
    try:
        _bootout(codex_home, runner, sleep=sleep)
        _bootstrap(dest, runner)
    except (OSError, RuntimeError) as exc:
        rollback_errors = []
        try:
            _bootout(codex_home, runner, sleep=sleep)
        except (OSError, RuntimeError) as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        try:
            if prior_bytes is None:
                dest.unlink(missing_ok=True)
            else:
                _write_atomic(dest, prior_bytes)
        except OSError as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        if was_loaded and prior_bytes is not None and not _loaded(codex_home, runner):
            try:
                _bootstrap(dest, runner)
            except (OSError, RuntimeError) as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise RuntimeError(f"{exc}; rollback failed: {'; '.join(rollback_errors)}") from exc
        raise
    return {**status(codex_home, launch_agents=launch_agents, runner=runner), "changed": True}


def ensure(workspace: str | Path, codex_home: str | Path, *,
           python: str | Path | None = None, codex_bin: str | Path | None = None,
           enabled_override: str | None = None,
           launch_agents: str | Path | None = None, runner=None, sleep=time.sleep) -> dict:
    with timer_lock(launch_agents):
        if enabled_override is None and _FLAG not in os.environ:
            prior_path = plist_path(codex_home, launch_agents)
            if prior_path.exists():
                with open(prior_path, "rb") as stream:
                    prior = plistlib.load(stream)
                if prior.get("Label") != label_for(codex_home):
                    raise ValueError("existing Codex reset timer label does not match")
                prior_env = prior.get("EnvironmentVariables")
                if not isinstance(prior_env, dict):
                    raise ValueError("existing Codex reset timer environment is invalid")
                enabled_override = prior_env.get(_FLAG)
                if enabled_override is not None and not isinstance(enabled_override, str):
                    raise ValueError("existing Codex reset enable flag is invalid")
        job = render(workspace, codex_home, python=python, codex_bin=codex_bin,
                     enabled_override=enabled_override)
        return _install_locked(job, codex_home, launch_agents=launch_agents,
                               runner=runner, sleep=sleep)


def install(workspace: str | Path, codex_home: str | Path, **kwargs) -> dict:
    return ensure(workspace, codex_home, **kwargs)


def uninstall(codex_home: str | Path, *, launch_agents: str | Path | None = None,
              runner=None, sleep=time.sleep) -> dict:
    with timer_lock(launch_agents):
        dest = plist_path(codex_home, launch_agents)
        removed = dest.exists()
        _bootout(codex_home, runner, sleep=sleep)
        dest.unlink(missing_ok=True)
        return {"label": label_for(codex_home), "plist": str(dest),
                "removed": removed, "loaded": _loaded(codex_home, runner)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["ensure", "install", "status", "uninstall"])
    parser.add_argument("--workspace")
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--codex-bin")
    parser.add_argument("--launch-agents")
    args = parser.parse_args(argv)
    if args.command in ("ensure", "install") and not args.workspace:
        parser.error(f"{args.command} needs --workspace")
    if sys.platform != "darwin" and args.launch_agents is None:
        if args.command == "ensure":
            print("launchd unavailable on this platform")
            return 0
        parser.error("launchd timer requires macOS")
    try:
        if args.command in ("ensure", "install"):
            result = ensure(args.workspace, args.codex_home, codex_bin=args.codex_bin,
                            launch_agents=args.launch_agents)
        elif args.command == "uninstall":
            result = uninstall(args.codex_home, launch_agents=args.launch_agents)
        else:
            result = status(args.codex_home, launch_agents=args.launch_agents)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"codex-auto-reset-timer: {exc}", file=sys.stderr)
        return 1
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
