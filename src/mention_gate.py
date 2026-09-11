"""Owner @-mention ingestion trigger: shared policy for whether a message that
tags the OWNER counts as a mention of the bot.

Today, in a requireMention channel, a message @-tagging the owner (not the
bot) never reaches the fleet at all. This gate ADDS that capability: while ON,
a bridge treats an owner-tagged message from anyone but the owner as if it
had mentioned the bot, so it is ingested as a task. OFF (the default) is
exactly today's behavior. Channels with requireMention:false already ingest
everything and are unaffected either way.

State file: `<workspace>/state/mention-gate.json`
    {"mentions_enabled": bool, "until": "<ISO-8601>" | null}
(`mentions_enabled` means THIS GATE is on — owner-tag triggers ingestion.)

A missing or malformed file reads as DISABLED — fail-CLOSED to today's
behavior: the feature adds ingestion, so a broken state file must never
surprise-ingest. `until` is an optional auto-expiry for `on --for`: once it
has passed (or cannot be parsed), the gate reads OFF again without a write.

Audit log: `<workspace>/state/mention-gate-ingested.jsonl` — one fsync'd JSON
line per message ingested BECAUSE of this gate, so the owner can review what
the toggle pulled in.

Owner identifiers are PARAMETERS — bridges inject the ids they already know
(Discord allowFrom/tierMap owners, a gateway owner mxid). Nothing is hardcoded.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATE_FILENAME = "mention-gate.json"
INGESTED_FILENAME = "mention-gate-ingested.jsonl"

_DEFAULT_STATE = {"mentions_enabled": False, "until": None}


def _state_path(workspace: Path) -> Path:
    return Path(workspace) / "state" / STATE_FILENAME


def _ingested_path(workspace: Path) -> Path:
    return Path(workspace) / "state" / INGESTED_FILENAME


def read_state(workspace: Path) -> dict:
    """Read the gate state; any read/shape problem returns the fail-closed default."""
    try:
        data = json.loads(_state_path(workspace).read_text(encoding="utf-8"))
        enabled = data.get("mentions_enabled")
        if not isinstance(enabled, bool):
            return dict(_DEFAULT_STATE)
        until = data.get("until")
        return {"mentions_enabled": enabled,
                "until": until if isinstance(until, str) else None}
    except Exception:
        return dict(_DEFAULT_STATE)


def write_state(workspace: Path, mentions_enabled: bool, until: "str | None" = None) -> Path:
    """Atomically persist the gate state (temp sibling + os.replace). Returns the path."""
    path = _state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"mentions_enabled": bool(mentions_enabled), "until": until},
        indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=STATE_FILENAME + ".", suffix=".tmp",
                               dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _parse_iso(value: "str | None") -> "datetime | None":
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def owner_tag_triggers_ingest(workspace: Path, now: "datetime | None" = None) -> bool:
    """True only while the gate is ON and `until` (if set) has not expired.

    Everything else — gate off, missing/malformed state, an `until` that has
    passed or cannot be parsed — reads False: today's behavior stands."""
    state = read_state(workspace)
    if not state["mentions_enabled"]:
        return False
    if state["until"] is None:
        return True
    until = _parse_iso(state["until"])
    if until is None:
        return False  # unparseable expiry fails closed, not open-ended ON
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current < until


def message_tags_owner(mention_ids, text: str, owner_ids) -> bool:
    """True when any owner id appears in the platform mentions array, or as a
    Discord mention token (`<@ID>` / `<@!ID>`) in the text — belt and braces."""
    owners = {str(o).strip() for o in (owner_ids or []) if str(o).strip()}
    if not owners:
        return False
    if any(str(m).strip() in owners for m in (mention_ids or [])):
        return True
    t = text or ""
    return any(f"<@{o}>" in t or f"<@!{o}>" in t for o in owners)


def log_gated_ingest(workspace: Path, entry: dict) -> Path:
    """Durably append one audit record for a message this gate pulled in."""
    path = _ingested_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    return path


def gated_ingest_count(workspace: Path) -> int:
    """How many messages the gate has pulled in so far (audit-log rows)."""
    try:
        return sum(1 for ln in _ingested_path(workspace)
                   .read_text(encoding="utf-8").splitlines() if ln.strip())
    except OSError:
        return 0
