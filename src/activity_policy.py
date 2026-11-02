#!/usr/bin/env python3
"""Access tier → capabilities → audience policy → projection. The tier says what a person may DO to
an agent; the projection says what they may SEE of it, and the two meet in one rule: private
execution detail follows agent ownership, shared task status follows the room's task.

TASK_STATUS is bound to the shared task, never to the agent: a member sees it because task X belongs
to this room, not because the room may see the agent's running tasks — so a No-access member of a
room still sees "Air · Working on this task" there, and nobody outside the task's room ever does.
"""
from __future__ import annotations

TIERS = ("owner", "team", "guest", "none")
CAPABILITIES: dict[str, frozenset[str]] = {
    "owner": frozenset({"agent.invoke", "agent.configure", "activity.view_private", "activity.view_room",
                        "availability.view_room"}),
    "team": frozenset({"agent.invoke", "activity.view_room", "availability.view_room"}),
    "guest": frozenset({"activity.view_room", "availability.view_room"}),
    "none": frozenset(),
}
ROOM_MEMBER_CAPABILITIES = frozenset({"task_activity.view_shared", "availability.view_room"})


def capabilities(tier: str, room_member: bool = False) -> frozenset[str]:
    """Tier capabilities plus what room membership itself grants; unknown tiers grant nothing."""
    caps = set(CAPABILITIES.get(tier, frozenset()))
    if room_member:
        caps |= ROOM_MEMBER_CAPABILITIES
    return frozenset(caps)


def projections_for(viewer_tier: str, viewer_room: str | None, task_room: str | None,
                    viewer_is_room_member: bool = False) -> frozenset[str]:
    """Which projections this viewer may receive for a task that belongs to `task_room`."""
    caps = capabilities(viewer_tier, viewer_is_room_member)
    out: set[str] = set()
    if "activity.view_private" in caps:
        out |= {"RUNTIME_DETAIL", "TASK_STATUS", "AVAILABILITY"}
        return frozenset(out)
    same_room = viewer_room is not None and viewer_room == task_room
    if same_room and "task_activity.view_shared" in caps:
        out.add("TASK_STATUS")
    if "availability.view_room" in caps:
        out.add("AVAILABILITY")  # policy-filtered per room by the availability projection
    return frozenset(out)
