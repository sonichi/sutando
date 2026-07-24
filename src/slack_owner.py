"""Slack owner-recipient resolution helpers."""

from __future__ import annotations

from typing import Any, Dict, Optional


def resolve_proactive_owner_id(access_data: Dict[str, Any]) -> Optional[str]:
    """Return the configured owner for a proactive Slack DM.

    ``allowFrom`` can include owner, team, and other users.  Proactive owner
    notifications must never use arbitrary set iteration over that mixed list.
    Prefer the TOFU-enrolled owner when it is still owner-eligible, otherwise
    use the first owner-tier entry in the persisted list order.

    Unmapped allowlist entries retain Slack's legacy owner default.
    """
    allow_list = access_data.get("allowFrom") or []
    tier_map = access_data.get("tierMap") or {}
    owner_ids = [
        str(uid)
        for uid in allow_list
        if tier_map.get(str(uid), "owner") == "owner"
    ]
    if not owner_ids:
        return None

    tofu_owner = access_data.get("tofuOwner")
    if tofu_owner is not None and str(tofu_owner) in owner_ids:
        return str(tofu_owner)
    return owner_ids[0]
