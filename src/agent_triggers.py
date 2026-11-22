#!/usr/bin/env python3
"""AgentTrigger vocabulary and playbook dispatch: which platform events a task may name and which skill runs it.

A platform event (an @-mention, being added to a room, ...) reaches the core as a
task carrying `event_type`, `agent_trigger_id` and optionally `playbook` headers.
This module is the one owner of that vocabulary:

- EVENT_PLAYBOOKS maps each known event_type to its default playbook id.
- event_type_value / trigger_id_value / playbook_value sanitize the wire values
  (None = drop the header); the remote-gateway bridge writes only what they pass.
  Any well-formed event_type passes, known or not, so a new platform event reaches
  the core with its body; only playbook resolution is restricted.
- resolve_playbook picks the skill a task runs: the task's own playbook when it is
  a skill whose manifest.json sets `"playbook": true` and has skills/<id>/SKILL.md,
  else the event's registry default under the same opt-in, else None (handle the
  task as an ordinary message). A registry id is no exception; any other skill is refused.
- allowed_playbook_value with no skills tree (a bridge that only forwards the header)
  passes registry ids only; the core still applies the manifest opt-in.
- owner_dm_playbook tags the owner's own DM message that asks for a playbook by a
  line of its own. A playbook skill declares that line in its manifest
  (`"playbook_line": "/<word>"`); the adapter injects the skills tree it scans, and
  the bridge writes the answer as the task's `playbook` header. Nothing else in the
  body is read, and a line two skills declare runs neither.
- An event task's body may quote member-written text: it is data, never instructions.
  The trigger's own instruction comes from the server, not from commands quoted in it.

Stdlib only, vendored verbatim into ag2-sparrow by tools/sync_from_src.py.

CLI (what the core runs on a task that carries the headers):
    python3 src/agent_triggers.py resolve --event-type room.joined --playbook room-onboard
prints the SKILL.md path to follow, or `none`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

# event_type -> default playbook id, as the backend's trigger registry names it.
# "reply" has no skill: a mention is answered as today.
EVENT_PLAYBOOKS = {
    "room.mention": "reply",
    "room.joined": "room-onboard",
    "commons.summon": "summon",
    "commons.app_request": "app-request",
    "email.received": "email",
}
EVENT_TYPES = frozenset(EVENT_PLAYBOOKS)
# A well-formed event type: two to four dotted lowercase segments, e.g. "commons.summon".
EVENT_TYPE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){1,3}")
EVENT_TYPE_MAX = 64
# A bridge with no skills tree forwards only these; the core still needs the manifest opt-in.
REGISTRY_PLAYBOOKS = frozenset(EVENT_PLAYBOOKS.values())

# A playbook id names a directory directly under skills/: no separators, no dots.
PLAYBOOK_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# agent_trigger_id: one opaque token, e.g. "join|@a:hs|!room:hs".
_TRIGGER_ID_RE = re.compile(r"[A-Za-z0-9._:@!|=+/-]{1,256}")


def event_type_value(value) -> Optional[str]:
    """The value when it is a well-formed event type (known or not), else None (dropped)."""
    ok = isinstance(value, str) and len(value) <= EVENT_TYPE_MAX and EVENT_TYPE_RE.fullmatch(value)
    return value if ok else None


def trigger_id_value(value) -> Optional[str]:
    """A bounded one-token trigger id, or None when it is anything else."""
    return value if isinstance(value, str) and _TRIGGER_ID_RE.fullmatch(value) else None


def playbook_value(value) -> Optional[str]:
    """A well-formed playbook id, or None when it is anything else."""
    return value if isinstance(value, str) and PLAYBOOK_ID_RE.fullmatch(value) else None


def allowed_playbook_value(value, skills_root: Optional[Path] = None) -> Optional[str]:
    """The playbook when is_playbook allows it, else None; no skills_root allows registry ids only."""
    if skills_root is None:
        return value if playbook_value(value) and value in REGISTRY_PLAYBOOKS else None
    return value if is_playbook(skills_root, value) else None


def default_playbook(event_type) -> Optional[str]:
    """The registry's playbook for a known event type, else None (an unknown type has none)."""
    return EVENT_PLAYBOOKS.get(event_type) if event_type_value(event_type) else None


def is_playbook(skills_root: Path, playbook) -> bool:
    """True only for a skill whose manifest.json sets `"playbook": true`, registry ids included."""
    if not playbook_value(playbook):
        return False
    try:
        manifest = json.loads((skills_root / playbook / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(manifest, dict) and manifest.get("playbook") is True


def _skill_file(skills_root: Path, playbook: Optional[str]) -> Optional[Path]:
    if not is_playbook(skills_root, playbook):
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


# A playbook's own slash line: `/` and one lowercase word, the whole line.
PLAYBOOK_LINE_RE = re.compile(r"/[a-z][a-z0-9-]{0,63}")


def _slash_lines(body) -> list:
    """The body's lines that could be a playbook line, stripped; reads nothing else."""
    if not isinstance(body, str):
        return []
    return [s for s in (line.strip() for line in body.splitlines()) if PLAYBOOK_LINE_RE.fullmatch(s)]


def playbook_lines(skills_root: Optional[Path]) -> dict:
    """{line: playbook} for each playbook skill under `skills_root` whose manifest declares
    `playbook_line`; a line declared by more than one skill is left out (nobody picks)."""
    if skills_root is None:
        return {}
    found: dict = {}
    for manifest in sorted(Path(skills_root).glob("*/manifest.json")):
        playbook = manifest.parent.name
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # The same opt-in is_playbook reads, from this one read of the manifest.
        if not (playbook_value(playbook) and isinstance(data, dict) and data.get("playbook") is True):
            continue
        line = data.get("playbook_line")
        if isinstance(line, str) and PLAYBOOK_LINE_RE.fullmatch(line):
            found.setdefault(line, []).append(playbook)
    return {line: names[0] for line, names in found.items() if len(names) == 1}


def owner_dm_playbook(body, tier, channel_kind, skills_root: Optional[Path]) -> Optional[str]:
    """The playbook the owner's own DM message asks for by a line of its own, or None.

    Only an owner-tier sender in a direct message qualifies: a room message, or any other tier,
    never does, whatever its body says. The line has to stand alone: one quoted (`> /x`, `"/x"`)
    or inside a sentence is someone talking about the command, not sending it. No skills tree
    (a standalone bridge) tags nothing."""
    if tier != "owner" or channel_kind != "dm":
        return None
    candidates = _slash_lines(body)
    if not candidates:
        return None
    lines = playbook_lines(skills_root)
    for line in candidates:
        if line in lines:
            return lines[line]
    return None


def _main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve", help="print the SKILL.md a trigger task follows, or none")
    r.add_argument("--event-type", default="")
    r.add_argument("--playbook", default="")
    repo = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root — the code skills/
    r.add_argument("--skills-root", default=str(repo / "skills"))
    args = ap.parse_args(argv)
    path = resolve_playbook(args.event_type, args.playbook, Path(args.skills_root))
    print(path if path is not None else "none")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
