#!/usr/bin/env python3
"""One agent's gateway bridge under sparrowd, from the declaration its claude-local install wrote.

`declarations(workspace, vault_keys)` reads every `<workspace>/agents/*.bridge/worker.json` and keeps
only a marked, well-formed one whose dirs sit under `<workspace>/agents` and whose vault key exists,
with one reason line for each one skipped. Run as a script with a declaration path, it re-checks that
declaration, puts the token from the vault into the bridge's env (never on disk), injects the agent's
own dirs and the workspace's task stamper, and runs the package bridge in this process.
"""
from __future__ import annotations

import json
import os
import re
import runpy
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
_REPO = _SRC.parent
for _p in (str(_SRC), str(_REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

WRITTEN_BY = "ag2-sparrow-claude-local install"
BRIDGE_MODULE = "ag2_sparrow.remote_gateway_bridge"
DIR_KEYS = ("AGENT_CONNECT_TASK_DIR", "AGENT_CONNECT_RESULT_DIR", "AGENT_CONNECT_STATE_DIR")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def load(path: Path, agents: Path) -> "tuple[dict | None, str]":
    """(declaration, '') or (None, why it is not run)."""
    try:
        decl = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"{path}: unreadable or malformed ({type(e).__name__})"
    if not isinstance(decl, dict) or decl.get("written_by") != WRITTEN_BY:
        return None, f"{path}: not written by {WRITTEN_BY}"
    name, env = decl.get("name"), decl.get("env")
    if not isinstance(name, str) or not _NAME.match(name):
        return None, f"{path}: the worker name is missing or not a name"
    if decl.get("module") != BRIDGE_MODULE:
        return None, f"{path}: names a module other than the bridge"
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        return None, f"{path}: env is not a string map"
    root = Path(agents).resolve()
    for key in DIR_KEYS:
        if not env.get(key) or not Path(env[key]).resolve().is_relative_to(root):
            return None, f"{path}: {key} points outside {root}"
    if not all(isinstance(decl.get(k), str) and _ENV_NAME.match(decl[k]) for k in ("token_vault_key", "token_env")):
        return None, f"{path}: the vault key or the token variable is not a name"
    return decl, ""


def declarations(workspace: Path, vault_keys) -> "tuple[list, list]":
    """([(path, declaration)], [reason]) for the agent bridges under `workspace`."""
    agents, found, skipped = Path(workspace) / "agents", [], []
    names = set(vault_keys)
    for path in sorted(agents.glob("*.bridge/worker.json")):
        decl, why = load(path, agents)
        if decl is not None and decl["token_vault_key"] not in names:
            decl, why = None, f"{path}: the vault has no {decl['token_vault_key']}; vault set it to the agent's relay token"
        if decl is None:
            skipped.append(why)
        else:
            found.append((path, decl))
    return found, skipped


def main(argv=None, workspace=None, get_key=None, run=runpy.run_module) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: sparrow_agent_bridge.py <workspace>/agents/<agent>.bridge/worker.json", file=sys.stderr)
        return 2
    if workspace is None:
        from workspace_default import resolve_workspace
        workspace = resolve_workspace()
    decl, why = load(Path(args[0]), Path(workspace) / "agents")
    if decl is None:
        print(f"sparrow-agent-bridge: not starting: {why}", file=sys.stderr)
        return 2
    if get_key is None:
        from vault_intercept import get_vault_key as get_key
    try:
        token = get_key(decl["token_vault_key"])
    except KeyError:
        print(f"sparrow-agent-bridge: not starting: the vault has no {decl['token_vault_key']}", file=sys.stderr)
        return 1
    # The agent's own token and id only: no host token file may shadow them, and nudges stay the core's.
    for key in ("AG2_DEVICE_ENV", "REMOTE_TASK_TOKEN_FILE"):
        os.environ.pop(key, None)
    os.environ.update(decl["env"], REMOTE_PROACTIVE_ROOM="")
    os.environ[decl["token_env"]] = token
    from ag2_sparrow._dirs import set_dirs
    from ag2_sparrow.local_task_protocol import set_task_stamper
    from task_envelope import stamp_text
    set_dirs(task_dir=decl["env"]["AGENT_CONNECT_TASK_DIR"], result_dir=decl["env"]["AGENT_CONNECT_RESULT_DIR"],
             state_dir=decl["env"]["AGENT_CONNECT_STATE_DIR"])
    set_task_stamper(stamp_text)
    run(BRIDGE_MODULE, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
