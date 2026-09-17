"""Canonical run-dir + runtime-socket resolution — the ONE definition shared
by the daemon (server.py), the CLI (src/runtime-cli/sutando-runtime.py) and
the shell descriptor (scripts/sutando-config.sh, which execs this module).

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
instance_id) tuple the instance registry keys on, using the shared bounded
encoding in `instance_key.py`. Two rules keep every consumer on one locator:

  * the ACTOR CHAIN lives here (`agent_id()`), not in each consumer. The
    daemon resolved env → enrolled record → `local-agent` while the CLI knew
    only env and the shell published the pre-actor flat socket, so a fresh
    daemon was unreachable from its own canonical CLI (review P1).
  * the socket and the lock come from the SAME `instance_run_dir()`. When
    they were resolved independently, two actors could be handed one socket
    while holding two different locks.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Importable by a bare file loader (tests, CLI) as well as by the daemon.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from instance_key import (DEFAULT_INSTANCE, TRUNC, bound,  # noqa: E402
                          instance_key)

DEFAULT_ACTOR = "local-agent"

# The actor half's env precedence, in order, first NON-EMPTY wins. Exported
# because a consumer reading another process's identity needs the same list.
ACTOR_ENV_NAMES = ("SUTANDO_AGENT_ID", "AGENT_MXID", "AGENT_ID")
SOCK_NAME = "runtime.sock"
# AF_UNIX sun_path is 104 bytes on darwin/BSD and 108 on Linux, NUL included.
SUN_PATH_MAX = 103 if sys.platform == "darwin" else 107


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


def runtime_state_dir() -> Path:
    """The daemon's state dir. Shared with server.py so the actor chain below
    reads the same enrolled record the daemon itself resolves."""
    env = os.environ.get("SUTANDO_RUNTIME_STATE")
    if env:
        return Path(env)
    sys.path.insert(0, str(_HERE.parent))  # src/
    from workspace_default import resolve_workspace  # noqa: PLC0415
    return Path(resolve_workspace()) / "state"


def instance_id() -> str:
    """The instance this process belongs to. "default" preserves the
    single-instance world; a second instance sets SUTANDO_INSTANCE_ID and
    every runtime resource scopes under it (isolation spec V1)."""
    return os.environ.get("SUTANDO_INSTANCE_ID") or DEFAULT_INSTANCE


def enrolled_agent_id(state_dir) -> str | None:
    """The agent id this install enrolled as, or None. Absent, unreadable and
    malformed records are all "not enrolled" — never a hard failure."""
    if not state_dir:
        return None
    try:
        rec = json.loads((Path(state_dir) / "auth" / "ag2space.json").read_text())
        return (rec.get("agent_id") or "").strip() or None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def agent_id(state_dir=None) -> str:
    """The actor half of the runtime identity: env → enrolled record →
    DEFAULT_ACTOR. Every consumer MUST come through here — a consumer that
    stops one link short of another resolves a different socket."""
    env = next((v for v in (os.environ.get(n) for n in ACTOR_ENV_NAMES) if v), None)
    if env:
        return env
    if state_dir is None:
        try:
            state_dir = runtime_state_dir()
        except Exception:  # noqa: BLE001 — no workspace ≠ no runtime identity
            return DEFAULT_ACTOR
    return enrolled_agent_id(state_dir) or DEFAULT_ACTOR


_MIN_DIR_BYTES = len(TRUNC) + 16  # a bare digest, the shortest bounded form


def instance_run_dir(instance: str | None = None,
                     agent: str | None = None) -> Path:
    """The ONE directory holding both the socket and the lock for a single
    (agent_id, instance_id) tuple, named by the same bounded composite key the
    registry files under and re-bounded so the socket inside it fits sun_path.
    A run dir too long to leave any budget still yields a directory — the lock
    has no such cap, and the socket may well be overridden."""
    key = instance_key(agent or agent_id(), instance or instance_id())
    root = run_dir()
    budget = SUN_PATH_MAX - len(str(root).encode()) - len(SOCK_NAME) - 2
    return root / bound(key, max(budget, _MIN_DIR_BYTES))


def legacy_socket() -> Path:
    """The pre-actor flat socket: ONE per run dir, so it cannot represent more
    than one actor."""
    return run_dir() / "sutando-runtime.sock"


def socket_path(instance: str | None = None,
                agent: str | None = None) -> str:
    """SUTANDO_RUNTIME_SOCKET overrides; otherwise
    <run dir>/<bounded (agent, instance) key>/runtime.sock."""
    env = os.environ.get("SUTANDO_RUNTIME_SOCKET")
    if env:
        return env
    inst = instance or instance_id()
    who = agent or agent_id()
    legacy = legacy_socket()
    # Upgrade path, actor-safe: the flat socket predates actor scoping, so it
    # is honored ONLY for the undeclared actor — never shared between two.
    if inst == DEFAULT_INSTANCE and who == DEFAULT_ACTOR and legacy.exists():
        return str(legacy)
    sock = str(instance_run_dir(inst, who) / SOCK_NAME)
    if len(sock.encode()) > SUN_PATH_MAX:
        raise ValueError(
            f"run dir {run_dir()} leaves no room for an AF_UNIX socket path "
            f"(cap {SUN_PATH_MAX}) — set SUTANDO_RUN_DIR to a shorter directory "
            f"or SUTANDO_RUNTIME_SOCKET to an explicit path")
    return sock


def lock_path(instance: str | None = None,
              agent: str | None = None) -> Path:
    return instance_run_dir(instance, agent) / "instance.lock"


if __name__ == "__main__":  # the shell resolver execs this — one policy, no copy
    _what = sys.argv[1] if len(sys.argv) > 1 else "--socket"
    _emit = {"--socket": socket_path, "--run-dir": run_dir,
             "--agent-id": agent_id, "--instance-dir": instance_run_dir}
    if _what not in _emit:
        raise SystemExit(f"usage: {sys.argv[0]} [{'|'.join(_emit)}]")
    sys.stdout.write(str(_emit[_what]()))
