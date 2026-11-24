"""The quota notice: the account card, unasked, once the session has hit its usage limit.

A session at its limit cannot answer, so nobody learns why it went quiet. The launcher already reads
the pane each tick; when Claude Code's limit banner leads a line there (cli_wedge_patterns'
provider-limit families), the owner gets the account card in their direct message: what happened,
when it resets if the banner says, and the account controls to switch with. One card per limit: the
next is sent only after the banner has been gone for CLEAR_S and comes back.

The owner's DM room is not something a connector is told; it is remembered from the owner's own
messages there (`remember_room`, at the claim), in a record of its own: the claim writes only that
one and the supervisor only the incident's, so neither can undo the other's write. Until a room has
been seen no card can be sent, and the launcher's own stall card still reaches the owner.

Queued is not told. The bridge records what became of each queued card (connector_cards.outcome), and
only that record decides: `posted` is told; `dropped` (expired with no send tried) is queued again as
a new card; `unsure` (expired after a send that may have gone through) is queued again under its own
key, which the server takes for the same message. A card with no record and no file left is not known
to be any of these, so it is never sent again: that could tell the owner twice. Nothing here
types into the session or changes an account.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from ... import card_desk, claude_local_records as records, connect_bridge as cb, connector_cards
from ...cli_wedge_patterns import ABNORMAL_PATTERNS, PROVIDER_LIMIT_PATTERNS
from . import account_card, detector

FILE = "quota-card.json"        # the incident: the supervisor's alone
ROOM_FILE = "quota-room.json"   # the owner's DM room: the claim's alone

CLEAR_S = 600.0         # the banner gone this long: the limit is over, and a later one is a new card
RETRY_S = 60.0          # a card that could not be queued is tried again no sooner than this
UNKNOWN_S = 2 * connector_cards.MAX_QUEUED_S

# The reset time as the banner gives it: a clock time, optionally after a date, optionally with a zone.
_CLOCK = r"\d{1,2}(?::\d{2})?\s?[ap]m"
_DAY = r"(?:[A-Z][a-z]{2}\s\d{1,2}|\d{1,2}\s[A-Z][a-z]{2})"
_WHEN = re.compile(r"\bresets?\s+(?:at\s+|on\s+)?(?:(?P<day>%s),?\s+(?:at\s+)?)?(?P<clock>%s)(?:\s*\((?P<zone>[A-Za-z_]+(?:/[A-Za-z_]+){0,2})\))?"
                   % (_DAY, _CLOCK), re.IGNORECASE)
_LIMIT = tuple(rx for name, rx in ABNORMAL_PATTERNS if name in PROVIDER_LIMIT_PATTERNS)
_ROOM = re.compile(r"^![^\s:]+(:\S+)?$")


def _read(root, name) -> dict:
    try:
        rec = json.loads((records.state_dir(root) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def _write(root, name, rec: dict) -> bool:
    try:
        records.write_durable(records.state_dir(root) / name, json.dumps(rec, sort_keys=True).encode("utf-8"))
        return True
    except (OSError, records.RecordError):
        return False


def load(root) -> dict:
    return _read(root, FILE)


def save(root, rec: dict) -> bool:
    return _write(root, FILE, rec)


def room(root) -> str:
    """The owner's DM room as last seen, '' when none has been."""
    kept = str(_read(root, ROOM_FILE).get("room") or "")
    return kept if _ROOM.match(kept) else ""


def remember_room(root, seen) -> None:
    """Keep the owner's DM room, seen on a message of theirs there; anything that is not a room id is ignored."""
    seen = str(seen or "")
    if _ROOM.match(seen) and room(root) != seen:
        _write(root, ROOM_FILE, {"room": seen})


def reset_words(text: str) -> str:
    """When the limit resets ("resets 3pm (America/Los_Angeles)", "resets Nov 28, 9am"), '' when the
    banner does not say. Only the limit banner's own line is read, and only a time is taken from it: a
    clock time, with a date before it and a zone after it when given. The words are built from those
    parts, never copied from the pane, so nothing else on the line can reach the DM."""
    for line in reversed(str(text or "").splitlines()):
        line = detector._DECOR.sub("", line)
        if any(rx.match(line) for rx in _LIMIT):
            found = _WHEN.search(line)
            if not found:
                return ""
            day, zone = found.group("day"), found.group("zone")
            return "resets %s%s%s" % (" ".join(day.split()) + ", " if day else "", "".join(found.group("clock").split()).lower(),
                                      " (%s)" % zone if zone else "")
    return ""


def words(limited: list, text: str, using: str) -> str:
    what = "is out of credits" if "out-of-credits" in limited else "has reached its usage limit"
    when = reset_words(text)
    return "%s %s%s, so I can't answer until then. Switch to another account or an API key to go on now. %s" % (
        account_card.NAME, what, " (%s)" % when if when else " (it does not say when that resets)", using)


def delivery(bridge, rec: dict, at: float) -> str:
    """What became of the card `rec` queued: 'posted', 'dropped' or 'unsure' as the bridge recorded it, 'pending'
    while it is in the outbox or the bridge may still be settling it, 'unknown' when UNKNOWN_S later
    there is still neither a record nor a file. Absence is never read as either outcome."""
    qid = str(rec.get("qid") or "")
    said = connector_cards.outcome(bridge, qid)
    if said:
        return said
    if Path(bridge).joinpath(*connector_cards.POSTED, qid + ".json").is_file():
        return "posted"
    if Path(bridge).joinpath(*connector_cards.OUTBOX, qid + ".json").is_file():
        return "pending"
    return "pending" if at - float(rec.get("queued") or 0) <= UNKNOWN_S else "unknown"


def notice(root, cfg: dict, names, text: str, log, now=time.time) -> str:
    """One tick's reading of the pane: `names` are the banner families on it. What was done: 'quota-card'
    (queued for the owner's DM), 'quota-card-posted', 'quota-card-dropped' (queued again later),
    'quota-card-unsure' (queued again later under the same key),
    'quota-card-unknown' (no outcome was ever recorded: not sent again), 'quota-no-room',
    'quota-not-posted' (tried again later), 'quota-cleared', or '' for nothing."""
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
        save(root, {})
        return "quota-cleared"
    if rec.get("gone") is not None:
        rec = {k: v for k, v in rec.items() if k != "gone"}
        save(root, rec)
    if rec.get("told"):
        return ""
    bridge = str(cb.bridge_state(cfg))
    if rec.get("qid"):
        fate = delivery(bridge, rec, at)
        if fate == "pending":
            return ""
        if fate == "posted":
            save(root, dict(rec, told=True))
            return "quota-card-posted"
        if fate == "unknown":   # it may have been posted: a second card would tell the owner twice
            save(root, dict(rec, told=True, unknown=True))
            return "quota-card-unknown"
        if fate == "unsure":    # the same key and time: the same queue id, one message at the server
            save(root, dict({k: v for k, v in rec.items() if k not in ("qid", "queued", "tried")}, again=True))
            return "quota-card-unsure"
        rec = {k: v for k, v in rec.items() if k not in ("qid", "key", "at", "queued", "tried", "again")}
        save(root, rec)
        return "quota-card-dropped"
    if at - float(rec.get("tried") or 0) < RETRY_S:
        return ""
    # The card's key is saved before it is queued: a stop between the two queues the same card again, not a second.
    rec = dict(rec, since=rec.get("since") or at, tried=at, key=rec.get("key") or uuid.uuid4().hex,
               at=rec.get("at") or time.time_ns())
    if not save(root, rec):
        return "quota-not-posted"
    to = room(root)
    if not to:
        return "quota-no-room"
    if not card_desk.usable(bridge):
        return "quota-not-posted"
    card = account_card.AccountCard(root, cfg, lambda result_file, body: None, log)
    built = card.card("pending", words(limited, text, card.using()), ask=True)
    try:
        qid = connector_cards.queue(bridge, to, *built, key=rec["key"], at_ns=rec["at"], again=bool(rec.get("again")))
    except (OSError, ValueError) as e:
        log("quota card not queued:", "%s: %s" % (type(e).__name__, e))
        return "quota-not-posted"
    save(root, dict({k: v for k, v in rec.items() if k != "again"}, qid=qid, queued=at))
    return "quota-card"
