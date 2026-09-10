#!/usr/bin/env python3
"""What transport shape a task has, as opposed to which instance produced it.

`source` used to answer both, so two homeservers behind the same bridge were
indistinguishable while every consumer below switched on the shared value. The
split gives `source` the producing instance and `channel_kind` the shape.

Readers must ask HERE rather than reading either field, so that a task written
before the split keeps answering correctly and only one place knows the order.
"""
from __future__ import annotations

FIELD = "channel_kind"
LEGACY = "source"


def kind_of(task) -> str:
    """The task's transport kind, or "" when it declares neither field.

    Falls back to `source` because envelopes minted before the split carry the
    kind there; a task written after it carries both.
    """
    # Duck-typed, not isinstance(dict): the production caller passes a
    # TaskHeaders, and a dict check silently answers "" for every real task.
    get = getattr(task, "get", None)
    if not callable(get):
        return ""
    for key in (FIELD, LEGACY):
        value = get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def is_kind(task, kinds) -> bool:
    """Whether the task's kind is one of `kinds`, compared case-insensitively."""
    kind = kind_of(task).casefold()
    return bool(kind) and kind in {str(k).casefold() for k in kinds}
