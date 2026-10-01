"""Where a task-progress update may be delivered: one provider-neutral verdict.

`delivery_route(source, channel)` returns "builtin" (Slack/Discord/Telegram),
"gateway" (a Matrix room id the remote gateway posts into), or None when the
task has no delivery path. The source decides only the built-in senders; any
other source, known or not, routes to the gateway iff its channel is a valid
Matrix room id (strict `!opaque:server`, or a v12 id). Everything else, e.g.
local-voice, runtime-api, onboarding-wizard, gets None whatever config exists.
Accepted trade-off: provider labels are install-configured, so a future writer
carrying a real room id will send. Callers: the task-progress
skill's notify.py and step.py, and core-supervisor-relay.py. A None verdict is
never a delivery; senders exit NO_ROUTE_EXIT so a caller cannot read it as one.
"""
from __future__ import annotations

import re

BUILTIN_SENDERS = frozenset({"slack", "discord", "telegram"})

# `!opaque:server`, or a room v12 id: `!` + unpadded base64url SHA-256 (43 chars).
GATEWAY_ROOM_RE = re.compile(r"!(?:[^:\s]+:\S+|[A-Za-z0-9_-]{43})")

# Distinct from 1 (a send that failed): nothing was sent because nothing may be.
NO_ROUTE_EXIT = 3


def delivery_route(source: "str | None", channel: "str | None") -> "str | None":
    if source in BUILTIN_SENDERS:
        return "builtin"
    if channel and GATEWAY_ROOM_RE.fullmatch(channel):
        return "gateway"
    return None


def no_route_message(source: "str | None", channel: "str | None") -> str:
    return f"[task-progress] {source!r} / {channel!r} has no delivery path; nothing to send"
