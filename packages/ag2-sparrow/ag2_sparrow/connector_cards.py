"""Agent panel cards for a connector: an agent that is not the core has no skills folder and no room
client, so it queues a card for its bridge to post and judges the click itself.

A connector calls `queue` with a card built by the core's own `agent_card.build`. The bridge's `Poster`
posts each queued card as a room message carrying `space.ag2.card` and leaves the event id in
`posted/`; the connector's `absorb` then records the card in its own ledger (`issued_cards`, under its
work root), which is what makes a later click count. `click` is `agent_card.click_verdict` for a task:
only the owner's tap on a card this agent posted, in the DM it was posted in, is a command.

A card queued with `edit` (the event id of a card this agent posted, e.g. the one the owner tapped) is
shown in place of that card: the bridge sends it as an edit of that event, so the owner sees one card
whose words change, not a second card. The edit's card is recorded under the original event, the one
the owner sees and taps. An edit the server refuses is posted as a new card instead.

Queue and posted files live under the bridge's state dir (`agent-cards/outbox`, `agent-cards/posted`);
the bridge never writes into the work root. Standard library only, plus the bundled card modules.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

OUTBOX, POSTED = ("agent-cards", "outbox"), ("agent-cards", "posted")
KEYS = ("agent-cards", "keys")     # one marker per keyed card ever queued, so a repeat is not a second card
KEYS_KEEP_S = 7 * 86400.0
CARD_KEY = "space.ag2.card"
MAX_QUEUED_S = 600.0
_NAME_RE = re.compile(r"^[0-9a-f]{32}\.json$")
# A room id with its server, or the newer kind that has none (room version 12).
_ROOM_RE = re.compile(r"^![^\s:]+(:\S+)?$")
_EVENT_RE = re.compile(r"^\$\S+$")


def _core():
    """The bundled card modules. They import their siblings by bare name, as in the core's src/."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import agent_card
    import issued_cards
    return agent_card, issued_cards


def _write(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> "dict | None":
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _entries(folder: Path) -> list:
    try:
        return sorted(p for p in folder.iterdir() if _NAME_RE.match(p.name))
    except OSError:
        return []


def queue(bridge_state, room: str, body: str, card: dict, *, now=None, key: str = "", at_ns: int = 0,
          edit: str = "") -> str:
    """Queue `card` for `room`; the queue id. A card the client would not draw is refused here. With
    `edit` (a posted card's event id) it is shown in place of that card instead of as a new one.

    With `key` (and the `at_ns` it was first decided at) the call may be repeated: the id is made of
    those two, and a card already queued under it is not queued again, so a caller that saved its
    intent, queued, and stopped before saving that it had, can simply queue again after a restart."""
    agent_card, _ = _core()
    found = agent_card.problem(card)
    if found:
        raise ValueError("the card would not render: %s" % found)
    if not _ROOM_RE.match(room or "") or not body or "\n" in body:
        raise ValueError("a card needs a room id and a one-line body")
    if edit and not _EVENT_RE.match(edit):
        raise ValueError("a card can only be shown in place of an event id")
    # The clock leads the id, so two cards queued between sweeps are posted in the order they were queued.
    if not key:
        qid = "%016x%s" % (time.time_ns(), uuid.uuid4().hex[:16])
    else:
        qid = "%016x%s" % (int(at_ns) & 0xFFFFFFFFFFFFFFFF, hashlib.sha256(key.encode("utf-8")).hexdigest()[:16])
        marker = Path(bridge_state).joinpath(*KEYS) / qid
        if marker.exists():
            return qid
    _write(Path(bridge_state).joinpath(*OUTBOX) / (qid + ".json"),
           dict({"room": room, "body": body, "card": card, "queued_at": time.time() if now is None else now},
                **({"edit": edit} if edit else {})))
    if key:
        # The marker follows the card: a stop between the two queues it once more, never drops it.
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
        _forget_old_keys(marker.parent)
    return qid


def _forget_old_keys(folder: Path) -> None:
    """Drop markers older than KEYS_KEEP_S: long past any restart that could repeat their card."""
    horizon = time.time() - KEYS_KEEP_S
    try:
        for marker in folder.iterdir():
            if marker.stat().st_mtime < horizon:
                marker.unlink(missing_ok=True)
    except OSError:
        pass


class Poster:
    """The bridge's half: post what connectors queued. `post(payload) -> answer` is the bridge's own
    room call; a card that could not be posted stays queued until it is MAX_QUEUED_S old."""

    def __init__(self, bridge_state, post, log=print, clock=time.time):
        self.state, self.post, self.log, self.clock = Path(bridge_state), post, log, clock

    def sweep(self) -> int:
        posted, held = 0, set()
        for path in _entries(self.state.joinpath(*OUTBOX)):
            record = _read(path)
            card = (record or {}).get("card")
            if not record or not isinstance(card, dict) or not _ROOM_RE.match(str(record.get("room") or "")):
                path.unlink(missing_ok=True)
                continue
            # A card waits behind an earlier one of the same card in the same room that is not posted
            # yet: posted out of order, the earlier one would arrive last and replace it.
            same = (record["room"], str(card.get("id") or ""))
            if same in held:
                continue
            if self.clock() - float(record.get("queued_at") or 0) > MAX_QUEUED_S:
                self.log("agent-card: dropped a card queued over %d s ago" % MAX_QUEUED_S)
                path.unlink(missing_ok=True)
                continue
            payload = {"op": "message", "room_id": record["room"], "body": str(record.get("body") or ""),
                       "extra_content": {CARD_KEY: card}, "dedupe_key": "agent-card:" + path.stem}
            target = str(record.get("edit") or "")
            try:
                _, event_id = self.send(payload, target if _EVENT_RE.match(target) else "")
            except Exception as e:  # noqa: BLE001 - stays queued, retried next sweep
                self.log("agent-card: post failed (will retry): %s" % type(e).__name__)
                held.add(same)
                continue
            if not event_id.startswith("$"):
                self.log("agent-card: the post returned no event id (will retry)")
                held.add(same)
                continue
            _write(self.state.joinpath(*POSTED) / path.name,
                   dict({"event_id": event_id, "room": record["room"], "card": card, "posted_at": self.clock()},
                        **({"edited": True} if target and event_id == target else {})))
            path.unlink(missing_ok=True)
            posted += 1
        return posted

    def send(self, payload, target: str = ""):
        """(answer, event id the card now shows at). With `target` the card is sent as an edit of that
        event, and stays recorded under it; an edit the server refuses (a 4xx: the event is not there, or not
        this agent's; or an answer that is not a success) is posted as a new card instead, so the owner is
        still told."""
        if target:
            try:
                answer = self.post(dict(payload, op="edit", event_id=target,
                                        dedupe_key="agent-card-edit:" + payload["dedupe_key"].split(":", 1)[1]))
            except Exception as e:  # noqa: BLE001 - only a refusal falls back; anything else is retried
                code = getattr(e, "code", None)
                code = code if isinstance(code, int) else getattr(e, "status", None)
                if not (isinstance(code, int) and 400 <= code < 500):
                    raise
                self.log("agent-card: edit refused (%s), posted as a new card" % code)
            else:
                if isinstance(answer, dict) and not answer.get("is_error") and (answer.get("ok") or answer.get("event_id")):
                    return answer, target
                self.log("agent-card: edit not accepted, posted as a new card")
        answer = self.post(payload)
        return answer, str((answer or {}).get("event_id") or "") if isinstance(answer, dict) else ""


def absorb(bridge_state, work_root, handler: str = "") -> int:
    """The connector's half: record each posted card in this agent's ledger, so its buttons can act."""
    _, issued_cards = _core()
    taken = 0
    for path in _entries(Path(bridge_state).joinpath(*POSTED)):
        record = _read(path)
        if record and isinstance(record.get("card"), dict):
            # An accepted edit: the card now shown at that event replaces the one tapped (issued_cards.record).
            issued_cards.record(Path(work_root), str(record.get("event_id") or ""), record["card"],
                                room=str(record.get("room") or ""), lane="", handler=handler,
                                edited=bool(record.get("edited")))
            taken += 1
        path.unlink(missing_ok=True)
    return taken


def click(headers: dict, said: str, work_root):
    """`agent_card.click_verdict` for a connector's task. The owner is the sender the broker attests
    as owner; `owner_may_act` still refuses any other tier and any room but the owner's DM."""
    agent_card, _ = _core()
    return agent_card.click_verdict(headers, (said or "").strip(), Path(work_root),
                                    lambda lane: str(headers.get("user_id") or ""))
