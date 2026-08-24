#!/usr/bin/env python3
"""Skill-callable CLI for locked Discord access.json mutations (#3318 blocker 1).

Wraps `access_store.mutate_access_file` so EVERY mutating `/discord:access`
skill subcommand — a freehand Read/Write-tool edit in a SEPARATE OS process
from the bridge, with no other coordination available — goes through the
same locked read-modify-write transaction as every other access.json writer
(tier-map seeding, thread-engage seeding, pairing-code issuance), instead of
racing them for a lost update.

Usage:
  python3 scripts/access-mutate.py pair <code>
  python3 scripts/access-mutate.py deny <code>
  python3 scripts/access-mutate.py allow <senderId>
  python3 scripts/access-mutate.py remove <senderId>
  python3 scripts/access-mutate.py policy <pairing|allowlist|disabled>
  python3 scripts/access-mutate.py group-add <channelId> [--no-mention] [--allow id1,id2]
  python3 scripts/access-mutate.py group-rm <channelId>
  python3 scripts/access-mutate.py group-append <channelId> <senderId> [<senderId> ...]
  python3 scripts/access-mutate.py group-rm-allow <channelId> <senderId> [<senderId> ...]
  python3 scripts/access-mutate.py set <key> <value>

Prints a one-line JSON result and exits 0 on success, 1 on failure (unknown
group/code, unreadable/corrupt access.json, bad arguments).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(REPO / "src"))

from access_store import (  # noqa: E402
    mutate_access_file,
    resolve_discord_access_file,
    discord_access_backup_file,
    _atomic_write_owner_only,
)

_VALID_POLICIES = ("pairing", "allowlist", "disabled")
_VALID_REPLY_TO_MODES = ("off", "first", "all")
_SET_KEYS = ("ackReaction", "replyToMode", "textChunkLimit", "chunkMode", "mentionPatterns")


def _backup(data: dict) -> None:
    """Best-effort durable backup — mirrors discord-bridge.py's
    `_backup_access_to_disk`. Never raises: a failed backup must not fail an
    otherwise-successful skill mutation.

    Uses the same atomic born-0600 writer as the live access.json write
    (`access_store._atomic_write_owner_only`) instead of a plain
    `Path.write_text` — a bare write is neither atomic (a reader can observe
    a truncated/partial file mid-write) nor owner-only under a permissive
    umask (#3318 blocker 2)."""
    if not (isinstance(data, dict) and isinstance(data.get("allowFrom"), list)):
        return
    backup_path = discord_access_backup_file()
    try:
        backup_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_write_owner_only(backup_path, json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def _mutate(mutator) -> dict:
    result = mutate_access_file(resolve_discord_access_file(), mutator, backup=_backup)
    if result is None:
        return {"ok": False, "error": "access.json unreadable/corrupt — not modified"}
    return result


def _pair(code: str) -> dict:
    approved = {}

    def _mutator(data):
        pending = data.get("pending", {})
        entry = pending.get(code)
        now_ms = int(time.time() * 1000)
        if not isinstance(entry, dict) or entry.get("expiresAt", 0) <= now_ms:
            return None, {"ok": False, "error": f"pairing code {code!r} not found or expired"}
        sender_id = entry.get("senderId")
        chat_id = entry.get("chatId")
        allow = list(data.get("allowFrom", []))
        if sender_id not in allow:
            allow.append(sender_id)
        data["allowFrom"] = allow
        pending = dict(pending)
        del pending[code]
        data["pending"] = pending
        approved["senderId"] = sender_id
        approved["chatId"] = chat_id
        return data, {"ok": True, "senderId": sender_id, "chatId": chat_id}

    result = _mutate(_mutator)
    # Write the approved-marker only after the locked mutation commits — a
    # failed access.json write must never leave a stray "you're in" marker.
    if result.get("ok") and approved.get("senderId"):
        approved_dir = resolve_discord_access_file().parent / "approved"
        try:
            approved_dir.mkdir(parents=True, exist_ok=True)
            (approved_dir / str(approved["senderId"])).write_text(str(approved["chatId"]))
        except OSError as e:
            result = dict(result)
            result["warning"] = f"allowFrom updated but approved-marker write failed: {e}"
    return result


def _deny(code: str) -> dict:
    def _mutator(data):
        pending = data.get("pending", {})
        if code not in pending:
            return None, {"ok": True, "removed": False}
        pending = dict(pending)
        del pending[code]
        data["pending"] = pending
        return data, {"ok": True, "removed": True}

    return _mutate(_mutator)


def _allow(sender_id: str) -> dict:
    def _mutator(data):
        allow = list(data.get("allowFrom", []))
        if sender_id in allow:
            return None, {"ok": True, "added": False}
        allow.append(sender_id)
        data["allowFrom"] = allow
        return data, {"ok": True, "added": True}

    return _mutate(_mutator)


def _remove(sender_id: str) -> dict:
    def _mutator(data):
        allow = list(data.get("allowFrom", []))
        if sender_id not in allow:
            return None, {"ok": True, "removed": False}
        data["allowFrom"] = [a for a in allow if a != sender_id]
        return data, {"ok": True, "removed": True}

    return _mutate(_mutator)


def _policy(mode: str) -> dict:
    if mode not in _VALID_POLICIES:
        return {"ok": False, "error": f"invalid policy {mode!r} — must be one of {_VALID_POLICIES}"}

    def _mutator(data):
        data["dmPolicy"] = mode
        return data, {"ok": True, "dmPolicy": mode}

    return _mutate(_mutator)


def _parse_group_add_args(args: list[str]) -> tuple[bool, list[str]] | None:
    """Parse the optional [--no-mention] [--allow id1,id2] tail. Returns
    (require_mention, allow_from) or None on a malformed flag."""
    require_mention = True
    allow_from: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--no-mention":
            require_mention = False
            i += 1
        elif arg == "--allow":
            if i + 1 >= len(args):
                return None
            allow_from = [s for s in args[i + 1].split(",") if s]
            i += 2
        elif arg.startswith("--allow="):
            allow_from = [s for s in arg[len("--allow="):].split(",") if s]
            i += 1
        else:
            return None
    return require_mention, allow_from


def _group_add(channel_id: str, flags: list[str]) -> dict:
    parsed = _parse_group_add_args(flags)
    if parsed is None:
        return {"ok": False, "error": f"bad group-add flags: {flags!r} — expected [--no-mention] [--allow id1,id2]"}
    require_mention, allow_from = parsed

    def _mutator(data):
        groups = dict(data.get("groups", {}))
        groups[channel_id] = {"requireMention": require_mention, "allowFrom": allow_from}
        data["groups"] = groups
        return data, {"ok": True, "channelId": channel_id, "requireMention": require_mention, "allowFrom": allow_from}

    return _mutate(_mutator)


def _group_rm(channel_id: str) -> dict:
    def _mutator(data):
        groups = data.get("groups", {})
        if channel_id not in groups:
            return None, {"ok": True, "removed": False}
        groups = dict(groups)
        del groups[channel_id]
        data["groups"] = groups
        return data, {"ok": True, "removed": True}

    return _mutate(_mutator)


def _set(key: str, value: str) -> dict:
    if key not in _SET_KEYS:
        return {"ok": False, "error": f"unknown key {key!r} — must be one of {_SET_KEYS}"}
    parsed: object = value
    if key == "replyToMode" and value not in _VALID_REPLY_TO_MODES:
        return {"ok": False, "error": f"invalid replyToMode {value!r} — must be one of {_VALID_REPLY_TO_MODES}"}
    if key == "textChunkLimit":
        try:
            parsed = int(value)
        except ValueError:
            return {"ok": False, "error": f"textChunkLimit must be a number, got {value!r}"}
    if key == "chunkMode" and value not in ("length", "newline"):
        return {"ok": False, "error": f"invalid chunkMode {value!r} — must be 'length' or 'newline'"}
    if key == "mentionPatterns":
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"mentionPatterns must be a JSON array of strings: {e}"}
        if not isinstance(parsed, list) or not all(isinstance(p, str) for p in parsed):
            return {"ok": False, "error": "mentionPatterns must be a JSON array of strings"}

    def _mutator(data):
        data[key] = parsed
        return data, {"ok": True, key: parsed}

    return _mutate(_mutator)


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


_USAGE = """usage:
  access-mutate.py pair <code>
  access-mutate.py deny <code>
  access-mutate.py allow <senderId>
  access-mutate.py remove <senderId>
  access-mutate.py policy <pairing|allowlist|disabled>
  access-mutate.py group-add <channelId> [--no-mention] [--allow id1,id2]
  access-mutate.py group-rm <channelId>
  access-mutate.py group-append <channelId> <senderId> [<senderId> ...]
  access-mutate.py group-rm-allow <channelId> <senderId> [<senderId> ...]
  access-mutate.py set <key> <value>"""

# One required positional (code / senderId / mode / channelId) beyond the command name.
_ONE_ARG_COMMANDS = {
    "pair": _pair,
    "deny": _deny,
    "allow": _allow,
    "remove": _remove,
    "policy": _policy,
    "group-rm": _group_rm,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_USAGE, file=sys.stderr)
        return 1
    cmd = argv[1]
    rest = argv[2:]

    if cmd in _ONE_ARG_COMMANDS:
        if len(rest) != 1:
            print(_USAGE, file=sys.stderr)
            return 1
        result = _ONE_ARG_COMMANDS[cmd](rest[0])
    elif cmd == "group-add":
        if len(rest) < 1:
            print(_USAGE, file=sys.stderr)
            return 1
        result = _group_add(rest[0], rest[1:])
    elif cmd == "group-append":
        if len(rest) < 2:
            print(_USAGE, file=sys.stderr)
            return 1
        result = _group_append(rest[0], rest[1:])
    elif cmd == "group-rm-allow":
        if len(rest) < 2:
            print(_USAGE, file=sys.stderr)
            return 1
        result = _group_rm_allow(rest[0], rest[1:])
    elif cmd == "set":
        if len(rest) != 2:
            print(_USAGE, file=sys.stderr)
            return 1
        result = _set(rest[0], rest[1])
    else:
        print(f"unknown command: {cmd!r}\n{_USAGE}", file=sys.stderr)
        return 1

    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
