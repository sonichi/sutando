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


def instance_id() -> str:
    """The instance this process belongs to. "default" preserves the
    single-instance world; a second instance sets SUTANDO_INSTANCE_ID and
    every runtime resource scopes under it (isolation spec V1)."""
    return os.environ.get("SUTANDO_INSTANCE_ID") or "default"


def instance_run_dir(instance: str | None = None) -> Path:
    return run_dir() / (instance or instance_id())


def socket_path(instance: str | None = None) -> str:
    """SUTANDO_RUNTIME_SOCKET overrides; otherwise instance-scoped
    <run dir>/<instance>/runtime.sock so two instances can never collide.
    The legacy flat <run dir>/sutando-runtime.sock is still honored for the
    default instance when it already exists (pre-M2 daemons/clients)."""
    env = os.environ.get("SUTANDO_RUNTIME_SOCKET")
    if env:
        return env
    inst = instance or instance_id()
    legacy = run_dir() / "sutando-runtime.sock"
    if inst == "default" and legacy.exists():
        return str(legacy)
    return str(instance_run_dir(inst) / "runtime.sock")


def lock_path(instance: str | None = None) -> Path:
    return instance_run_dir(instance) / "instance.lock"
