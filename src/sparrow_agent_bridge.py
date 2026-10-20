#!/usr/bin/env python3
"""One agent's gateway bridge under sparrowd, from the declaration its claude-local install wrote.

A declaration is data the agent's own session could write, so it chooses nothing: everything the
bridge runs with is derived from the declaration's path segment (`<agents>/<segment>.bridge/`) by the
adapter's own functions, and the file is only checked to agree with it. The worker name, the vault
key, the token variable and the exact env (three dirs plus `AGENT_MXID`, whose id must map to the same
segment) are each compared; any difference skips the declaration with one reason line.

`declarations(workspace, vault_keys)` lists the valid ones whose vault key exists. What a declaration's
own bytes can be (not UTF-8, not JSON, too deep, too large, not a regular file) skips that one entry with a
reason; so does a declaration file that exists but cannot be read, whose worker the supervisor keeps as it
is (`held`). An agents directory that cannot be listed or a home that cannot be entered is `ScanUnavailable`
(nothing is known), never "nothing declared": a supervisor keeps what runs until the scan can be made
again. Each declaration carries the digest of its bytes, so an edited one is a changed worker. Run as a script with
a declaration path, it re-checks it, builds the bridge's environment (sparrowd's own, minus the host's
token, identity and owner-DM variables, plus the derived env and the vault token, never on disk),
injects the agent's dirs and the workspace's task stamper, and runs the package bridge in this process.
"""
from __future__ import annotations

import hashlib
import json
import os
import runpy
import stat
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
_REPO = _SRC.parent
for _p in (str(_SRC), str(_REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ag2_sparrow import agent_identity  # noqa: E402
from ag2_sparrow.adapters.claude_code import cli as adapter  # noqa: E402

WRITTEN_BY = f"{adapter.COMMAND} install"
BRIDGE_MODULE = "ag2_sparrow.remote_gateway_bridge"
TOKEN_ENV = "REMOTE_TASK_TOKEN"
SUFFIX = ".bridge"
# A declaration is a few hundred bytes; anything past this is not one and is never read into memory.
MAX_DECLARATION_BYTES = 64 * 1024
# Never inherited from sparrowd: the host's token files, its agent identity and its owner DM.
HOST_ONLY = ("AG2_DEVICE_ENV", "REMOTE_TASK_TOKEN_FILE", TOKEN_ENV, "AGENT_MXID", "AGENT_ID", "AG2SPACE_USER_ID",
             "GATEWAY_INSTANCE", "REMOTE_PROACTIVE_ROOM", "AGENT_CONNECT_TASK_DIR", "AGENT_CONNECT_RESULT_DIR",
             "AGENT_CONNECT_STATE_DIR")


class ScanUnavailable(Exception):
    """The declarations (or what they need) could not be read: not an empty set, an unknown one."""


class DeclarationUnreadable(ScanUnavailable):
    """One declaration file exists but cannot be read: unknown for that entry only."""


def _stat(path: Path):
    """The stat, None when absent; any other failure to look is ScanUnavailable."""
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ScanUnavailable(f"{path}: unreadable ({type(e).__name__})") from e


def _resolved(env: dict) -> dict:
    return {k: str(Path(v).resolve()) if k.startswith("AGENT_CONNECT_") else v for k, v in env.items()}


def load(path: Path, agents: Path) -> "tuple[dict | None, str]":
    """(the derived declaration, '') or (None, why it is not run). The returned name, vault key and env
    come from the path's segment, never from the file."""
    path = Path(path)
    home = path.parent
    if path.name != adapter.DECLARATION or not home.name.endswith(SUFFIX) or \
            home.resolve().parent != Path(agents).resolve():
        return None, f"{path}: not a declaration directly under {agents}"
    segment = home.name[:-len(SUFFIX)]
    # What the file's own bytes can do is a skip with a reason; only a failure to look is unavailable.
    st = _stat(path)
    if st is None:
        return None, f"{path}: absent"
    if not stat.S_ISREG(st.st_mode):
        return None, f"{path}: not a regular file"
    if st.st_size > MAX_DECLARATION_BYTES:
        return None, f"{path}: {st.st_size} bytes is not a declaration (cap {MAX_DECLARATION_BYTES})"
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise DeclarationUnreadable(f"{path}: unreadable ({type(e).__name__})") from e
    try:
        decl = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:  # UnicodeDecodeError is a ValueError
        return None, f"{path}: malformed ({type(e).__name__})"
    if not isinstance(decl, dict) or decl.get("written_by") != WRITTEN_BY:
        return None, f"{path}: not written by {WRITTEN_BY}"
    env = decl.get("env")
    mxid = env.get("AGENT_MXID") if isinstance(env, dict) else None
    try:
        # The bridge reports the id verbatim, so it must be exact: no surrounding whitespace to strip.
        own = isinstance(mxid, str) and mxid == mxid.strip() and agent_identity.root_segment(mxid) == segment
    except ValueError:
        own = False
    if not own:
        return None, f"{path}: AGENT_MXID {mxid!r} is not the agent the segment {segment} names"
    root = str(Path(agents).resolve() / segment)
    want = {"name": adapter.worker_name(segment), "token_vault_key": adapter.vault_key(segment),
            "token_env": TOKEN_ENV, "module": BRIDGE_MODULE, "env": adapter.bridge_env(root, mxid)}
    for key in ("name", "token_vault_key", "token_env", "module"):
        if decl.get(key) != want[key]:
            return None, f"{path}: {key} {decl.get(key)!r} is not the derived {want[key]!r}"
    if sorted(env) != sorted(want["env"]):
        extra = sorted(set(env) ^ set(want["env"]))
        return None, f"{path}: env keys differ from the bridge's own ({', '.join(map(str, extra))})"
    try:
        same = _resolved(env) == _resolved(want["env"])
    except (TypeError, OSError, ValueError):
        same = False
    if not same:
        return None, f"{path}: env is not the canonical one for {segment}"
    return dict(want, digest=hashlib.sha256(raw).hexdigest()), ""


def bridge_environ(decl: dict, token: str, base=None) -> dict:
    """What the bridge runs under: sparrowd's environment minus the host-only variables, plus the derived
    env, an empty owner DM and the agent's token."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if k not in HOST_ONLY}
    env.update(decl["env"], REMOTE_PROACTIVE_ROOM="")
    env[TOKEN_ENV] = token
    return env


def declarations(workspace: Path, vault_keys, held=None) -> "tuple[list, list]":
    """([(path, declaration)], [reason]) for the agent bridges under `workspace`. An absent agents
    directory declares nothing; one that cannot be listed, or a home that cannot be entered, raises
    ScanUnavailable. A declaration file that cannot be read is a skip whose worker name is appended to
    `held`: its state is unknown, so the caller keeps whatever runs under that name."""
    agents, found, skipped = Path(workspace) / "agents", [], []
    names = set(vault_keys)
    try:
        homes = sorted(p for p in agents.iterdir() if p.name.endswith(SUFFIX))
    except FileNotFoundError:
        return [], []
    except OSError as e:
        raise ScanUnavailable(f"{agents}: unreadable ({type(e).__name__})") from e
    for home in homes:
        path = home / adapter.DECLARATION
        if _stat(path) is None:
            continue
        try:
            decl, why = load(path, agents)
        except DeclarationUnreadable as e:
            decl, why = None, str(e)
            if held is not None:
                held.append(adapter.worker_name(home.name[:-len(SUFFIX)]))
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
    try:
        decl, why = load(Path(args[0]), Path(workspace) / "agents")
    except ScanUnavailable as e:
        decl, why = None, str(e)
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
    environ = bridge_environ(decl, token)
    os.environ.clear()
    os.environ.update(environ)
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
