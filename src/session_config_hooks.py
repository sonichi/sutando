#!/usr/bin/env python3
"""Session-config hooks: the scripts installed skills declare with the manifest field
`on_session_config`, run once per Claude session launch (the core and each pool worker)
after its CLAUDE_CONFIG_DIR is resolved and before the session starts.

Generic core helper: it names the field, never a skill; `src/skill_roots.py` resolves the
declarers. The contract a hook relies on:

- run as `<python> <script> --config-dir <dir>` for a `.py`, `bash <script> --config-dir <dir>`
  for a `.sh`; any other suffix is never run;
- in its own process group, stdin from the null device, output to this process's stderr,
  waited on for at most HOOK_SECONDS each; past that the whole group is killed and reaped, so
  nothing it started writes after the launch continues; its exit status is reported, never fatal;
- the launch waits at most TOTAL_SECONDS for all of them, and never fails because of one.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, List, Tuple

FIELD = "on_session_config"
SUFFIXES = (".py", ".sh")
HOOK_SECONDS = 10.0
TOTAL_SECONDS = 20.0
DISCOVERY_SECONDS = 5.0
WINDOWS = os.name == "nt"


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if WINDOWS:
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass


def run(hooks: Iterable[Tuple[str, Path]], config_dir: str, python: str,
        deadline: float, env=None) -> list:
    """Run each injected (skill name, script); returns (name, exit code or None on timeout)."""
    out = []
    for name, script in hooks:
        suffix = Path(script).suffix
        if suffix not in SUFFIXES:
            print(f"session-config-hooks: {name}: not run (not a .py or .sh script)", file=sys.stderr)
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            print(f"session-config-hooks: {name}: not run (launch budget spent)", file=sys.stderr)
            continue
        argv = ([python] if suffix == ".py" else ["bash"]) + [str(script), "--config-dir", config_dir]
        group = ({"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)} if WINDOWS
                 else {"start_new_session": True})
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr,
                                    env=env, **group)
        except OSError as exc:
            print(f"session-config-hooks: {name}: not run ({type(exc).__name__})", file=sys.stderr)
            continue
        try:
            rc = proc.wait(min(HOOK_SECONDS, left))
        except subprocess.TimeoutExpired:
            rc = None
        finally:
            _kill_group(proc)  # the hook's own children too, even after it exited
        if rc != 0:
            print(f"session-config-hooks: {name}: {'timed out, killed' if rc is None else f'exit {rc}'}",
                  file=sys.stderr)
        out.append((name, rc))
    return out


def _discover(found: List) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import skill_roots
        from workspace_default import resolve_workspace
        roots = skill_roots.skill_roots(resolve_workspace(migrate=False))
        found.extend(skill_roots.declared_scripts(FIELD, roots, SUFFIXES))
        found.append(None)
    except Exception as exc:  # noqa: BLE001 — a launch never fails on a hook
        print(f"session-config-hooks: none run ({type(exc).__name__})", file=sys.stderr)


def main(argv: list) -> int:
    if len(argv) != 2 or argv[0] != "--config-dir" or not argv[1]:
        print("usage: session_config_hooks.py --config-dir <dir>", file=sys.stderr)
        return 0
    deadline = time.monotonic() + TOTAL_SECONDS
    found: List = []
    t = threading.Thread(target=_discover, args=(found,), daemon=True)
    t.start()
    t.join(DISCOVERY_SECONDS)
    if not found or found[-1] is not None:
        if t.is_alive():
            print("session-config-hooks: none run (discovery timed out)", file=sys.stderr)
        return 0
    # Hooks run on this thread, so no hook outlives this process's return.
    run(found[:-1], argv[1], sys.executable, deadline)
    return 0


if __name__ == "__main__":
    code = main(sys.argv[1:])
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
