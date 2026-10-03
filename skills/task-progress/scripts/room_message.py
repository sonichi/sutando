"""Public outgoing room-message payload contract; transport stays with callers.

Edit this canonical source; tools/sync_room_message.py distributes standalone copies.
"""
from __future__ import annotations

import json
from typing import Optional


# Fields of the message itself: in extra_content they mean a whole wrapper was passed.
RESERVED_EXTRA_KEYS = ("body", "msgtype", "room", "extra_content", "formatted_body", "format")


def extra_content_problem(extra) -> Optional[str]:
    """Why `extra` cannot ride as extra_content, or None when it can."""
    if not isinstance(extra, dict):
        return "extra_content must be a JSON object"
    reserved = [k for k in RESERVED_EXTRA_KEYS if k in extra]
    if reserved:
        return (f"extra_content carries {', '.join(reserved)} at the top level; those belong to "
                "the message, not its extra content. Pass only the extra_content object itself")
    if "space.ag2." in extra:
        return ('extra_content has the bare key "space.ag2."; a card key names its card, '
                "like space.ag2.collab.doc.summon")
    nested = _misplaced_card(extra)
    if nested:
        return (f"extra_content has a space.ag2.* key at {nested}, under a key that is not a "
                "card; a card must sit at the top level of extra_content or no client renders it")
    return None


def is_card_key(key) -> bool:
    """A space.ag2.* key that names something after the prefix."""
    return isinstance(key, str) and key.startswith("space.ag2.") and len(key) > len("space.ag2.")


def _misplaced_card(extra: dict) -> Optional[str]:
    """Path of a space.ag2.* key under a top-level key that is not a card. A card (a named
    space.ag2.* key whose value is an object) is never inspected: its sub-keys are its own."""
    for k, v in extra.items():
        if is_card_key(k) and isinstance(v, dict):
            continue
        found = _first_card_key(v, f"extra_content[{json.dumps(k, ensure_ascii=False)}]")
        if found:
            return found
    return None


def _first_card_key(value, path: str) -> Optional[str]:
    items = value.items() if isinstance(value, dict) else \
        enumerate(value) if isinstance(value, list) else ()
    for k, v in items:
        here = f"{path}[{json.dumps(k, ensure_ascii=False)}]"
        if isinstance(k, str) and k.startswith("space.ag2."):
            return here
        found = _first_card_key(v, here)
        if found:
            return found
    return None


def room_message_payload(payload: dict) -> dict:
    """Return the unchanged wire payload, refusing malformed extra content."""
    if "extra_content" in payload:
        problem = extra_content_problem(payload["extra_content"])
        if problem:
            raise ValueError(problem)
    return payload
