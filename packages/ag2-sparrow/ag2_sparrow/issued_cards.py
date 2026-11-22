"""The agent panel cards this agent itself posted, so a card click counts only as an answer to one of them.

Any room member can post a message that looks like a card, and the owner's tap on its button sends its
reply text as the owner. So a click acts only when it names a card recorded here (written when this
agent posted it), in the room it was posted in, replies to that card's own event, and carries a text
that card offered. One JSON file per workspace: event id -> {card id, room, lane, handler, offered reply
text -> its command ("" for plain words), picker, expiry}. A newer card with the same id in the same
room replaces the older record, so a settled card's buttons stop acting. The caller passes the
workspace; nothing here resolves it. Standard library only, plus file_lock and local_record.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Mapping, Optional

LEDGER = ("state", "agent-cards", "issued.json")
TTL_S = 3 * 86400
MAX_CARDS = 500


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
    from file_lock import locked_file
    from local_record import write_whole
    now = time.time() if now is None else now
    path = ledger_path(workspace)
    with locked_file(path.with_name(path.name + ".lock")):
        cards = {k: v for k, v in _read(path).items()
                 if isinstance(v, dict) and float(v.get("expires_at") or 0) > now
                 and not (v.get("card_id") == card_id and str(v.get("room") or "") == room)}
        if payloads or picker:
            cards[event_id] = {"card_id": card_id, "offers": payloads, "picker": picker, "room": room,
                               "lane": lane, "handler": handler, "expires_at": now + ttl_s}
        newest = sorted(cards.items(), key=lambda kv: kv[1]["expires_at"])[-MAX_CARDS:]
        write_whole(path, {"cards": dict(newest)})
    return bool(payloads or picker)


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
