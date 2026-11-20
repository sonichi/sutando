#!/usr/bin/env python3
"""Stop handlers: the scripts installed skills declare with the manifest field `on_stop_requested`,
offered each Stop the task watcher reads (a CANCEL_INSTRUCTION naming a task). Generic core helper:
it names the field, never a skill; `src/skill_roots.py` resolves the declarers. The contract:
- run as `<python> <script> <task id> --tasks-dir <inbox> --control-file <control task>` for a
  `.py`, `bash <script> ...` for a `.sh`; any other suffix is never run;
- exit 0 takes the stop, and the handler then owns its ruling; any other exit, or no exit within
  OFFER_SECONDS, leaves it to the next handler, and with none the watcher cancels the task at once.
Exit 0 when a handler took the stop, else 1.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple

FIELD = "on_stop_requested"
SUFFIXES = (".py", ".sh")
OFFER_SECONDS = 10.0


def offer(handlers: Iterable[Tuple[str, Path]], args: list, python: str,
          run: Callable = subprocess.run, timeout: float = OFFER_SECONDS) -> Optional[str]:
    """Offer the stop to each injected (skill name, script) in order; the name of the one that took it."""
    for name, script in handlers:
        suffix = Path(script).suffix
        if suffix not in SUFFIXES:
            continue
        argv = [python, str(script), *args] if suffix == ".py" else ["bash", str(script), *args]
        try:
            if run(argv, stdin=subprocess.DEVNULL, timeout=timeout).returncode == 0:
                return name
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"stop-hooks: {name}: not offered ({type(exc).__name__})", file=sys.stderr)
    return None


def main(argv: Optional[list] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import skill_roots
        from workspace_default import resolve_workspace
        handlers = skill_roots.declared_scripts(FIELD, skill_roots.skill_roots(resolve_workspace(migrate=False)),
                                                SUFFIXES)
    except Exception as exc:  # noqa: BLE001 — no handler found means the watcher's own cancel
        print(f"stop-hooks: none offered ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0 if offer(handlers, args, sys.executable) else 1


if __name__ == "__main__":
    sys.exit(main())
