"""Validated action ids for the desktop host's managed ACP desired state."""

from __future__ import annotations

import re
from typing import Any, Optional, Tuple

ACTION_KIND = "instance_set_desired"
PREFIX = "instance.set_desired:"
DESIRED = frozenset({"running", "stopped"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def parse(action_id: Any) -> Optional[Tuple[str, str]]:
    if not isinstance(action_id, str) or not action_id.startswith(PREFIX):
        return None
    rest = action_id[len(PREFIX):]
    instance_id, sep, desired = rest.partition(":")
    if not sep or ":" in desired or not _ID.fullmatch(instance_id) or desired not in DESIRED:
        return None
    return instance_id, desired


def action_id(instance_id: str, desired: str) -> str:
    value = f"{PREFIX}{instance_id}:{desired}"
    if parse(value) is None:
        raise ValueError(f"invalid managed instance action: {value!r}")
    return value
