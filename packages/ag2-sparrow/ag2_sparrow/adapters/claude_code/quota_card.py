"""The quota notice: the account card, unasked, once the session has hit its usage limit.

A session at its limit cannot answer, so nobody learns why it went quiet. The launcher already reads
the pane each tick; when Claude Code's limit banner leads a line there (cli_wedge_patterns'
provider-limit families), the owner gets the account card in their direct message: what happened,
when it resets if the banner says, and the account controls to switch with. One card per limit: the
next is sent only after the banner has been gone for CLEAR_S and comes back.

The owner's DM room is not something a connector is told; it is remembered from the owner's own
messages there (`remember_room`, at the claim). Until one has been seen no card can be sent, and the
launcher's own stall card still reaches the owner. Nothing here types into the session or changes
an account.
"""
from __future__ import annotations

import json
import re
import time
import uuid

from ... import claude_local_records as records
from ...cli_wedge_patterns import PROVIDER_LIMIT_PATTERNS
from . import account_card

FILE = "quota-card.json"
CLEAR_S = 600.0         # the banner gone this long: the limit is over, and a later one is a new card
RETRY_S = 60.0          # a card that could not be queued is tried again no sooner than this
_RESET = re.compile(r"\bresets?\b[^│|]{0,60}", re.IGNORECASE)
_ROOM = re.compile(r"^![^\s:]+(:\S+)?$")


def load(root) -> dict:
    try:
        rec = json.loads((records.state_dir(root) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def save(root, rec: dict) -> bool:
    try:
        records.write_durable(records.state_dir(root) / FILE, json.dumps(rec, sort_keys=True).encode("utf-8"))
        return True
    except (OSError, records.RecordError):
        return False


def remember_room(root, room) -> None:
    """Keep the owner's DM room, seen on a message of theirs there; anything that is not a room id is ignored."""
    room = str(room or "")
    rec = load(root)
    if _ROOM.match(room) and rec.get("room") != room:
        save(root, dict(rec, room=room))


def reset_words(text: str) -> str:
    """When the limit resets, in the banner's own words ("resets 3pm (America/Los_Angeles)"); '' when it
    does not say."""
    for line in reversed(str(text or "").splitlines()):
        found = _RESET.search(line)
        if found:
            return " ".join(found.group(0).split()).rstrip(" .·")[:60]
    return ""


def words(limited: list, text: str, using: str) -> str:
    what = "is out of credits" if "out-of-credits" in limited else "has reached its usage limit"
    when = reset_words(text)
    return "%s %s%s, so I can't answer until then. Switch to another account or an API key to go on now. %s" % (
        account_card.NAME, what, " (%s)" % when if when else " (it does not say when that resets)", using)


def notice(root, cfg: dict, names, text: str, log, now=time.time) -> str:
    """One tick's reading of the pane: `names` are the banner families on it. What was done: 'quota-card'
    (queued for the owner's DM), 'quota-no-room', 'quota-not-posted' (tried again later), 'quota-cleared',
    or '' for nothing."""
    limited = [n for n in names or [] if n in PROVIDER_LIMIT_PATTERNS]
    rec, at = load(root), now()
    if not limited:
        if rec.get("since") is None:
            return ""
        if rec.get("gone") is None:
            save(root, dict(rec, gone=at))
            return ""
        if at - float(rec["gone"]) < CLEAR_S:
            return ""
        save(root, {"room": rec["room"]} if rec.get("room") else {})
        return "quota-cleared"
    if rec.get("gone") is not None:
        rec = {k: v for k, v in rec.items() if k != "gone"}
        save(root, rec)
    if rec.get("told") or at - float(rec.get("tried") or 0) < RETRY_S:
        return ""
    # The card's key is saved before it is queued: a stop between the two queues the same card again, not a second.
    rec = dict(rec, since=rec.get("since") or at, tried=at, key=rec.get("key") or uuid.uuid4().hex,
               at=rec.get("at") or time.time_ns())
    if not save(root, rec):
        return "quota-not-posted"
    if not rec.get("room"):
        return "quota-no-room"
    card = account_card.AccountCard(root, cfg, lambda result_file, body: None, log)
    built = card.card("pending", words(limited, text, card.using()), ask=True)
    if not card.post(rec["room"], built, rec["key"], rec["at"]):
        return "quota-not-posted"
    save(root, dict(rec, told=True))
    return "quota-card"
