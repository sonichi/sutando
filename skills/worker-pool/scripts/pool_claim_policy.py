"""Eligibility of a non-exclusive claim marker, shared by routing and diagnostics."""
from __future__ import annotations

import pool_roster as pr


class InvalidClaimMarker(ValueError):
    """A claim marker cannot safely distinguish a service from a recipient."""


def is_non_exclusive(recipient: str, marker_state: str, roster) -> bool:
    """Judge already-read evidence; no workspace access or mutation."""
    if marker_state == "absent":
        return False
    if marker_state != "regular":
        raise InvalidClaimMarker(f"is {marker_state}; refusing to decide")
    if roster is None:
        raise InvalidClaimMarker("found but the roster is unreadable; refusing to decide")
    try:
        if not isinstance(roster, dict) or not isinstance(roster.get("workers"), dict):
            raise pr.RosterError("roster 'workers' must be an object")
        pr.validate_workers(roster["workers"])
    except pr.RosterError as exc:
        raise InvalidClaimMarker(f"found but the roster is malformed ({exc}); refusing to decide") from exc
    if not pr.unknown_targets(roster, [recipient]):
        raise InvalidClaimMarker(f"marks roster recipient {recipient!r}; refusing to decide")
    return True
