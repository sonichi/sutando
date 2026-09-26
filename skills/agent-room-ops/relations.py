#!/usr/bin/env python3
# DEPRECATED: room ops is being replaced by the AG2 Space MCP; use its room Actions.
# Kept only as the fallback when the MCP is unreachable (see SKILL.md).
"""room-ops · relations — the op:message field that cites the message replied to.

`say` and `mention` both build an op:message payload, so the rule for turning
`reply_to` into a wire field lives here once rather than in each. Pure: no I/O,
no gateway, no env.

`reply_to` -> m.relates_to.m.in_reply_to. This is a CITATION and the event stays
in the MAIN TIMELINE — it is not thread membership. `thread_root` -> the
gateway's own field for a `rel_type: m.thread` relation, built server-side from
the id, which puts the event IN that thread; it subsumes `reply_to`, which then
becomes the relation's fallback target. Either id is checked here first: posting
unrelated because an id was unusable would be the silent-wrong-place failure.
"""
from __future__ import annotations


class RelationError(ValueError):
    """A malformed event id. Raised rather than dropped: posting unrelated
    because an id was unusable is the silent-wrong-place failure."""


def _event_id(value, field: str) -> str:
    text = str(value).strip()
    if not text.startswith("$") or len(text) < 2:
        raise RelationError(f"{field} must be a Matrix event id like $abc, got {value!r}")
    return text


def relation_fields(reply_to=None, thread_root=None) -> dict:
    """op:message fields citing the message this post replies to and/or the
    thread it belongs in, or {}."""
    out = {}
    if reply_to:
        out["reply_to"] = _event_id(reply_to, "reply_to")
    if thread_root:
        out["thread_root"] = _event_id(thread_root, "thread_root")
    return out
