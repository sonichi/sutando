"""The agent panel cards this agent itself posted, so a card reply counts only as an answer to one of them.

Any room member can post a message that looks like a card, and the owner's tap on its button sends a
`card:` payload as the owner. So a payload is acted on only when the owner's message replies to an event
recorded here (written when this agent posted the card) and the payload is one that card offered.
One JSON file per workspace: event id -> {card id, the exact reply payloads offered, expiry}. The caller
passes the workspace; nothing here resolves it. Standard library only, plus file_lock and local_record.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Mapping, Optional

LEDGER = ("state", "agent-cards", "issued.json")
TTL_S = 14 * 86400
MAX_CARDS = 500


def ledger_path(workspace: Path) -> Path:
    return Path(workspace).joinpath(*LEDGER)


def offered(card: Mapping[str, object]) -> list:
    """Every `reply` payload a button, row action, menu item, footer link or shuffle of `card` can send."""
    out: list = []

    def take(b: object) -> None:
        if not isinstance(b, dict):
            return
        if b.get("kind") == "reply" and isinstance(b.get("reply"), str):
            out.append(b["reply"])
        for item in b.get("items") or []:
            take(item)

    header = card.get("header")
    shuffle = header.get("shuffle") if isinstance(header, dict) else None
    if isinstance(shuffle, dict) and isinstance(shuffle.get("reply"), str):
        out.append(shuffle["reply"])
    for b in card.get("buttons") or []:
        take(b)
    take(card.get("footer_link"))
    for grp in card.get("groups") or []:
        for row in (grp.get("rows") or []) if isinstance(grp, dict) else []:
            for act in (row.get("actions") or []) if isinstance(row, dict) else []:
                take(act)
    return sorted(set(out))


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cards = data.get("cards") if isinstance(data, dict) else None
    return cards if isinstance(cards, dict) else {}


def record(workspace: Path, event_id: str, card: Mapping[str, object], *, now: Optional[float] = None,
           ttl_s: float = TTL_S) -> bool:
    """Remember that this agent posted `card` as `event_id`; False when it offers no reply to remember."""
    payloads = offered(card)
    if not (isinstance(event_id, str) and event_id.startswith("$")) or not payloads:
        return False
    from file_lock import locked_file
    from local_record import write_whole
    now = time.time() if now is None else now
    path = ledger_path(workspace)
    with locked_file(path.with_name(path.name + ".lock")):
        cards = {k: v for k, v in _read(path).items()
                 if isinstance(v, dict) and float(v.get("expires_at") or 0) > now}
        cards[event_id] = {"card_id": str(card.get("id") or ""), "payloads": payloads, "expires_at": now + ttl_s}
        newest = sorted(cards.items(), key=lambda kv: kv[1]["expires_at"])[-MAX_CARDS:]
        write_whole(path, {"cards": dict(newest)})
    return True


def offered_by(workspace: Path, event_id: str, payload: str, *, now: Optional[float] = None) -> Optional[str]:
    """The card id when `event_id` is a live card this agent posted that offered `payload`, else None."""
    entry = _read(ledger_path(workspace)).get(str(event_id or ""))
    now = time.time() if now is None else now
    if not isinstance(entry, dict) or float(entry.get("expires_at") or 0) <= now:
        return None
    if payload not in (entry.get("payloads") or []):
        return None
    return str(entry.get("card_id") or "") or None


def main(argv=None) -> int:
    """`record --workspace W --event-id $E` with the posted card (or its room.message.send payload) on stdin."""
    import argparse
    p = argparse.ArgumentParser(description="Remember a card this agent posted.")
    p.add_argument("verb", choices=("record",))
    p.add_argument("--workspace", required=True)
    p.add_argument("--event-id", required=True)
    args = p.parse_args(argv)
    try:
        doc = json.loads(sys.stdin.read())
    except ValueError:
        print(json.dumps({"recorded": False, "why": "stdin is not JSON"}))
        return 2
    card = (((doc.get("extra_content") or {}).get("space.ag2.card")) if isinstance(doc, dict) else None) or doc
    ok = isinstance(card, dict) and record(Path(args.workspace), args.event_id, card)
    print(json.dumps({"recorded": bool(ok)}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
