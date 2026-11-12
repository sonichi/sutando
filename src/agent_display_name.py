#!/usr/bin/env python3
"""The name this agent introduces itself by: its live s.space display name, never a persona name.

Every self-introduction (the first-contact reply in the owner's DM, the room-onboard greeting, the
tutorial's Getting started page) takes its name from `resolve()`, in this order:

1. `profile`    - the agent's current Matrix profile displayname from its homeserver: the name
                  s.space shows for it, so a rename there is picked up on the next greeting.
2. `connection` - the `agent_display_name` the gateway listed for this same agent when the
                  connection was made (<workspace>/state/connections.json), for when (1) is offline.
3. `configured` - SUTANDO_DISPLAY_NAME, else AGENT_DISPLAY_NAME (environment, else the AG2 Space
                  channel env file).
4. `localpart`  - a name made from the mxid's localpart: `@blue-fox.agent:host` gives `Blue Fox`.
5. `fallback`   - "Sutando".

A Stand persona name (stand-identity.json) or a codename is never used: it is internal, and the
people greeted know the agent by its s.space display name. The agent's mxid is $AGENT_MXID, else
the channel env file's, else the launcher-named device file or channels/ag2space/.env.

CLI (what an agent runs before introducing itself):
    python3 src/agent_display_name.py [--room ROOM] [--agent-mxid MXID] [--json]
prints the name (with --json: {"name", "source", "agent_mxid"}). It never fails. Stdlib only.
With --room, a room whose connection names no identity, or that no connection serves, resolves with
no mxid (tiers 3 and 5): another server's room never borrows the primary agent's name.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Mapping, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

FALLBACK_NAME = "Sutando"
NAME_MAX = 64
CONFIGURED_VARS = ("SUTANDO_DISPLAY_NAME", "AGENT_DISPLAY_NAME")
PROFILE_TIMEOUT = 5
USER_AGENT = "sutando-agent-name/1"
MXID = re.compile(r"^@([^:\s]+):([A-Za-z0-9.\-]+(?::\d+)?)$")

Fetch = Callable[[str], bytes]


def clean(name: object) -> str:
    """`name` single-spaced and cut to NAME_MAX; '' for anything that is not a non-blank string."""
    return " ".join(name.split())[:NAME_MAX] if isinstance(name, str) else ""


def localpart_name(mxid: str) -> str:
    """A readable name from an mxid's localpart: `@blue-fox.agent:x` -> `Blue Fox`."""
    m = MXID.match(mxid or "")
    local = re.sub(r"\.agent$", "", m.group(1)) if m else ""
    words = [w for w in re.split(r"[-._=/]+", local) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)[:NAME_MAX]


def https_get(url: str) -> bytes:
    # The homeserver's edge refuses urllib's default User-Agent with a 403.
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=PROFILE_TIMEOUT) as resp:
        return resp.read(65536)


def profile_name(mxid: str, fetch: Optional[Fetch] = None) -> str:
    """The mxid's Matrix profile displayname from its homeserver; '' on any failure."""
    m = MXID.match(mxid or "")
    if not m:
        return ""
    fetch = fetch or https_get
    server = m.group(2)
    base = f"https://{server}"
    with contextlib.suppress(Exception):
        known = json.loads(fetch(f"https://{server}/.well-known/matrix/client"))
        url = str(known["m.homeserver"]["base_url"])
        if url.startswith("https://"):
            base = url
    try:
        got = json.loads(fetch(f"{base.rstrip('/')}/_matrix/client/v3/profile/"
                               f"{urllib.parse.quote(mxid, safe='')}/displayname"))
    except Exception:  # noqa: BLE001
        return ""
    return clean(got.get("displayname")) if isinstance(got, dict) else ""


def connection_name(mxid: str, workspace: Optional[Path] = None) -> str:
    """The display name the gateway listed for `mxid` on its connection row; '' when none."""
    if not mxid:
        return ""
    try:
        import connections
        rows = connections.read_rows(workspace)
    except Exception:  # noqa: BLE001 - an unreadable record leaves the next tier
        return ""
    return next((clean(r.get("agent_display_name")) for r in rows
                 if isinstance(r, dict) and r.get("agent_mxid") == mxid and clean(r.get("agent_display_name"))), "")


def configured_name(environ: Optional[Mapping[str, str]] = None) -> str:
    """SUTANDO_DISPLAY_NAME, else AGENT_DISPLAY_NAME, from the environment or the channel env file."""
    from channel_credentials import channel_value
    for var in CONFIGURED_VARS:
        with contextlib.suppress(Exception):
            name = clean(channel_value(var, environ))
            if name:
                return name
    return ""


def agent_mxid(environ: Optional[Mapping[str, str]] = None) -> str:
    """This agent's mxid: $AGENT_MXID, else the AG2 Space channel env file the room tooling
    resolves, else the launcher-named device file or channels/ag2space/.env read directly (a file
    that holds the identity but no usable token); '' when none names one."""
    from channel_credentials import channel_value
    from channel_token import token_from_env_file
    from util_paths import claude_home_path
    env = os.environ if environ is None else environ
    found = channel_value("AGENT_MXID", env)
    if found:
        return found
    files = [Path(p) for p in ((env.get("AG2_DEVICE_ENV") or "").strip(),) if p]
    with contextlib.suppress(Exception):
        files.append(claude_home_path("channels") / "ag2space" / ".env")
    return next((v for v in (token_from_env_file("AGENT_MXID", f) for f in files if f.is_file()) if v), "")


def resolve(mxid: Optional[str] = None, *, fetch: Optional[Fetch] = None,
            environ: Optional[Mapping[str, str]] = None,
            workspace: Optional[Path] = None, offline: bool = False) -> Tuple[str, str]:
    """(name, source) for this agent's self-introduction, in the order the module docstring gives;
    `offline` skips the homeserver (tier 1), for a monitor that may not make a network call."""
    if mxid is None:
        try:
            mxid = agent_mxid(environ)
        except Exception:  # noqa: BLE001 - no identity still yields a name
            mxid = ""
    mxid = (mxid or "").strip()
    for source, name in (("profile", lambda: "" if offline else profile_name(mxid, fetch)),
                         ("connection", lambda: connection_name(mxid, workspace)),
                         ("configured", lambda: configured_name(environ)),
                         ("localpart", lambda: localpart_name(mxid))):
        try:
            got = name()
        except Exception:  # noqa: BLE001 - a failing tier leaves the next one
            got = ""
        if got:
            return got, source
    return FALLBACK_NAME, "fallback"


def _main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--room", default="", help="the room the introduction is posted in")
    ap.add_argument("--agent-mxid", default="", help="this agent's mxid, when already known")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    mxid = args.agent_mxid.strip() or None
    if mxid is None and args.room:
        try:
            from channel_credentials import room_agent_mxid
            mxid = room_agent_mxid(args.room)  # None: one connection; '': none named for this room
        except Exception:  # noqa: BLE001 - no connection serves the room: never borrow the primary's
            mxid = ""
    if mxid is None:
        with contextlib.suppress(Exception):
            mxid = agent_mxid()
    name, source = resolve(mxid or "")
    if args.json:
        print(json.dumps({"name": name, "source": source, "agent_mxid": mxid or ""}))
    else:
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
