"""Canonical run-dir + runtime-socket resolution — the ONE definition shared
by the daemon (server.py) and the CLI (src/runtime-cli/sutando-runtime.py).

Review blocker: both sides had duplicated an invented macOS-only fallback
(`~/Library/Application Support/space.ag2.app/run`), which (a) meant two
copies of a path policy that could drift and (b) silently made the runtime
Mac-only. Resolution order:

  1. SUTANDO_RUN_DIR              — explicit override, always wins
  2. darwin: ~/Library/Application Support/space.ag2.app/run
     (the Desktop-supervised runtime root on macOS)
  3. $XDG_RUNTIME_DIR/sutando     — the Linux/systemd per-user run dir
  4. ~/.sutando/run               — last-resort portable fallback

The socket default lives here too so `SUTANDO_RUNTIME_SOCKET` is interpreted
identically on both ends of the connection.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def run_dir() -> Path:
    env = os.environ.get("SUTANDO_RUN_DIR")
    if env:
        return Path(env)
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support"
                / "space.ag2.app" / "run")
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "sutando"
    return Path.home() / ".sutando" / "run"


def socket_path() -> str:
    return (os.environ.get("SUTANDO_RUNTIME_SOCKET")
            or str(run_dir() / "sutando-runtime.sock"))
