"""First-writer-wins claims on an acknowledgement key, shared by every sender of that ack.

One record per key under <workspace>/state/ack-claims/. `claim()` creates it only where none
exists, so of two senders racing for `ack:<task>:initial` exactly one gets True and speaks; the
other stays silent. Only the holder may change or release its record, and it releases only
when nothing was sent. A record that exists but cannot be read counts as held: two lines are
worse than none. States: claimed (sending, outcome not yet known), confirmed (delivery
evidence recorded), unknown (the send may have landed). Contract: tests/ack-claim.test.py.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_record  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

STATES = ("claimed", "confirmed", "unknown")
_KEY = re.compile(r"\A[a-z][a-z-]*:[A-Za-z0-9._-]{1,160}:[a-z][a-z-]*\Z")


def claims_dir(workspace: Optional[Path] = None) -> Path:
    return Path(workspace or resolve_workspace(migrate=False)) / "state" / "ack-claims"


def _name(key: str) -> str:
    if not isinstance(key, str) or not _KEY.match(key):
        raise ValueError(f"not an ack key: {key!r}")
    return key.replace(":", "+")


def _path(workspace: Path, key: str) -> Path:
    return claims_dir(workspace) / f"{_name(key)}.json"


def claim(workspace: Path, key: str, holder: str, now: Optional[float] = None, **fields) -> bool:
    """True when this call created the claim and the caller may send; False when it already exists."""
    rec = {**fields, "id": _name(key), "key": key, "holder": holder, "state": "claimed",
           "claimed_at": time.time() if now is None else now}
    try:
        local_record.create_text_whole(_path(workspace, key), json.dumps(rec, ensure_ascii=False))
    except FileExistsError:
        return False
    return True


def read(workspace: Path, key: str) -> Optional[dict]:
    """The record, None when there is none, or {"state": "unknown"} when one exists but is unreadable."""
    p = _path(workspace, key)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"key": key, "state": "unknown", "holder": None}
    if not isinstance(d, dict) or d.get("key") != key or d.get("state") not in STATES:
        return {"key": key, "state": "unknown", "holder": d.get("holder") if isinstance(d, dict) else None}
    return d


def state(workspace: Path, key: str) -> str:
    """"none" when unclaimed, else the record's state; an unreadable record reads "unknown"."""
    d = read(workspace, key)
    return "none" if d is None else d["state"]


def update(workspace: Path, key: str, holder: str, new_state: Optional[str] = None, **fields) -> bool:
    """Merge `fields` (and a new state) into the holder's own record; False when it is not the holder's."""
    if new_state is not None and new_state not in STATES:
        raise ValueError(f"not an ack-claim state: {new_state!r}")
    d = read(workspace, key)
    if d is None or d.get("holder") != holder or d.get("id") != _name(key):
        return False
    d.update(fields)
    if new_state is not None:
        d["state"] = new_state
    local_record.write_text_whole(_path(workspace, key), json.dumps(d, ensure_ascii=False))
    return True


def release(workspace: Path, key: str, holder: str) -> bool:
    """Drop the holder's own claim, for a sender that sent nothing; False when it is not the holder's."""
    d = read(workspace, key)
    if d is None or d.get("holder") != holder or d.get("id") != _name(key):
        return False
    _path(workspace, key).unlink(missing_ok=True)
    return True


def held_by(workspace: Path, holder: str, state_filter: Optional[str] = None) -> list:
    """Every readable record this holder owns, optionally only in one state."""
    d = claims_dir(workspace)
    out = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(rec, dict) and rec.get("holder") == holder and rec.get("id") == p.stem
                and (state_filter is None or rec.get("state") == state_filter)):
            out.append(rec)
    return out
