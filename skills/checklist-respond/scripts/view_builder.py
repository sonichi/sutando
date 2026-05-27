"""
Builds discord.ui.View objects from ChecklistSpec.

Called from discord-bridge.py when [checklist] is detected in a result.
The bridge imports this module and passes it the discord module reference
so the view runs in the bridge's asyncio loop.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord as _discord
    from .detector import ChecklistSpec


# Custom-id prefix — must be short (budget is 100 chars total)
_CID_PREFIX = "cl_"


def cid(msg_id: str, item_id: str) -> str:
    """Build a button custom_id. Format: cl_<msg_id>_<item_id>"""
    return f"{_CID_PREFIX}{msg_id}_{item_id}"


def parse_cid(custom_id: str) -> tuple[str, str] | None:
    """Parse a button custom_id → (msg_id, item_id) or None."""
    if not custom_id.startswith(_CID_PREFIX):
        return None
    rest = custom_id[len(_CID_PREFIX):]
    # msg_id is 17-20 digit snowflake; split on last underscore
    parts = rest.rsplit("_", 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def build_view(discord, spec: "ChecklistSpec", msg_id: str, voters: dict) -> "discord.ui.View":
    """Build a discord.ui.View from a ChecklistSpec + current voter state.

    `voters` maps item_id → list of user_id strings that have voted for it.
    """
    view = discord.ui.View(timeout=None)

    if spec.kind == "lettered":
        for item in spec.items:
            # Picked items get green; others stay gray
            voted_by = voters.get(item.id, [])
            picked = item.state == "picked" or bool(voted_by)
            style = discord.ButtonStyle.success if picked else discord.ButtonStyle.secondary
            label = f"{item.id.upper()}) {item.label}"
            if voted_by:
                names = ", ".join(voters.get("_names", {}).get(uid, uid) for uid in voted_by)
                label = f"{label} ← {names}"
            btn = discord.ui.Button(
                style=style,
                label=label[:80],
                custom_id=cid(str(msg_id), item.id),
            )
            view.add_item(btn)

    elif spec.kind == "checklist":
        for item in spec.items:
            voted_by = voters.get(item.id, [])
            checked = item.state == "checked" or bool(voted_by)
            emoji = "✅" if checked else "⬜"
            style = discord.ButtonStyle.success if checked else discord.ButtonStyle.secondary
            label = f"{emoji} {item.label}"
            if voted_by:
                names = ", ".join(voters.get("_names", {}).get(uid, uid) for uid in voted_by)
                label = f"{label} ({names})"
            btn = discord.ui.Button(
                style=style,
                label=label[:80],
                custom_id=cid(str(msg_id), item.id),
            )
            view.add_item(btn)

    elif spec.kind == "yesno":
        yes_voters = voters.get("yes", [])
        no_voters = voters.get("no", [])
        yes_label = f"✅ Yes" + (f" ({len(yes_voters)})" if yes_voters else "")
        no_label = f"❌ No" + (f" ({len(no_voters)})" if no_voters else "")
        yes_style = discord.ButtonStyle.success if yes_voters else discord.ButtonStyle.secondary
        no_style = discord.ButtonStyle.danger if no_voters else discord.ButtonStyle.secondary
        view.add_item(discord.ui.Button(
            style=yes_style, label=yes_label[:80],
            custom_id=cid(str(msg_id), "yes"),
        ))
        view.add_item(discord.ui.Button(
            style=no_style, label=no_label[:80],
            custom_id=cid(str(msg_id), "no"),
        ))

    return view


def load_state(state_dir: Path, msg_id: str) -> dict | None:
    """Load checklist state from disk, or None if not found."""
    f = state_dir / f"{msg_id}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def save_state(state_dir: Path, msg_id: str, state: dict) -> None:
    """Persist checklist state to disk (best-effort)."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / f"{msg_id}.json").write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def apply_click(state: dict, item_id: str, user_id: str, username: str) -> dict:
    """Apply a button click to the checklist state and return the updated state."""
    voters = state.setdefault("voters", {})
    names = voters.setdefault("_names", {})
    names[user_id] = username

    kind = state.get("kind", "checklist")

    if kind == "lettered":
        # Single-pick: clear all others, set this one
        for iid in list(voters.keys()):
            if iid.startswith("_"):
                continue
            if iid != item_id and user_id in voters[iid]:
                voters[iid] = [u for u in voters[iid] if u != user_id]
        voters.setdefault(item_id, [])
        if user_id not in voters[item_id]:
            voters[item_id].append(user_id)

    elif kind == "checklist":
        # Toggle: add if not present, remove if present
        voters.setdefault(item_id, [])
        if user_id in voters[item_id]:
            voters[item_id] = [u for u in voters[item_id] if u != user_id]
        else:
            voters[item_id].append(user_id)

    elif kind == "yesno":
        # Toggle: mutually exclusive yes/no per user
        other = "no" if item_id == "yes" else "yes"
        voters.setdefault(item_id, [])
        voters.setdefault(other, [])
        if user_id in voters[other]:
            voters[other] = [u for u in voters[other] if u != user_id]
        if user_id in voters[item_id]:
            voters[item_id] = [u for u in voters[item_id] if u != user_id]
        else:
            voters[item_id].append(user_id)

    return state
