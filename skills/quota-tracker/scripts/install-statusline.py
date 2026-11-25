#!/usr/bin/env python3
"""`on_session_config` hook: installs (or, with QUOTA_STATUSLINE_ENABLED off, removes)
the quota status line recorder and StopFailure hook in one Claude config dir."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import statusline_config as cfg  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="install-statusline")
    ap.add_argument("--config-dir", required=True)
    a = ap.parse_args(argv)
    try:
        done = cfg.install(a.config_dir, cfg.enabled())
    except Exception as exc:  # noqa: BLE001 — a launch never fails on this
        print(f"quota-tracker: status line recorder not installed ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return 0
    if done != "unchanged":
        print(f"quota-tracker: status line recorder {done} in {a.config_dir}/settings.json",
              file=sys.stderr)
    try:
        import proxy_retire
        ws = cfg.workspace()
        if cfg.enabled():  # turning it back on is route-proxy.py's, before routing is decided
            said = proxy_retire.retire(ws)
            print(f"quota-tracker: credential proxy launch agent: {said}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"quota-tracker: credential proxy check skipped ({type(exc).__name__})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
