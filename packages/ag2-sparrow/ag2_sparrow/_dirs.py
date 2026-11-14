"""Filesystem interface for the ag2-sparrow transport client: it needs exactly
a TASK_DIR (incoming tasks land here), a RESULT_DIR (results read + posted back),
and a STATE_DIR (inflight/rooms ledgers). No 'workspace' concept, no sutando deps.

Resolution (per dir): explicit env → injected resolver → default under
~/.ag2-sparrow/. Sutando injects its own (workspace/tasks, /results, /state) via
set_dirs(); any other agent just sets the env vars (or takes the defaults).

An installer that hosts one agent per work root also reads `agents_dir()` (where per-agent roots
go: injected, else ~/.ag2-sparrow/agents) and `bridge_host()` (who runs each agent's bridge: the
host's supervisor name when injected, else '' and the installer writes the bridge's own unit)."""
import os
from pathlib import Path

_DEFAULT_BASE = Path.home() / ".ag2-sparrow"
_injected = None  # dict(task/result/state -> Path) set by internal sutando


def set_dirs(task_dir=None, result_dir=None, state_dir=None, agents_dir=None, bridge_host=None):
    """Internal callers (sutando) inject concrete dirs; external uses env/defaults."""
    global _injected
    _injected = {
        "task": Path(task_dir) if task_dir else None,
        "result": Path(result_dir) if result_dir else None,
        "state": Path(state_dir) if state_dir else None,
        "agents": Path(agents_dir) if agents_dir else None,
        "bridge_host": bridge_host or "",
    }


def _resolve(kind, env_var, default):
    if _injected and _injected.get(kind):
        return _injected[kind]
    v = os.environ.get(env_var)
    return Path(v) if v else default


def task_dir():
    return _resolve("task", "AGENT_CONNECT_TASK_DIR", _DEFAULT_BASE / "task_dir")


def result_dir():
    return _resolve("result", "AGENT_CONNECT_RESULT_DIR", _DEFAULT_BASE / "result_dir")


def state_dir():
    return _resolve("state", "AGENT_CONNECT_STATE_DIR", _DEFAULT_BASE / "state")


def task_dir_configured() -> bool:
    """Whether a task dir was chosen (injected or AGENT_CONNECT_TASK_DIR), not the default."""
    return bool(_injected and _injected.get("task")) or bool(os.environ.get("AGENT_CONNECT_TASK_DIR"))


def agents_dir():
    # Read HOME per call: _DEFAULT_BASE is fixed at import.
    return (_injected or {}).get("agents") or Path.home() / ".ag2-sparrow" / "agents"


def bridge_host() -> str:
    return (_injected or {}).get("bridge_host") or ""


def device_env_path():
    """The 0600 .env file holding REMOTE_TASK_TOKEN that Sparrow reads at startup; every installer
    points its service at this path and never copies the token anywhere else."""
    env = os.environ.get("AG2_DEVICE_ENV")
    return Path(env).expanduser() if env else Path.home() / ".config" / "ag2-sparrow" / "relay.env"
