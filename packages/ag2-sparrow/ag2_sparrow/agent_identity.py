"""The AG2 Space agent a bridge serves, and the one path segment a per-agent work root is named by.

`resolve` is the bridge's identity chain, in its order: the process env (`AGENT_MXID`, `AGENT_ID`),
then the channel .env files (`AGENT_MXID`, `AGENT_ID`, `AG2SPACE_USER_ID`), then the durable binding
`<state>/auth/ag2space.json`'s `agent_id`, which a seat (strict) never reads. The gateway bridge and the installers read it here, so an
install derives its root from exactly the identity the bridge will report. A launcher that has
decided the identity sets `PINNED_KEY` (see `launch_environ`): the chain is then `AGENT_MXID` alone,
so the bridge runs as the launcher's saved identity or as none, never one a file or binding supplies.
A .env file's lines are parsed by the credential owner, gateway_credential_sources. Stdlib only.

`root_segment` maps an id to one path segment, lowercase `[a-z0-9._-]`, no leading dot, at most
`SEGMENT_MAX` bytes. A plain MXID `@local:server` whose parts are lowercase `[a-z0-9.-]` (first
character alphanumeric) becomes `local_server`: exactly one `_`, so the id reads back from it. Any
other id becomes a readable prefix plus `__` and 12 hex of its sha256, so it never equals a plain
one (which has no `__`) and two such ids differ by their digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from . import gateway_credential_sources as _sources

ENV_KEYS = ("AGENT_MXID", "AGENT_ID")
FILE_KEYS = ("AGENT_MXID", "AGENT_ID", "AG2SPACE_USER_ID")
PINNED_KEY = "AG2_IDENTITY_PINNED"
SEGMENT_MAX = 64
DIGEST_HEX = 12
_PLAIN = re.compile(r"@([a-z0-9][a-z0-9.-]*):([a-z0-9][a-z0-9.-]*)")
_UNSAFE = re.compile(r"[^a-z0-9._-]")


# Parsing has one owner, so the identity tiers cannot disagree with the credential tiers.
unquote_env = _sources.unquote
file_value = _sources.config_from_files


def env_files(paths) -> list:
    """Readable .env files in the given order, as [(path, {key: value})]; unreadable ones are skipped."""
    out = []
    for path in paths:
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        out.append((path, _sources.parse_env_text(text)))
    return out


def enrolled(state_dir) -> str:
    """The binding's `agent_id`, '' when absent, unreadable, malformed or not a string."""
    if state_dir is None:
        return ""
    try:
        rec = json.loads((Path(state_dir) / "auth" / "ag2space.json").read_text())
        v = rec.get("agent_id")
        return v.strip() if isinstance(v, str) else ""
    except Exception:
        return ""


def pinned_environ(mxid: str) -> dict:
    """The variables that pin a bridge's identity to `mxid`, or to none when `mxid` is ''."""
    return {PINNED_KEY: "1", **({"AGENT_MXID": mxid} if mxid else {})}


def launch_environ(base, mxid: str) -> dict:
    """`base` with its identity variables replaced by `pinned_environ(mxid)`, for a launched bridge."""
    env = {k: v for k, v in base.items() if k not in ENV_KEYS and k != PINNED_KEY}
    env.update(pinned_environ(mxid))
    return env


def resolve(state_dir, channel_value, environ=None, strict=None) -> str:
    """The agent id by the bridge's chain; `channel_value(key)` reads the channel .env tier. '' = unknown.
    A pinned environment answers from its own `AGENT_MXID` alone. `strict` (default: the environment is a
    seat's) stops at the channel tier: the host's binding is the host's agent, never a seat's."""
    env = os.environ if environ is None else environ
    if strict is None:
        strict = _sources.is_seat(env)
    if env.get(PINNED_KEY):
        return (env.get("AGENT_MXID") or "").strip()
    for key in ENV_KEYS:
        v = (env.get(key) or "").strip()
        if v:
            return v
    for key in FILE_KEYS:
        v = (channel_value(key) or "").strip()
        if v:
            return v
    return "" if strict else enrolled(state_dir)


def root_segment(agent_id: str) -> str:
    """The one path segment for `agent_id`; ValueError on an empty id."""
    raw = (agent_id or "").strip()
    if not raw:
        raise ValueError("the agent id is empty")
    m = _PLAIN.fullmatch(raw)
    if m:
        plain = f"{m.group(1)}_{m.group(2)}"
        if len(plain) <= SEGMENT_MAX:
            return plain
    readable = _UNSAFE.sub("-", raw.lower().lstrip("@").replace(":", "_")).lstrip("._-")
    readable = readable[:SEGMENT_MAX - DIGEST_HEX - 2].rstrip("._-") or "agent"
    return f"{readable}__{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:DIGEST_HEX]}"
