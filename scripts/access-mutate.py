#!/usr/bin/env python3
"""Skill-callable CLI for locked Discord access.json mutations (#3318 blocker 1).

Wraps `access_store.mutate_access_file` so the `/discord:access` skill's
`group append`/`group rm-allow` subcommands — a freehand Read/Write-tool edit
in a SEPARATE OS process from the bridge, with no other coordination
available — go through the same locked read-modify-write transaction as
every other access.json writer (tier-map seeding, thread-engage seeding,
pairing-code issuance), instead of racing them for a lost update.

Usage:
  python3 scripts/access-mutate.py group-append <channelId> <senderId> [<senderId> ...]
  python3 scripts/access-mutate.py group-rm-allow <channelId> <senderId> [<senderId> ...]

Prints a one-line JSON result and exits 0 on success, 1 on failure (unknown
group, unreadable/corrupt access.json, bad arguments).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(REPO / "src"))

from access_store import (  # noqa: E402
    mutate_access_file,
    resolve_discord_access_file,
    discord_access_backup_file,
)


def _backup(data: dict) -> None:
    """Best-effort durable backup — mirrors discord-bridge.py's
    `_backup_access_to_disk`. Never raises: a failed backup must not fail an
    otherwise-successful skill mutation."""
    if not (isinstance(data, dict) and isinstance(data.get("allowFrom"), list)):
        return
    backup_path = discord_access_backup_file()
    try:
        backup_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def _group_append(channel_id: str, sender_ids: list[str]) -> dict:
    def _mutator(data):
        groups = data.get("groups", {})
        entry = groups.get(channel_id)
        if not isinstance(entry, dict):
            return None, {"ok": False, "error": f"group {channel_id!r} does not exist — run `group add` first"}
        allow = list(entry.get("allowFrom", []))
        added = [sid for sid in sender_ids if sid not in allow]
        if not added:
            return None, {"ok": True, "added": [], "skipped": sender_ids}
        allow.extend(added)
        entry["allowFrom"] = allow
        groups[channel_id] = entry
        data["groups"] = groups
        return data, {"ok": True, "added": added, "skipped": [s for s in sender_ids if s not in added]}

    result = mutate_access_file(resolve_discord_access_file(), _mutator, backup=_backup)
    if result is None:
        return {"ok": False, "error": "access.json unreadable/corrupt — not modified"}
    return result


def _group_rm_allow(channel_id: str, sender_ids: list[str]) -> dict:
    def _mutator(data):
        groups = data.get("groups", {})
        entry = groups.get(channel_id)
        if not isinstance(entry, dict):
            return None, {"ok": False, "error": f"group {channel_id!r} does not exist"}
        allow = list(entry.get("allowFrom", []))
        removed = [sid for sid in sender_ids if sid in allow]
        if not removed:
            return None, {"ok": True, "removed": [], "skipped": sender_ids}
        allow = [a for a in allow if a not in sender_ids]
        entry["allowFrom"] = allow
        groups[channel_id] = entry
        data["groups"] = groups
        return data, {"ok": True, "removed": removed, "skipped": [s for s in sender_ids if s not in removed]}

    result = mutate_access_file(resolve_discord_access_file(), _mutator, backup=_backup)
    if result is None:
        return {"ok": False, "error": "access.json unreadable/corrupt — not modified"}
    return result


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(
            "usage: access-mutate.py <group-append|group-rm-allow> <channelId> <senderId> [<senderId> ...]",
            file=sys.stderr,
        )
        return 1
    cmd, channel_id, *sender_ids = argv[1:]
    if cmd == "group-append":
        result = _group_append(channel_id, sender_ids)
    elif cmd == "group-rm-allow":
        result = _group_rm_allow(channel_id, sender_ids)
    else:
        print(f"unknown command: {cmd!r}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
