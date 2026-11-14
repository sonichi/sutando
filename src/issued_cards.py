"""The agent panel cards this agent itself posted, so a card click counts only as an answer to one of them.

Any room member can post a message that looks like a card, and the owner's tap on its button sends its
reply text as the owner. So a click acts only when it names a card recorded here (written when this
agent posted it), in the room it was posted in, replies to that card's own event, and carries a text
that card offered. One JSON file per workspace: event id -> {card id, room, lane, handler, offered reply
text -> its command ("" for plain words), picker, expiry}, and for a v2 card its revision and each live
`pick` (option ids) and `input` (length bounds) control by id. A newer card with the same id in the same
room replaces the older record, so a settled card's buttons stop acting. Each v2 submission (its
interaction id) is claimed once, by one task, in a second file. The caller passes the workspace; nothing
here resolves it. Standard library only, plus file_lock and local_record.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Mapping, Optional

LEDGER = ("state", "agent-cards", "issued.json")
CLAIMS = ("state", "agent-cards", "claimed.json")
TTL_S = 3 * 86400
MAX_CARDS = 500
MAX_CLAIMS = 2000
# v2 (agent panel card v2): the controls whose value comes back, and where each may sit.
V2_CHOICE_PLACES = {"pick": ("button", "row", "item"), "input": ("button", "row")}
_V2_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_REVISION_MAX = 2147483647
_OPTIONS_MAX, _OPTION_ID_MAX, _FIELD_MAX = 50, 255, 200


def ledger_path(workspace: Path) -> Path:
    return Path(workspace).joinpath(*LEDGER)


def _actions(card: Mapping[str, object]):
    """Every button, footer link, row action and menu item of `card`."""
    def walk(b: object):
        if isinstance(b, dict):
            yield b
            for item in b.get("items") or []:
                yield from walk(item)

    for b in card.get("buttons") or []:
        yield from walk(b)
    yield from walk(card.get("footer_link"))
    for grp in card.get("groups") or []:
        for row in (grp.get("rows") or []) if isinstance(grp, dict) else []:
            for act in (row.get("actions") or []) if isinstance(row, dict) else []:
                yield from walk(act)


def offered(card: Mapping[str, object]) -> dict:
    """{reply text: command} for every `reply` action of `card` and its shuffle; "" for a reply that is
    plain words. A text two actions give different commands is dropped: a click on it could mean either."""
    pairs = [(b["reply"].strip(), b["command"] if isinstance(b.get("command"), str) else "")
             for b in _actions(card) if b.get("kind") == "reply" and isinstance(b.get("reply"), str)]
    header = card.get("header")
    shuffle = header.get("shuffle") if isinstance(header, dict) else None
    if isinstance(shuffle, dict) and isinstance(shuffle.get("reply"), str):
        pairs.append((shuffle["reply"].strip(), ""))
    out: dict = {}
    for text, command in pairs:
        out[text] = command if out.get(text, command) == command else None
    return {t: c for t, c in out.items() if t and c is not None}


def _placed(card: Mapping[str, object]):
    """(place, action) for every button, row action and menu item of `card`, the footer link left out."""
    def walk(b: object, place: str):
        if isinstance(b, dict):
            yield place, b
            for item in b.get("items") or []:
                yield from walk(item, "item")

    for b in card.get("buttons") or []:
        yield from walk(b, "button")
    for grp in card.get("groups") or []:
        for row in (grp.get("rows") or []) if isinstance(grp, dict) else []:
            for act in (row.get("actions") or []) if isinstance(row, dict) else []:
                yield from walk(act, "row")


def _bound(value: object, low: int, high: int, default: int) -> Optional[int]:
    if value is None:
        return default
    ok = isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    return value if ok else None


def _choice_control(b: Mapping[str, object]) -> Optional[dict]:
    """The stored form of one live v2 `pick` (static options) or `input`, or None for a control the
    client shows inert (a bad shape, or disabled)."""
    if b.get("disabled") is True:
        return None
    if b["kind"] == "pick":
        options = b.get("options")
        if "source" in b or not isinstance(options, list) or not 1 <= len(options) <= _OPTIONS_MAX:
            return None
        ids = [o.get("id") if isinstance(o, dict) else None for o in options]
        if not all(isinstance(i, str) and 1 <= len(i) <= _OPTION_ID_MAX for i in ids) or len(set(ids)) != len(ids):
            return None
        return {"kind": "pick", "options": ids}
    field = b.get("field")
    if not isinstance(field, dict) or not (isinstance(field.get("name"), str) and _V2_ID.fullmatch(field["name"])):
        return None
    low, high = _bound(field.get("min_length"), 0, _FIELD_MAX, 1), _bound(field.get("max_length"), 1, _FIELD_MAX, _FIELD_MAX)
    if low is None or high is None or low > high:
        return None
    return {"kind": "input", "min_length": low, "max_length": high}


def choices(card: Mapping[str, object]) -> Optional[dict]:
    """{"revision", "controls": {id: control}} for a v2 card, None for any other card or one the client
    would not read (no revision, a repeated control id, more than one input). Only live pick and input
    controls are kept; a done or expired card keeps none."""
    revision = card.get("revision")
    if card.get("v") != 2 or isinstance(card.get("v"), bool) or isinstance(revision, bool) \
            or not isinstance(revision, int) or not 1 <= revision <= _REVISION_MAX:
        return None
    seen, inputs, controls = set(), 0, {}
    for place, b in _placed(card):
        cid = b.get("id")
        if cid is None:
            continue
        if not isinstance(cid, str) or cid in seen:
            return None
        seen.add(cid)
        inputs += b.get("kind") == "input"
        if b.get("kind") in V2_CHOICE_PLACES and place in V2_CHOICE_PLACES[b["kind"]] and _V2_ID.fullmatch(cid):
            control = _choice_control(b)
            if control:
                controls[cid] = control
    if inputs > 1 or (inputs and card.get("input") is not None):
        return None
    if card.get("state") in ("done", "expired"):
        controls = {}
    return {"revision": revision, "controls": controls}


def has_picker(card: Mapping[str, object]) -> bool:
    """Whether `card` holds a connect picker, whose no-match path replies "Connect <typed text>"."""
    return any(b.get("kind") == "connect_picker" for b in _actions(card))


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cards = data.get("cards") if isinstance(data, dict) else None
    return cards if isinstance(cards, dict) else {}


def record(workspace: Path, event_id: str, card: Mapping[str, object], *, room: str = "", lane: str = "",
           handler: str = "", now: Optional[float] = None, ttl_s: float = TTL_S) -> bool:
    """Remember that this agent posted `card` as `event_id` in `room` on connection `lane`; `handler` is
    the script that runs its commands. Older records of the same card id in that room are dropped
    either way. False when the card offers no reply to remember."""
    if not (isinstance(event_id, str) and event_id.startswith("$")):
        return False
    payloads, picker, card_id = offered(card), has_picker(card), str(card.get("id") or "")
    v2 = choices(card)
    live_v2 = bool(v2 and v2["controls"])
    from file_lock import locked_file
    from local_record import write_whole
    now = time.time() if now is None else now
    path = ledger_path(workspace)
    with locked_file(path.with_name(path.name + ".lock")):
        cards = {k: v for k, v in _read(path).items()
                 if isinstance(v, dict) and float(v.get("expires_at") or 0) > now
                 and not (v.get("card_id") == card_id and str(v.get("room") or "") == room)}
        if payloads or picker or live_v2:
            cards[event_id] = {"card_id": card_id, "offers": payloads, "picker": picker, "room": room,
                               "lane": lane, "handler": handler, "expires_at": now + ttl_s}
            if live_v2:
                cards[event_id].update({"v": 2, "revision": v2["revision"], "controls": v2["controls"]})
        newest = sorted(cards.items(), key=lambda kv: kv[1]["expires_at"])[-MAX_CARDS:]
        write_whole(path, {"cards": dict(newest)})
    return bool(payloads or picker or live_v2)


def settle(workspace: Path, event_id: str) -> bool:
    """Forget the card posted as `event_id`, so no button of it acts again; False when it was not known."""
    from file_lock import locked_file
    from local_record import write_whole
    path = ledger_path(workspace)
    with locked_file(path.with_name(path.name + ".lock")):
        cards = _read(path)
        if cards.pop(str(event_id or ""), None) is None:
            return False
        write_whole(path, {"cards": cards})
    return True


def live(workspace: Path, event_id: str, *, now: Optional[float] = None) -> Optional[dict]:
    """The unexpired record of the card this agent posted as `event_id`, else None."""
    entry = _read(ledger_path(workspace)).get(str(event_id or ""))
    now = time.time() if now is None else now
    if not isinstance(entry, dict) or float(entry.get("expires_at") or 0) <= now:
        return None
    return entry if isinstance(entry.get("offers"), dict) else None


CLAIMED, SAME, DUPLICATE, REFUSED = "claimed", "same", "duplicate", "refused"


def _held_claims(path: Path) -> Optional[dict]:
    """The claims file's entries; {} when it does not exist, None when it cannot be trusted."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    held = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(held, dict):
        return None
    for v in held.values():
        expiry = v.get("expires_at") if isinstance(v, dict) else None
        if not isinstance(v, dict) or not isinstance(v.get("claimant"), str) \
                or isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
            return None
    return held


def claim(workspace: Path, interaction_id: str, claimant: str, choice: Mapping[str, object], *,
          expires_at: float, now: Optional[float] = None) -> str:
    """Claim the v2 submission `interaction_id` for the task id `claimant` until `expires_at` (its card's
    expiry). Once per submission id across tasks: CLAIMED the first time, SAME for the task id that
    claimed it (its handler reading it again, or that task re-delivered under its own id), DUPLICATE for
    any other task. REFUSED, with nothing written, when the file cannot be trusted or is full of live claims."""
    from file_lock import locked_file
    from local_record import write_whole
    now = time.time() if now is None else now
    path = Path(workspace).joinpath(*CLAIMS)
    with locked_file(path.with_name(path.name + ".lock")):
        held = _held_claims(path)
        if held is None:
            return REFUSED
        held = {k: v for k, v in held.items() if v["expires_at"] > now}
        earlier = held.get(interaction_id)
        if earlier is not None:
            return SAME if earlier["claimant"] == claimant else DUPLICATE
        if len(held) >= MAX_CLAIMS or expires_at <= now:
            return REFUSED
        held[interaction_id] = {"claimant": claimant, "choice": dict(choice), "at": now, "expires_at": expires_at}
        write_whole(path, {"claims": held})
    return CLAIMED


def main(argv=None) -> int:
    """`record --workspace W --event-id $E [--room R --lane L --handler H]` with the posted card (or its room.message.send payload) on stdin."""
    import argparse
    p = argparse.ArgumentParser(description="Remember a card this agent posted.")
    p.add_argument("verb", choices=("record",))
    p.add_argument("--workspace", required=True)
    p.add_argument("--event-id", required=True)
    p.add_argument("--room", default="")
    p.add_argument("--lane", default="")
    p.add_argument("--handler", default="")
    args = p.parse_args(argv)
    try:
        doc = json.loads(sys.stdin.read())
    except ValueError:
        print(json.dumps({"recorded": False, "why": "stdin is not JSON"}))
        return 2
    card = (((doc.get("extra_content") or {}).get("space.ag2.card")) if isinstance(doc, dict) else None) or doc
    ok = isinstance(card, dict) and record(Path(args.workspace), args.event_id, card, room=args.room, lane=args.lane,
                                        handler=args.handler)
    print(json.dumps({"recorded": bool(ok)}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
