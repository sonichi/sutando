"""Dependency-light runner for optional script-backed capabilities.

Callers own capability discovery and inject the script path. This module only
standardizes the fail-open subprocess contract, so core infrastructure does not
need to know which skills or adapter features are installed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence


ErrorHandler = Callable[[Exception], None]


def run_optional_script(
    script_path: Path,
    args: Sequence[str],
    *,
    timeout: float,
    on_error: Optional[ErrorHandler] = None,
    input: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    isolated: bool = False,
) -> Optional[str]:
    """Run an installed script and return non-empty stdout on success.

    A missing script, non-zero exit, empty stdout, or execution failure returns
    ``None``. The caller may log execution failures through ``on_error``;
    missing and ordinary non-zero outcomes remain silent.

    ``input`` is written to the script's stdin; ``env`` replaces its whole
    environment (the caller's allowlist, never the parent's); ``isolated`` runs
    it as ``python -I``. Each is passed only when given, so a caller that names
    none runs exactly the call it always did.
    """
    if not script_path.exists():
        return None
    extra = {}
    if input is not None:
        extra["input"] = input
    if env is not None:
        extra["env"] = dict(env)
    try:
        result = subprocess.run(
            [sys.executable, *(["-I"] if isolated else []), str(script_path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            **extra,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception as exc:
        if on_error is not None:
            on_error(exc)
    return None
