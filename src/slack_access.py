"""Slack access-record semantics — the three states, owned in one place.

`access.json` distinguishes three states, and the difference is operational,
not cosmetic:

    file absent             -> UNCONFIGURED   never configured; TOFU open
    allowFrom populated     -> ENROLLED       an owner is enrolled
    allowFrom empty         -> LOCKED         admin locked it down; TOFU CLOSED
    record unreadable       -> UNKNOWN        we cannot tell — including a
                                              present `allowFrom` that is not a
                                              list of string user IDs

UNKNOWN is reported rather than folded into LOCKED because the two consumers
need OPPOSITE fail-safes and each must choose its own: the bridge denies
access (nobody gets in on an unreadable record), while the health check must
not fabricate a remedy it cannot support.

Two consumers MUST map those the same way:
  - `src/slack-bridge.py:load_allowed`  (the live gate)
  - `src/health-check.py`               (tells the operator what to fix)

A boolean `exists()` collapses the last two, which makes the health check
advise "enable Event Subscriptions" for a workspace where that changes
nothing: the bridge stays silent because no user is permitted to reach it.
Provider I/O (the bridge's mtime cache) stays at the edge; this module only
reads and classifies.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple, Optional, Set

UNCONFIGURED = "unconfigured"
ENROLLED = "enrolled"
LOCKED = "locked"
UNKNOWN = "unknown"


class SlackAccess(NamedTuple):
    allowed: Optional[Set[str]]
    record: Optional[dict]


def read_access(access_file) -> SlackAccess:
    """Read the record. `record` is None whenever nothing parseable was read,
    so a caller cannot mistake a fail-safe lockout for a real empty record."""
    try:
        record = json.loads(Path(access_file).read_text())
    except FileNotFoundError:
        return SlackAccess(None, None)
    except Exception:  # noqa: BLE001 — unreadable must never read as enrolled
        return SlackAccess(set(), None)
    if not isinstance(record, dict):
        return SlackAccess(set(), None)
    # Validate the SHAPE, not just that a conversion succeeds. `"U123"` and `[7]`
    # both convert fine and would enrol nobody real, so they must read UNKNOWN.
    allow = record.get("allowFrom")
    if allow is None:
        allowed: Set[str] = set()   # absent key: the bridge's own `.get(..., [])`
    elif isinstance(allow, list) and all(isinstance(u, str) for u in allow):
        # Drop blanks per ENTRY, not per record: `["U1", ""]` must keep U1 working,
        # and a bare `[""]` then reads LOCKED, whose remedy is "add an allowed id".
        allowed = {u.strip() for u in allow if u.strip()}
    else:
        return SlackAccess(set(), None)
    # record is not None ONLY on a genuine parse; that is what separates a real
    # empty allowFrom (LOCKED) from an unreadable one (UNKNOWN).
    return SlackAccess(allowed, record)


def enrollment_state(access: SlackAccess) -> str:
    if access.allowed is None:
        return UNCONFIGURED
    if access.allowed:
        return ENROLLED
    return LOCKED if access.record is not None else UNKNOWN


def access_state(access_file) -> str:
    return enrollment_state(read_access(access_file))
