#!/usr/bin/env python3
"""Register the AG2 Space MCP server for this Sutando install, without the desktop app.

Finds the agent credential in the ag2space channel env, asks the relay for the mint and
MCP URLs (GET <relay>/v1/mcp/discovery), writes the proxy's descriptor (no secret in it),
then prints, or with --apply runs, the command that adds the `ag2-space` stdio server.

  python3 skills/ag2-space-mcp/scripts/register.py            # dry run: show what it would do
  python3 skills/ag2-space-mcp/scripts/register.py --apply    # Claude Code: claude mcp add-json
  python3 skills/ag2-space-mcp/scripts/register.py --runtime codex   # print the config.toml entry
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path
from shutil import which
from subprocess import run
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SKILL_DIR = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
REPO = SKILL_DIR.parent.parent
sys.path.insert(0, str(REPO / "src"))

from channel_env_resolve import resolve_channel_env  # noqa: E402
from channel_token import RELAY_TOKEN_VARS, token_from_env_file  # noqa: E402

from util_paths import claude_home_path  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

SERVER_KEY = "ag2-space"
SOURCE = "ag2space"
PROXY = SKILL_DIR / "ag2-mcp-proxy.mjs"
MANIFEST = SKILL_DIR / "manifest.json"
PATH_SETTINGS = ("AG2_MCP_DESCRIPTOR", "AG2_MCP_LOG", "AG2_MCP_ROOM_ACTIONS")
USER_AGENT = "sutando-ag2-space-mcp-register/1"
LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


class RegisterError(Exception):
    pass


def find_credential(env_file: Path) -> Tuple[str, str, str]:
    """(env_key, relay_url, secret) from the env file; the proxy re-reads the key itself."""
    for key in RELAY_TOKEN_VARS:
        value = token_from_env_file(key, env_file)
        if not value:
            continue
        relay, sep, secret = value.partition("|")
        if not sep:
            relay, secret = token_from_env_file("REMOTE_TASK_URL", env_file), value
        if not relay or not secret:
            raise RegisterError(f"{key} in {env_file} has no relay URL; save it as '<relay-url>|<secret>'")
        return key, relay, secret
    raise RegisterError(f"no {' / '.join(RELAY_TOKEN_VARS)} in {env_file}")


def check_url(field: str, value: str) -> str:
    u = urlparse(value or "")
    if u.scheme == "https" or (u.scheme == "http" and u.hostname in LOOPBACK):
        return value
    raise RegisterError(f"discovery returned a {field} that is not https: {value!r}")


def http_discover(relay: str, secret: str) -> dict:
    req = Request(relay.rstrip("/") + "/v1/mcp/discovery",
                  headers={"Authorization": f"Bearer {secret}", "User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RegisterError("this relay has no hosted MCP (discovery not configured)") from None
        if e.code == 401:
            raise RegisterError("the relay refused the agent credential; reconnect the agent") from None
        raise RegisterError(f"discovery failed: HTTP {e.code}") from None


def build_descriptor(env_file: Path, discover: Callable[[str, str], dict]) -> dict:
    env_key, relay, secret = find_credential(env_file)
    urls = discover(relay, secret)
    return {"version": 1, "lane": "primary", "env_file": str(env_file), "env_key": env_key,
            "mint_url": check_url("mint_url", urls.get("mint_url")),
            "mcp_url": check_url("mcp_url", urls.get("mcp_url")),
            "ca_bundle": None, "agent_id": None}


def write_descriptor(path: Path, descriptor: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(descriptor, indent=2) + "\n")
    os.replace(tmp, path)


def settings(workspace: Path, environ=None) -> dict:
    """Env override > manifest config default; a relative value is under the workspace."""
    env = os.environ if environ is None else environ
    declared = json.loads(MANIFEST.read_text()).get("config", {})
    out = {}
    for key in PATH_SETTINGS:
        value = env.get(key) or declared.get(key)
        if not value:
            raise RegisterError(f"{key} is neither set nor declared in {MANIFEST.name}")
        path = Path(value).expanduser()
        out[key] = path if path.is_absolute() else workspace / path
    return out


def loose_permissions(path: Path) -> bool:
    return bool(os.stat(path).st_mode & 0o077)


def server_entry(node: str, paths: dict) -> dict:
    return {"type": "stdio", "command": node, "args": [str(PROXY)],
            "env": {k: str(paths[k]) for k in PATH_SETTINGS}}


def codex_toml(entry: dict) -> str:
    env = ", ".join(f"{k} = {json.dumps(v)}" for k, v in entry["env"].items())
    return (f"[mcp_servers.{SERVER_KEY}]\ncommand = {json.dumps(entry['command'])}\n"
            f"args = {json.dumps(entry['args'])}\nenv = {{ {env} }}\n")


def main(argv: Optional[list] = None, discover: Callable[[str, str], dict] = http_discover) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runtime", choices=("claude", "codex"), default="claude")
    ap.add_argument("--apply", action="store_true", help="run `claude mcp add-json` (Claude Code only)")
    ap.add_argument("--env-file", help="the env file holding the agent credential (default: the ag2space channel)")
    args = ap.parse_args(argv)

    env_file = Path(args.env_file) if args.env_file else resolve_channel_env(claude_home_path("channels"), SOURCE)
    if env_file is None:
        print(f"no agent credential found under {claude_home_path('channels', SOURCE)}. Connect an agent in "
              "AG2 Space, then save its token there as REMOTE_TASK_TOKEN='<relay-url>|<secret>'.", file=sys.stderr)
        return 1
    node = which("node")
    if not node:
        print("node is not on PATH; the proxy needs Node 18 or newer", file=sys.stderr)
        return 1
    workspace = Path(resolve_workspace())
    try:
        paths = settings(workspace)
        descriptor = build_descriptor(env_file, discover)
    except RegisterError as e:
        print(f"register: {e}", file=sys.stderr)
        return 1
    descriptor_path = paths["AG2_MCP_DESCRIPTOR"]
    write_descriptor(descriptor_path, descriptor)
    if loose_permissions(env_file):
        print(f"warning: {env_file} is accessible by other users; run chmod 600 on it", file=sys.stderr)
    print(f"descriptor: {descriptor_path} (mcp {descriptor['mcp_url']}, key {descriptor['env_key']} "
          f"in {env_file}; the secret stays there)", flush=True)

    entry = server_entry(node, paths)
    if args.runtime == "codex":
        print("add this to $CODEX_HOME/config.toml:\n\n" + codex_toml(entry))
        return 0
    cmd = ["claude", "mcp", "add-json", "--scope", "user", SERVER_KEY, json.dumps(entry)]
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(claude_home_path()))
    if not args.apply:
        print(f"dry run; with --apply this runs (CLAUDE_CONFIG_DIR={env['CLAUDE_CONFIG_DIR']}):\n  "
              + " ".join(cmd[:6]) + " '<entry>'\n" + json.dumps(entry, indent=2))
        return 0
    if not which("claude"):
        print("claude is not on PATH", file=sys.stderr)
        return 1
    done = run(cmd, env=env)
    if done.returncode == 0:
        print("registered. Restart the core, then call the ag2.whoami tool to check.")
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
