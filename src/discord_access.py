"""Shared Discord collaborator policy and verified task admission."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from access_store import read_access_for_transaction, resolve_discord_access_file
from local_task_protocol import canonical_access_tier, parse_task_headers, valid_task_id
from task_envelope import verify_text
from workspace_default import resolve_workspace

MAX_TASK_CHARS = 131072


def resolve_is_collaborator(access_data, sender_id, serving_channel_id):
    """Resolve collaborator membership only in the channel being served."""
    try:
        serving_cfg = (access_data.get("groups", {}) or {}).get(str(serving_channel_id), {})
        if isinstance(serving_cfg, dict) and sender_id in set(serving_cfg.get("collaborators", []) or []):
            return True
    except Exception:
        pass
    return False


def resolve_team_collaborator(access_data, access_tier, sender_id, serving_channel_id):
    """Collaborator membership never promotes another tier to Team."""
    if access_tier != "team":
        return False
    return resolve_is_collaborator(access_data, sender_id, serving_channel_id)


def admitted_team_collaborator(access_data, sender_id, channel_id):
    try:
        if access_data.get("dmPolicy", "pairing") not in {"pairing", "allowlist"}:
            return False
        if not resolve_team_collaborator(access_data, "team", sender_id, channel_id):
            return False
        groups = access_data.get("groups", {})
        group = groups[str(channel_id)]
        global_allow = access_data.get("allowFrom", [])
        channel_allow = group.get("allowFrom", [])
        collaborators = group.get("collaborators", [])
        if not all(isinstance(items, list) and all(isinstance(item, str) for item in items)
                   for items in (global_allow, channel_allow, collaborators)):
            return False
        if sender_id not in channel_allow and sender_id not in global_allow:
            return False
        if sender_id in global_allow:
            tier_map = access_data.get("tierMap", {})
            return isinstance(tier_map, dict) and canonical_access_tier(tier_map.get(sender_id, "team")) == "team"
        return sender_id in channel_allow
    except Exception:
        return False


def authorize_collaborator_task(text: str, access_data, workspace: Path) -> dict:
    denied = {"authorized": False}
    try:
        if len(text) > MAX_TASK_CHARS or "\x00" in text or verify_text(text, workspace)["verdict"] != "verified":
            return denied
        values = {}
        for line in text.split("\n"):
            if line.startswith("task:"):
                break
            key, separator, value = line.partition(":")
            if separator:
                values.setdefault(key, []).append(value.strip())
        else:
            return denied
        required = ("id", "access_tier", "source", "collaborator", "user_id", "channel_id")
        if any(len(values.get(key, [])) != 1 for key in required):
            return denied
        parsed = parse_task_headers(text)
        if (parsed.get("source") != "discord" or parsed.get("collaborator") != "true"
                or canonical_access_tier(parsed.get("access_tier")) != "team"
                or not valid_task_id(parsed.get("id")) or not parsed.body.strip()):
            return denied
        sender, channel = parsed.get("user_id"), parsed.get("channel_id")
        if not all(value and value.isascii() and value.isdigit() for value in (sender, channel)):
            return denied
        if not admitted_team_collaborator(access_data, sender, channel):
            return denied
        return {"authorized": True, "channel_id": channel, "body": parsed.body}
    except Exception:
        return denied


def authorize_task_file(task_file: Path) -> dict:
    try:
        workspace = resolve_workspace().resolve()
        task_file = Path(task_file)
        if task_file.is_symlink() or task_file.resolve().parent != (workspace / "tasks").resolve():
            return {"authorized": False}
        with task_file.open(encoding="utf-8") as source:
            text = source.read(MAX_TASK_CHARS + 1)
        parsed = parse_task_headers(text)
        if task_file.name not in {f"{parsed.get('id')}.txt", f"{parsed.get('id')}.txt.processing"}:
            return {"authorized": False}
        access_data = read_access_for_transaction(resolve_discord_access_file())
        return authorize_collaborator_task(text, access_data, workspace)
    except Exception:
        return {"authorized": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-file", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(authorize_task_file(args.task_file)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
