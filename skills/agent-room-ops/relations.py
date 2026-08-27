#!/usr/bin/env python3
"""room-ops · relations — the op:message fields that place a post in a thread.

`say` and `mention` both build an op:message payload, so the rule for turning
(reply_to, thread_root) into wire fields lives here once rather than in each.
Pure: no I/O, no gateway, no env.

Wire contract, from the gateway's op:message handler:

  - `reply_to`    -> m.relates_to.m.in_reply_to  (a rich reply). Supported today.
  - `thread_root` -> m.relates_to rel_type m.thread. NOT supported by the gateway
    yet; an unknown field is dropped, so a thread_root post degrades to the rich
    reply carried alongside it rather than vanishing. That fallback is the reason
    `thread_root` always emits a `reply_to` too.

The Matrix thread shape wants in_reply_to to name the LATEST event in the thread,
falling back to the root when the caller has nothing newer — which is what a
caller passing thread_root alone means.
"""
from __future__ import annotations


class RelationError(ValueError):
    """A malformed event id. Raised rather than dropped: posting unthreaded
    because an id was unusable is the silent-wrong-place failure."""


def _event_id(value, field: str) -> str:
    text = str(value).strip()
    if not text.startswith("$") or len(text) < 2:
        raise RelationError(f"{field} must be a Matrix event id like $abc, got {value!r}")
    return text


def relation_fields(reply_to=None, thread_root=None) -> dict:
    """op:message fields placing the post in a thread / reply chain.

    thread_root alone -> reply_to falls back to the root (see module docstring).
    Neither -> {} (an unrelated top-level post).
    """
    if thread_root:
        root = _event_id(thread_root, "thread_root")
        latest = _event_id(reply_to, "reply_to") if reply_to else root
        return {"thread_root": root, "reply_to": latest}
    if reply_to:
        return {"reply_to": _event_id(reply_to, "reply_to")}
    return {}
