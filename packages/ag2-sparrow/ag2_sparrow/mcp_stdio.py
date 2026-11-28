"""The AG2 Space MCP server as a local agent itself, over stdio: what a connector hands its CLI so the agent's
own turns can act in rooms (post, @-mention, Room Commons) as the agent.

The desktop's `ag2-mcp-proxy` (`--mcp-proxy-command`, recorded at install as MCP_PROXY_COMMAND) runs under
`mcp_name_shim`, which lists and forwards only the tools granted for the turn. The proxy is given only the path
of a non-secret descriptor (`ag2-sparrow-mcp descriptor --env-file <the agent's token file>`, written 0600 in the
connector's state dir) naming the agent's own token file; it mints a short-lived token as the agent itself, so
the relay token never enters the CLI's environment. Shared by the Antigravity (ACP) and Codex (app-server)
connectors. Stdlib only.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import mcp_auth, mcp_name_shim

MINT_TIMEOUT_S = 30.0
# What the token exchange's child keeps of this process's (already allowlisted) environment.
MINT_ENV_KEEP = ("PATH", "HOME", "USER", "LANG", "TMPDIR")
DESCRIPTOR_NAME = "ag2-mcp-descriptor.json"
GRANT_NAME = "ag2-mcp-grant.json"
PROXY_LOG_NAME = "ag2-mcp-proxy.log"
SHIM_NAME = "ag2-mcp-shim"
STATUS_NAME = "ag2-mcp-status.json"
# What a turn that may have the tools is told (the shim's names: dots become underscores).
ROOM_TOOLS_HINT = ("When this session has the %s MCP tools, they act in AG2 Space as you, this agent: use "
                   "room_action_execute (the server's room.action.execute; find Actions with room_actions_search and "
                   "room_actions_describe) to create or edit Room Commons pages and for other room actions, and put "
                   "the page link in your reply." % mcp_auth.SERVER_NAME)


def mcp_proxy_command(value: "str | None") -> "tuple[str, str]":
    """(the stdio proxy's command line, each word quoted, '') or ('', why). Its first word must be an executable
    file: an unquoted path with a space (the desktop's engine sits under Application Support) splits there, so
    it is refused with the quoting to use rather than recorded broken."""
    if not value:
        return "", ""
    try:
        argv = shlex.split(value)
    except ValueError as e:
        return "", f"--mcp-proxy-command does not parse ({e})"
    exe = shutil.which(argv[0]) if argv else None
    if not exe or len(argv) < 2:
        return "", ("--mcp-proxy-command must name an executable and its arguments, each path quoted when it has "
                    "a space: --mcp-proxy-command \"'<node>' '<path>/ag2-mcp-proxy.mjs'\"")
    return shlex.join([os.path.abspath(exe)] + argv[1:]), ""


def mint_argv(*command):
    """`ag2-sparrow-mcp <command...>` (default `acp`) from this very package (a source tree or the zipapp),
    isolated (-I) so neither the child's directory nor PYTHONPATH can put another module first."""
    root = str(Path(mcp_auth.__file__).resolve().parents[1])
    code = ("import sys; sys.path.insert(0, %r); from ag2_sparrow.mcp_auth import main; "
            "raise SystemExit(main(%r))" % (root, list(command or ("acp",))))
    return [sys.executable, "-I", "-c", code]


def mint_env(token_env_file, agent_id, environ=None):
    """(the exchange's environment, '') or ({}, why): the agent's own token file as its bridge reads it
    (`connect_bridge.credential_env`) and MINT_ENV_KEEP, nothing else."""
    from . import connect_bridge
    cred, problem = connect_bridge.credential_env(token_env_file, agent_id)
    if problem:
        return {}, problem
    source = os.environ if environ is None else environ
    env = {k: source[k] for k in MINT_ENV_KEEP if k in source}
    env.update(cred)
    return env, ""


def _exchange(command, token_env_file, agent_id, run, extra=()):
    """(the JSON object `ag2-sparrow-mcp <command>` printed, '') or (None, why), run as `mint_env` says."""
    env, problem = mint_env(token_env_file, agent_id)
    if problem:
        return None, problem
    try:
        done = run(mint_argv(command, *extra), env=env, cwd=os.path.dirname(token_env_file) or "/", capture_output=True,
                   text=True, timeout=MINT_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "the token exchange did not run (%s)" % type(e).__name__
    if done.returncode != 0:
        lines = (done.stderr or "").strip().splitlines()
        return None, (lines[-1][:200] if lines else "the token exchange exited %s" % done.returncode)
    try:
        out = json.loads(done.stdout)
    except ValueError:
        out = None
    return (out, "") if isinstance(out, dict) else (None, "the token exchange printed no JSON object")


def shim_args(argv):
    """The arguments that run `argv` (the proxy) under `mcp_name_shim`, from this very package, isolated: agy
    drops every tool whose name has a dot, so the shim offers `room_action_execute` for `room.action.execute`."""
    root = str(Path(mcp_auth.__file__).resolve().parents[1])
    code = ("import sys; sys.path.insert(0, %r); from ag2_sparrow.mcp_name_shim import main; "
            "raise SystemExit(main(sys.argv[1:]))" % root)
    return ["-I", "-c", code, "--"] + list(argv)


def write_launcher(state, turn="", tools=frozenset()):
    """`<state>/ag2-mcp-shim` (0700): a plain executable that runs the shim, so the CLI's stdio entry is a file and
    its args are only the proxy's command line (agy started no process for a `python -I -c <code>` entry). It
    hands the shim this turn's id, `<state>/ag2-mcp-status.json`, where the shim says how far it got, and the
    server tools it may list and call (`tools`; none by default)."""
    path = os.path.join(state, SHIM_NAME)
    env = {"AG2_MCP_SHIM_TURN": turn, "AG2_MCP_SHIM_STATUS": os.path.join(state, STATUS_NAME),
           "AG2_MCP_SHIM_TOOLS": ",".join(sorted(tools))}
    text = "#!/bin/sh\n%s exec %s \"$@\"\n" % (" ".join("%s=%s" % (k, shlex.quote(v)) for k, v in env.items()),
                                              " ".join(shlex.quote(a) for a in [sys.executable] + shim_args([])))
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o700)
    os.replace(tmp, path)
    return path


def _descriptor_file(desc, token_env_file):
    """The file a descriptor must name: the .env itself, or for `token_format: "raw"` (no env_key) the raw
    token file that seat .env points at; None for any other shape."""
    if desc.get("token_format") == "raw" and "env_key" not in desc:
        return mcp_auth.seat_token_file(token_env_file) or None
    if "token_format" not in desc and isinstance(desc.get("env_key"), str):
        return os.path.realpath(token_env_file)
    return None


def stdio_entry(proxy_command, token_env_file, agent_id, state_dir, run=subprocess.run, turn="", tools=frozenset()):
    """(the ACP stdio `McpServer` running the desktop's `ag2-mcp-proxy` as this agent, '') or (None, why). The
    proxy gets only the path of a non-secret descriptor (written 0600 in `state_dir`) naming the agent's own
    token file, as Claude Code's `ag2-space` stdio entry does; the relay token never enters the CLI's environment."""
    argv = shlex.split(proxy_command or "")
    if not argv:
        return None, "no --mcp-proxy-command"
    desc, why = _exchange("descriptor", token_env_file, agent_id, run, ("--env-file", token_env_file))
    if desc is None:
        return None, why
    try:
        for k in ("mint_url", "mcp_url"):
            mcp_auth._safe_url(k, desc.get(k))
        ok = desc.get("version") == 1 and isinstance(desc.get("env_file"), str) \
            and os.path.realpath(desc["env_file"]) == _descriptor_file(desc, token_env_file)
    except mcp_auth.McpAuthError:
        ok = False
    if not ok:
        return None, "the token exchange printed no proxy descriptor"
    if isinstance(agent_id, str) and agent_id.startswith("@") and ":" in agent_id:
        desc["agent_id"] = agent_id   # who a re-mint must turn out to be
    state = os.path.abspath(state_dir)
    path = os.path.join(state, DESCRIPTOR_NAME)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(desc, f)
    os.replace(tmp, path)
    return {"name": mcp_auth.SERVER_NAME, "command": write_launcher(state, turn, tools), "args": list(argv),
            "env": [{"name": "AG2_MCP_DESCRIPTOR", "value": path},
                    {"name": "AG2_MCP_LOG", "value": os.path.join(state, PROXY_LOG_NAME)}]}, ""


def grant_env(state_dir, launch):
    """The env that makes a shim forward calls only under a live per-turn grant (`write_grant`) naming `launch`."""
    return {mcp_name_shim.GRANT_ENV: os.path.join(os.path.abspath(state_dir), GRANT_NAME),
            mcp_name_shim.LAUNCH_ENV: launch}


def launch_of(config):
    """The launch id a thread config's ag2-space entry carries (`grant_env`), or ''."""
    try:
        launch = config["mcp_servers"][mcp_auth.SERVER_NAME]["env"][mcp_name_shim.LAUNCH_ENV]
    except (KeyError, TypeError):
        return ""
    return launch if isinstance(launch, str) else ""


def write_grant(state_dir, launch, tier, tools, ttl, now=time.time):
    """Write `<state>/ag2-mcp-grant.json` (0600) for the turn about to start: the launch whose shim may forward
    `tools` until `ttl` seconds from now. Returns the grant's own turn id."""
    turn = uuid.uuid4().hex
    path = os.path.join(os.path.abspath(state_dir), GRANT_NAME)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"launch": launch, "turn": turn, "tier": tier, "tools": sorted(tools), "expires": now() + ttl}, f)
    os.replace(tmp, path)
    return turn


def retire_grant(state_dir):
    """Remove the turn's grant: from here no shim forwards a call until the next turn's is written."""
    try:
        os.unlink(os.path.join(os.path.abspath(state_dir), GRANT_NAME))
    except FileNotFoundError:
        pass
