#!/usr/bin/env python3
"""`on_session_route` hook: runs before a launch decides its API routing. With status line
tracking off, it turns the credential-proxy launch agent back on (only if proxy_retire.py
turned it off) and waits for its port to listen, so the first launch after an opt-out is
routed through the proxy rather than going direct with neither proxy nor recorder."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proxy_retire  # noqa: E402
import statusline_config as cfg  # noqa: E402

LISTEN_WAIT_S = 8.0


def listening(runner: Callable = proxy_retire._run) -> bool:
    out = runner(["lsof", "-nP", f"-iTCP:{proxy_retire.PORT}", "-sTCP:LISTEN"])
    return out is not None and out.returncode == 0


def main(runner: Callable = proxy_retire._run, wait_s: float = LISTEN_WAIT_S, sleep: Callable = time.sleep) -> int:
    try:
        if cfg.enabled():
            return 0
        said = proxy_retire.restore(cfg.workspace(), runner=runner)
        if said in ("restored", "already loaded"):
            deadline = time.monotonic() + wait_s
            while not listening(runner) and time.monotonic() < deadline:
                sleep(0.25)
            said += ", listening" if listening(runner) else ", not listening yet"
        print(f"quota-tracker: credential proxy launch agent: {said}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — a launch never fails on this
        print(f"quota-tracker: credential proxy restore skipped ({type(exc).__name__})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
