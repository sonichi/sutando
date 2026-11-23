#!/usr/bin/env python3
"""Session-config hooks: the scripts installed skills declare with the manifest field
`on_session_config`, run once per Claude session launch (the core and each pool worker)
after its CLAUDE_CONFIG_DIR is resolved and before the session starts.

Generic core helper: it names the field, never a skill; `src/skill_roots.py` resolves the
declarers. The contract a hook relies on:

- run as `<python> <script> --config-dir <dir>` for a `.py`, `bash <script> --config-dir <dir>`
  for a `.sh`; any other suffix is never run;
- stdin from the null device, output to this process's stderr, waited on for at most
  HOOK_SECONDS each (then killed); its exit status is reported, never fatal;
- the launch waits at most TOTAL_SECONDS for all of them, and never fails because of one.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, Tuple

FIELD = "on_session_config"
SUFFIXES = (".py", ".sh")
HOOK_SECONDS = 10.0
TOTAL_SECONDS = 20.0


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
        try:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr,
                                  timeout=min(HOOK_SECONDS, left), env=env)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = None
        except OSError as exc:
            print(f"session-config-hooks: {name}: not run ({type(exc).__name__})", file=sys.stderr)
            continue
        if rc != 0:
            print(f"session-config-hooks: {name}: {'timed out' if rc is None else f'exit {rc}'}",
                  file=sys.stderr)
        out.append((name, rc))
    return out


def _work(config_dir: str, deadline: float) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import skill_roots
        from workspace_default import resolve_workspace
        roots = skill_roots.skill_roots(resolve_workspace(migrate=False))
        hooks = skill_roots.declared_scripts(FIELD, roots, SUFFIXES)
    except Exception as exc:  # noqa: BLE001 — a launch never fails on a hook
        print(f"session-config-hooks: none run ({type(exc).__name__})", file=sys.stderr)
        return
    run(hooks, config_dir, sys.executable, deadline)


def main(argv: list) -> int:
    if len(argv) != 2 or argv[0] != "--config-dir" or not argv[1]:
        print("usage: session_config_hooks.py --config-dir <dir>", file=sys.stderr)
        return 0
    deadline = time.monotonic() + TOTAL_SECONDS
    t = threading.Thread(target=_work, args=(argv[1], deadline), daemon=True)
    t.start()
    t.join(TOTAL_SECONDS + 1)
    return 0


if __name__ == "__main__":
    code = main(sys.argv[1:])
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
