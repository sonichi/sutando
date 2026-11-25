"""The agent panel cards this agent itself posted, so a card click counts only as an answer to one of them.

Any room member can post a message that looks like a card, and the owner's tap on its button sends its
reply text as the owner. So a click acts only when it names a card recorded here (written when this
agent posted it), in the room it was posted in, replies to that card's own event, and carries a text
that card offered. One JSON file per workspace: event id -> {card id, room, lane, handler, offered reply
text -> its command ("" for plain words), picker, expiry}. A newer card with the same id in the same
room replaces the older record, so a settled card's buttons stop acting; the replaced event is kept as
retired, so a retried send the platform answers with it is a no-op and never displaces the newer card. The caller passes the
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
# Retired ids and send intents must outlive the live-card cap, or a replay under load finds neither.
MAX_RETIRED = 10 * MAX_CARDS


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


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read(path: Path) -> dict:
    cards = _load(path).get("cards")
    return cards if isinstance(cards, dict) else {}


def _retired(path: Path, now: float) -> dict:
    """{event id: {"why": "replaced" | "settled", "expires_at"}} of cards no longer live, unexpired."""
    got = _load(path).get("retired")
    return {k: v for k, v in (got.items() if isinstance(got, dict) else ())
            if isinstance(v, dict) and float(v.get("expires_at") or 0) > now}


def intend(workspace: Path, operation_id: str, *, now: Optional[float] = None) -> float:
    """Note, before the send, when the card sent as `operation_id` was first meant; a retried send keeps
    its first time. Pass the result to record() as `issued_at`, so a replayed older card never displaces
    a newer one even when its first record was lost."""
    from file_lock import locked_file
    from local_record import write_whole
    now = time.time() if now is None else now
    path = ledger_path(workspace)
    with locked_file(path.with_name(path.name + ".lock")):
        doc = _load(path)
        intents = {k: v for k, v in (doc.get("intents") or {}).items()
                   if isinstance(v, (int, float)) and v > now - TTL_S}
        # Strictly after every earlier intent, so two sends in one clock tick still have an order.
        first = intents.setdefault(str(operation_id), max([now] + [v + 1e-6 for v in intents.values()]))
        old = sorted(intents.items(), key=lambda kv: kv[1])[-MAX_RETIRED:]
        write_whole(path, {**doc, "intents": dict(old)})
    return float(first)


def record(workspace: Path, event_id: str, card: Mapping[str, object], *, room: str = "", lane: str = "",
           handler: str = "", now: Optional[float] = None, ttl_s: float = TTL_S,
           issued_at: Optional[float] = None, edited: bool = False) -> bool:
    """Remember that this agent posted `card` as `event_id` in `room` on connection `lane`; `handler` is
    the script that runs its commands. Older records of the same card id in that room are dropped; a card
    issued before a live one of its id (`issued_at`, from intend()) is retired instead. With `edited`,
    `card` is what `event_id` shows now (an accepted edit of a card this agent posted there): that event's
    earlier record, live or retired, gives way to it. False when the card offers no reply to remember, or
    was not kept."""
    if not (isinstance(event_id, str) and event_id.startswith("$")):
        return False
    payloads, picker, card_id = offered(card), has_picker(card), str(card.get("id") or "")
    from file_lock import locked_file
    from local_record import write_whole
    now = time.time() if now is None else now
    path = ledger_path(workspace)
    with locked_file(path.with_name(path.name + ".lock")):
        live_cards = {k: v for k, v in _read(path).items()
                      if isinstance(v, dict) and float(v.get("expires_at") or 0) > now}
        retired = _retired(path, now)
        # An event the platform hands back for a retried send is no newer card: a superseded or settled one
        # stays retired (a settled card's buttons never act twice), and a live one replaces nothing.
        if edited:
            was = live_cards.pop(event_id, None)
            retired.pop(event_id, None)
            if not (payloads or picker):    # the edit has no buttons: the ones it replaced never act again
                retired[event_id] = {"why": "settled", "expires_at": _retire_until(was, now)}
        elif event_id in retired:
            return False
        cards = dict(live_cards)
        order = now if issued_at is None else float(issued_at)
        if event_id not in live_cards:
            same = {k: v for k, v in live_cards.items()
                    if v.get("card_id") == card_id and str(v.get("room") or "") == room}
            if any(float(v.get("issued_at") or 0) > order for v in same.values()):
                retired[event_id] = {"why": "replaced", "expires_at": _retire_until(None, now)}
                _save(path, cards, retired)
                return False
            for k, v in same.items():
                cards.pop(k)
                retired[k] = {"why": "replaced", "expires_at": _retire_until(v, now)}
        if payloads or picker:
            cards[event_id] = {"card_id": card_id, "offers": payloads, "picker": picker, "room": room,
                               "lane": lane, "handler": handler, "expires_at": now + ttl_s, "issued_at": order}
        _save(path, cards, retired)
    return bool(payloads or picker)


def _save(path: Path, cards: dict, retired: dict) -> None:
    """Write the ledger under the caller's lock, keeping the send intents it holds."""
    from local_record import write_whole
    newest = sorted(cards.items(), key=lambda kv: kv[1]["expires_at"])[-MAX_CARDS:]
    old = sorted(retired.items(), key=lambda kv: float(kv[1].get("expires_at") or 0))[-MAX_RETIRED:]
    write_whole(path, {**_load(path), "cards": dict(newest), "retired": dict(old)})


def _retire_until(entry: object, now: float) -> float:
    """How long a retired id stays known: a full TTL from now at least, past a short-lived card's own life,
    so a send retried after the card expired still finds it retired."""
    own = float(entry.get("expires_at") or 0) if isinstance(entry, dict) else 0.0
    return max(own, now + TTL_S)


def settle(workspace: Path, event_id: str, *, now: Optional[float] = None) -> bool:
    """Forget the card posted as `event_id`, so no button of it acts again; False when it was not known."""
    from file_lock import locked_file
    from local_record import write_whole
    path = ledger_path(workspace)
    with locked_file(path.with_name(path.name + ".lock")):
        cards = _read(path)
        entry = cards.pop(str(event_id or ""), None)
        if entry is None:
            return False
        retired = _load(path).get("retired") or {}
        retired[str(event_id)] = {"why": "settled",
                                  "expires_at": _retire_until(entry, time.time() if now is None else now)}
        write_whole(path, {**_load(path), "cards": cards, "retired": retired})
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
