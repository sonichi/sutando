#!/usr/bin/env python3
"""room-ops · relations — the op:message field that cites the message replied to.

`say` and `mention` both build an op:message payload, so the rule for turning
`reply_to` into a wire field lives here once rather than in each. Pure: no I/O,
no gateway, no env.

`reply_to` -> m.relates_to.m.in_reply_to. This is a CITATION and the event stays
in the MAIN TIMELINE — it is not thread membership. Only a relation with
`rel_type: m.thread` puts an event in a thread, and the gateway has no field for
that today, so this module deliberately offers no way to ask for one: a call that
reported success while landing outside the requested thread would be the
silent-wrong-place failure the id check below exists to prevent.
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


def relation_fields(reply_to=None) -> dict:
    """op:message fields citing the message this post replies to, or {}."""
    if reply_to:
        return {"reply_to": _event_id(reply_to, "reply_to")}
    return {}
