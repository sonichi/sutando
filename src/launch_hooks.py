#!/usr/bin/env python3
"""Launch hooks: the scripts installed skills declare with the manifest field `on_launch`, each
started once per core launch, detached, so no launch waits on or fails because of one.

Generic core helper: it names the field, never a skill; `src/skill_roots.py` resolves the
declarers across the sanctioned roots and `main()` (the launcher's edge, run by
src/agent/start-cli.sh) injects them into `start()`. The contract a hook relies on:

- run with no arguments: `<python> <script>` for a `.py`, `bash <script>` for a `.sh`; any other
  suffix is never run, whatever a manifest that skipped lint declares;
- in its own session, stdin from /dev/null, stdout and stderr appended to
  `<workspace>/logs/launch-hook-<skill>.log`, which must be a regular file (a symlink, FIFO or
  anything else there means the hook is not started, and nothing blocks on it);
- nobody waits for it, so it must finish on its own; one that cannot start is reported on stderr
  and the others still start.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

FIELD = "on_launch"
SUFFIXES = (".py", ".sh")  # the schema's and the linter's contract, enforced here too


def start(hooks: Iterable[Tuple[str, Path]], log_dir: Path, python: str,
          env: Optional[dict] = None) -> List[Tuple[str, int]]:
    """Start each injected (skill name, script) detached; returns (name, pid) of those started."""
    started = []
    for name, script in hooks:
        suffix = Path(script).suffix
        if suffix not in SUFFIXES:
            print(f"launch-hooks: {name}: not started (not a .py or .sh script)", file=sys.stderr)
            continue
        argv = [python, str(script)] if suffix == ".py" else ["bash", str(script)]
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log = open_log(log_dir / f"launch-hook-{name}.log")
        except OSError as exc:
            print(f"launch-hooks: {name}: not started (log: {type(exc).__name__})", file=sys.stderr)
            continue
        try:
            with log:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True, close_fds=True, env=env)
            started.append((name, proc.pid))
        except OSError as exc:
            print(f"launch-hooks: {name}: not started ({type(exc).__name__})", file=sys.stderr)
    return started


def open_log(path: Path):
    """The hook's log for appending: a regular file, never followed through a symlink, and never
    waited on (a FIFO with no reader fails at once). Anything else raises OSError."""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path.name} is not a regular file")
        os.set_blocking(fd, True)
        return os.fdopen(fd, "ab")
    except BaseException:
        os.close(fd)
        raise


def main() -> int:
    """The edge: resolve the workspace and the declarers, start them, and always exit 0."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from skill_roots import declared_scripts, skill_roots
        from workspace_default import resolve_workspace
        workspace = resolve_workspace(migrate=False)
        hooks = declared_scripts(FIELD, skill_roots(workspace), SUFFIXES)
    except Exception as exc:  # noqa: BLE001 — a launch never fails on a hook
        print(f"launch-hooks: none started ({type(exc).__name__})", file=sys.stderr)
        return 0
    for name, pid in start(hooks, Path(workspace) / "logs", sys.executable):
        print(f"launch-hooks: started {name} (pid {pid})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
