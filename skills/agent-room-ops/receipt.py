#!/usr/bin/env python3
"""room-ops · receipt — one reading of the `op: message` response envelope.

`say` and `mention` post the same envelope, so they must agree on what counts as
proof of delivery. The gateway can answer HTTP 200 while swallowing a room-send
failure, and it can also answer `{"ok": true}` with no event id on a send that
really landed — so a boolean cannot carry the distinction without being wrong in
one direction:

    fail-closed on a missing id -> the caller re-sends a delivered message
    fail-open  on a missing id -> the caller suppresses its fallback, reply lost

`tests/cron-notify.test.py` already pins the fail-open half for `_post_to_room`
("the return must still be truthy or the caller re-sends"). Rather than adopt the
opposite rule here and leave two contradictory policies in one repo, this returns
THREE states and lets the caller choose which risk it carries.
"""
from __future__ import annotations

# confirmed = an event id came back. unconfirmed = 200 with nothing proving
# delivery. failed = transport or HTTP error.
CONFIRMED = "confirmed"
UNCONFIRMED = "unconfirmed"
FAILED = "failed"
# The request may have reached the gateway (timeout, transport error): not a no.
UNKNOWN = "unknown"


def http_error_state(code) -> str:
    """The receipt state for an HTTP error status, for `say` and `mention` alike.

    ONLY 4xx is a definite refusal. Every other error status may have APPLIED the
    write -- a 3xx redirect loop applies the POST and then fails the client -- so
    it is UNKNOWN and must park rather than settle as proven non-delivery. This
    mirrors `src/outbox_adapter.classify_response`, which this repo already treats
    as the delivery contract; a second reading of it here is how the two drifted.
    """
    try:
        code = int(code)
    except (TypeError, ValueError):
        return UNKNOWN
    return FAILED if 400 <= code < 500 else UNKNOWN


def event_id_of(parsed) -> str | None:
    """The only accepted proof: a non-empty string event id in a dict body."""
    if isinstance(parsed, dict):
        eid = parsed.get("event_id")
        if isinstance(eid, (str, int)) and str(eid).strip():
            return str(eid)
    return None


def classify(parsed) -> tuple[str, str | None, str | None]:
    """-> (state, event_id, reason). Never raises; a junk body is UNCONFIRMED."""
    eid = event_id_of(parsed)
    if eid:
        return CONFIRMED, eid, None
    if isinstance(parsed, dict) and not parsed:
        return UNCONFIRMED, None, "gateway returned 200 with an empty body — delivery not confirmed"
    if isinstance(parsed, dict):
        return UNCONFIRMED, None, "gateway returned 200 with no event_id — delivery not confirmed"
    return UNCONFIRMED, None, "gateway returned 200 with a non-object body — delivery not confirmed"
