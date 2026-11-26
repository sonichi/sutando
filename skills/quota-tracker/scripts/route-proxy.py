#!/usr/bin/env python3
"""`on_session_route` hook: runs before a launch decides its API routing. With status line
tracking off, it makes sure the credential-proxy launch agent runs (bootstrap, or install
if never installed) unless CREDENTIAL_PROXY_ENABLED is off, and waits for its port, so the
first launch after an opt-out is routed through the proxy, never left with neither."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import proxy_retire  # noqa: E402
import statusline_config as cfg  # noqa: E402

LISTEN_WAIT_S = 8.0
LOCK_WAIT_S = 25.0  # a retirement holding it runs in a config hook killed after 10 s


def listening(runner: Callable = proxy_retire._run) -> bool:
    out = runner(["lsof", "-nP", f"-iTCP:{proxy_retire.PORT}", "-sTCP:LISTEN"])
    return out is not None and out.returncode == 0


def main(runner: Callable = proxy_retire._run, wait_s: float = LISTEN_WAIT_S, sleep: Callable = time.sleep,
         install: Callable[[], bool] = proxy_retire._install) -> int:
    try:
        if cfg.enabled():
            return 0
        # Held through the port wait, so no retirement boots it out after it reads as ready.
        with proxy_retire.proxy_lock(LOCK_WAIT_S, sleep) as held:
            if not held:
                print("quota-tracker: credential proxy launch agent: not started (proxy lock not taken); "
                      "the launcher's port check decides routing", file=sys.stderr)
                return 0
            said = proxy_retire.ensure_running(cfg.workspace(), runner=runner, install=install)
            if said in ("restored", "started", "installed", "already loaded"):
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
