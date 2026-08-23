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

Within the run dir, live resources are scoped by the SAME (agent_id,
instance_id) tuple the instance registry keys on, using the shared encoding in
`instance_key.py`. Scoping by instance alone meant two actors the registry
listed as distinct fought over one socket and one lock.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Importable by a bare file loader (tests, CLI) as well as by the daemon.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from instance_key import encode_part  # noqa: E402


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


def agent_id() -> str | None:
    """The actor half of the runtime identity, from the same env chain the
    daemon resolves. None means no actor is declared — the pre-actor layout."""
    return (os.environ.get("SUTANDO_AGENT_ID")
            or os.environ.get("AGENT_MXID")
            or os.environ.get("AGENT_ID")
            or None)


def instance_run_dir(instance: str | None = None,
                     agent: str | None = None) -> Path:
    """Run dir for one (agent_id, instance_id) tuple. Identity here is the
    SAME tuple the instance registry keys on: scoping by instance alone let
    two actors that the registry lists as distinct collide on socket + lock."""
    inst = instance or instance_id()
    who = agent or agent_id()
    if not who:
        return run_dir() / encode_part(inst, "instance_id")
    return (run_dir() / encode_part(who, "agent_id")
            / encode_part(inst, "instance_id"))


def socket_path(instance: str | None = None,
                agent: str | None = None) -> str:
    """SUTANDO_RUNTIME_SOCKET overrides; otherwise identity-scoped
    <run dir>/[<agent>/]<instance>/runtime.sock so two instances can never
    collide. The legacy flat <run dir>/sutando-runtime.sock is still honored
    for the default instance when it already exists (pre-M2 daemons/clients)."""
    env = os.environ.get("SUTANDO_RUNTIME_SOCKET")
    if env:
        return env
    inst = instance or instance_id()
    legacy = run_dir() / "sutando-runtime.sock"
    if inst == "default" and legacy.exists():
        return str(legacy)
    return str(instance_run_dir(inst, agent) / "runtime.sock")


def lock_path(instance: str | None = None,
              agent: str | None = None) -> Path:
    return instance_run_dir(instance, agent) / "instance.lock"
