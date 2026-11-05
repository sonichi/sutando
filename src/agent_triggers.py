#!/usr/bin/env python3
"""AgentTrigger vocabulary and playbook dispatch: which platform events a task may name and which skill runs it.

A platform event (an @-mention, being added to a room, ...) reaches the core as a
task carrying `event_type`, `agent_trigger_id` and optionally `playbook` headers.
This module is the one owner of that vocabulary:

- EVENT_PLAYBOOKS maps each known event_type to its default playbook id.
- event_type_value / trigger_id_value / playbook_value sanitize the wire values
  (None = drop the header); the remote-gateway bridge writes only what they pass.
- resolve_playbook picks the skill a task runs: the task's own playbook when it is
  a valid id with skills/<id>/SKILL.md, else the event's registry default, else
  None (handle the task as an ordinary message).

Stdlib only, vendored verbatim into ag2-sparrow by tools/sync_from_src.py.

CLI (what the core runs on a task that carries the headers):
    python3 src/agent_triggers.py resolve --event-type room.joined --playbook room-onboard
prints the SKILL.md path to follow, or `none`.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

# event_type -> default playbook id. "reply" has no skill: a mention is answered as today.
EVENT_PLAYBOOKS = {
    "room.mention": "reply",
    "room.joined": "room-onboard",
}
EVENT_TYPES = frozenset(EVENT_PLAYBOOKS)

# A playbook id names a directory directly under skills/: no separators, no dots.
PLAYBOOK_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# agent_trigger_id: one opaque token, e.g. "join|@a:hs|!room:hs".
_TRIGGER_ID_RE = re.compile(r"[A-Za-z0-9._:@!|=+/-]{1,256}")


def event_type_value(value) -> Optional[str]:
    """The value when it is a known event type, else None (dropped, not defaulted)."""
    return value if isinstance(value, str) and value in EVENT_TYPES else None


def trigger_id_value(value) -> Optional[str]:
    """A bounded one-token trigger id, or None when it is anything else."""
    return value if isinstance(value, str) and _TRIGGER_ID_RE.fullmatch(value) else None


def playbook_value(value) -> Optional[str]:
    """A well-formed playbook id, or None when it is anything else."""
    return value if isinstance(value, str) and PLAYBOOK_ID_RE.fullmatch(value) else None


def default_playbook(event_type) -> Optional[str]:
    """The registry's playbook for a known event type, else None."""
    known = event_type_value(event_type)
    return EVENT_PLAYBOOKS[known] if known else None


def _skill_file(skills_root: Path, playbook: Optional[str]) -> Optional[Path]:
    if not playbook_value(playbook):
        return None
    path = skills_root / playbook / "SKILL.md"
    return path if path.is_file() else None


def resolve_playbook(event_type, playbook, skills_root: Path) -> Optional[Path]:
    """The SKILL.md a trigger task follows, or None for today's message handling."""
    for candidate in (playbook, default_playbook(event_type)):
        path = _skill_file(skills_root, candidate)
        if path is not None:
            return path
    return None


def _main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve", help="print the SKILL.md a trigger task follows, or none")
    r.add_argument("--event-type", default="")
    r.add_argument("--playbook", default="")
    r.add_argument("--skills-root", default=str(Path(__file__).resolve().parent.parent / "skills"))
    args = ap.parse_args(argv)
    path = resolve_playbook(args.event_type, args.playbook, Path(args.skills_root))
    print(path if path is not None else "none")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
