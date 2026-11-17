#!/usr/bin/env python3
"""Register the AG2 Space MCP server for this Sutando install, without the desktop app.

ag2-sparrow-mcp (packages/ag2-sparrow/ag2_sparrow/mcp_auth.py) owns discovery, the mint
rules and the token helper. This finds the core's agent credential and adds that entry.

  python3 skills/ag2-space-mcp/scripts/register.py           # show the entry and the command
  python3 skills/ag2-space-mcp/scripts/register.py --apply   # run claude mcp add-json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from shutil import which
from subprocess import run
from typing import Callable, Optional

SKILL_DIR = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
REPO = SKILL_DIR.parent.parent
SPARROW = REPO / "packages" / "ag2-sparrow"
sys.path.insert(0, str(REPO / "src"))

from channel_env_resolve import resolve_channel_env  # noqa: E402

from util_paths import claude_home_path  # noqa: E402

SOURCE = "ag2space"
# Credential selectors the bridge would read before AG2_DEVICE_ENV; cleared so the entry reflects the file.
_SHADOWING = ("REMOTE_TASK_TOKEN", "REMOTE_TASK_URL", "REMOTE_TASK_TOKEN_FILE", "AG2_REMOTE_TOKEN")


def load_mcp_auth(env_file: Path):
    """ag2_sparrow.mcp_auth from this checkout, resolving the credential from `env_file` alone."""
    os.environ["AG2_DEVICE_ENV"] = str(env_file)
    for var in _SHADOWING:
        os.environ.pop(var, None)
    if str(SPARROW) not in sys.path:
        sys.path.insert(0, str(SPARROW))
    from ag2_sparrow import mcp_auth
    return mcp_auth


def loose_permissions(path: Path) -> bool:
    return bool(os.stat(path).st_mode & 0o077)


def main(argv: Optional[list] = None, load: Callable[[Path], object] = load_mcp_auth) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="run `claude mcp add-json` (Claude Code only)")
    ap.add_argument("--env-file", help="the env file holding the agent credential (default: the ag2space channel)")
    args = ap.parse_args(argv)
    caller_env = dict(os.environ)  # load() narrows os.environ for resolution; claude gets the caller's

    env_file = Path(args.env_file) if args.env_file else resolve_channel_env(claude_home_path("channels"), SOURCE)
    if env_file is None:
        print(f"no agent credential found under {claude_home_path('channels', SOURCE)}. Connect an agent in "
              "AG2 Space, then save its token there as REMOTE_TASK_TOKEN='<relay-url>|<secret>'.", file=sys.stderr)
        return 1
    if loose_permissions(env_file):
        print(f"warning: {env_file} is accessible by other users; run chmod 600 on it", file=sys.stderr)
    mcp_auth = load(env_file)
    try:
        entry = mcp_auth.setup_entry(mcp_auth.resolve_gateway(), str(SPARROW))
    except mcp_auth.McpAuthError as e:
        print(f"register: {e}", file=sys.stderr)
        return 1
    cmd = ["claude", "mcp", "add-json", "--scope", "user", mcp_auth.SERVER_NAME, json.dumps(entry)]
    # CLAUDE_CONFIG_DIR passes through only when the caller already set it: no default is invented.
    print(f"entry: {entry['type']} {entry['url']} (credential stays in {env_file}; a helper mints per connect)")
    if not args.apply:
        print("not registered yet; with --apply this runs:\n  " + " ".join(cmd[:6]) + " '<entry>'\n"
              + json.dumps(entry, indent=2))
        return 0
    if not which("claude"):
        print("claude is not on PATH", file=sys.stderr)
        return 1
    done = run(cmd, env=caller_env)
    if done.returncode == 0:
        print("registered. Restart the core, then call the ag2.whoami tool to check.")
    else:
        print(f"claude mcp add-json failed; if {mcp_auth.SERVER_NAME} is already registered, run "
              f"`claude mcp remove --scope user {mcp_auth.SERVER_NAME}` first", file=sys.stderr)
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
