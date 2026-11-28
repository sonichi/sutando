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

A card queued with `below` answers a tap: it is posted as a new card under the tap, where the owner is
looking, and the tapped card is edited down to a line pointing there (no buttons). `moved/` keeps where
each tapped card went, so a later edit of it (a sign-in that ends, a code withdrawn) lands on the new
card instead of on the line left behind.

What became of a queued card outlives both files: the bridge leaves `outcomes/<queue id>` saying
`posted`, `dropped` (expired with no send ever tried) or `unsure` (expired after a send that failed or
did not answer, which may have gone through), kept as long as a key's marker. A connector that looks
after the receipt was absorbed still reads a fact (`outcome`) and never infers one from a file's
absence. An `unsure` card may be queued again under its own key (`again`): the same queue id, so the
same dedupe key at the server, which makes the repeat one message. Only the bridge writes or replaces
an outcome; a connector never removes one, so it cannot erase what a sweep has just recorded. A
requeued entry stays `attempted`: `unsure` stands until the bridge records `posted`.

Queue and posted files live under the bridge's state dir (`agent-cards/outbox`, `agent-cards/posted`);
the bridge never writes into the work root. Standard library only, plus the bundled card modules.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import time
import uuid
from pathlib import Path

OUTBOX, POSTED = ("agent-cards", "outbox"), ("agent-cards", "posted")
KEYS = ("agent-cards", "keys")     # one marker per keyed card ever queued, so a repeat is not a second card
OUTCOMES = ("agent-cards", "outcomes")     # what became of each queued card: posted, dropped or unsure
MOVED = ("agent-cards", "moved")       # a tapped card answered below: the event its answer is shown at
ANSWERED_BELOW = "Answered below."
POINTER_CARD = "answered-below"     # its own id: a record of it must not retire the answer, which shares the old one
KEYS_KEEP_S = 7 * 86400.0
CARD_KEY = "space.ag2.card"
MAX_QUEUED_S = 600.0
RETRY_BASE_S, RETRY_MAX_S = 5.0, 60.0     # the wait before a send with no verdict is tried again, doubling
# The statuses that refuse one edit for good: its target is not there or not this agent's, or its payload is bad.
EDIT_REFUSED = frozenset({400, 403, 404, 410, 413, 422})
THROTTLED = frozenset({408, 425, 429, 500, 502, 503, 504})


class NoVerdict(Exception):
    """The relay answered an edit without accepting or refusing it."""
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


def _private_dirs(folder: Path) -> None:
    """`folder` (agent-cards/outbox or /posted) and agent-cards made owner-only whatever the umask: a card may
    carry a short-lived sign-in code."""
    folder.mkdir(parents=True, exist_ok=True)
    for d in (folder.parent, folder):
        if (d.stat().st_mode & 0o777) != 0o700:
            os.chmod(d, 0o700)


def _write(path: Path, record: dict) -> None:
    """`record` at `path`, owner-only from its creation on, replaced in one step."""
    _private_dirs(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        if hasattr(os, "fchmod"):
            os.fchmod(f.fileno(), 0o600)       # a temp left by an older writer keeps its mode on O_TRUNC
        f.write(json.dumps(record, sort_keys=True))
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
          edit: str = "", again: bool = False, below: bool = False) -> str:
    """Queue `card` for `room`; the queue id. A card the client would not draw is refused here. With
    `edit` (a posted card's event id) it is shown in place of that card instead of as a new one; with
    `below` too, it is posted under the tap and the tapped card points to it.

    With `key` (and the `at_ns` it was first decided at) the call may be repeated: the id is made of
    those two, and a card already queued under it is not queued again, so a caller that saved its
    intent, queued, and stopped before saving that it had, can simply queue again after a restart.
    `again` queues a keyed card whose outcome is `unsure` once more, under the same id; any other is left."""
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
        entry = Path(bridge_state).joinpath(*OUTBOX) / (qid + ".json")
        requeue = marker.exists() and again and outcome(bridge_state, qid) == "unsure" and not entry.is_file()
        if marker.exists() and not requeue:
            return qid
    _write(Path(bridge_state).joinpath(*OUTBOX) / (qid + ".json"),
           dict({"room": room, "body": body, "card": card, "queued_at": time.time() if now is None else now},
                **({"edit": edit} if edit else {}), **({"below": True} if edit and below else {}),
                **({"attempted": True} if key and requeue else {})))
    if key:
        # The marker follows the card: a stop between the two queues it once more, never drops it.
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
        _forget_old_keys(marker.parent)
    return qid


def _settle(bridge_state, qid: str, what: str) -> None:
    """Record what became of the queued card `qid`. Written before its outbox entry is removed, so the
    entry being gone always has a recorded reason; one that cannot be written leaves the entry queued."""
    path = Path(bridge_state).joinpath(*OUTCOMES) / qid
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(what, encoding="utf-8")
    os.replace(tmp, path)
    _forget_old_keys(path.parent)


def outcome(bridge_state, qid: str) -> str:
    """'posted', 'dropped' or 'unsure' once the bridge has settled the queued card `qid`; '' while it
    has not (still queued, never queued, or settled longer ago than KEYS_KEEP_S)."""
    if not re.match(r"^[0-9a-f]{32}$", str(qid or "")):
        return ""
    try:
        said = (Path(bridge_state).joinpath(*OUTCOMES) / qid).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""
    return said if said in ("posted", "dropped", "unsure") else ""


def withdraw(bridge_state, qid: str) -> bool:
    """Take back the queued card `qid` before any send of it was tried: settled as dropped, its entry removed.
    False when it is not queued, or a send was already tried (that one stands, whatever became of it)."""
    if not re.match(r"^[0-9a-f]{32}$", str(qid or "")):
        return False
    path = Path(bridge_state).joinpath(*OUTBOX) / (qid + ".json")
    record = _read(path)
    if not record or record.get("attempted"):
        return False
    try:
        _settle(bridge_state, qid, "dropped")
        path.unlink(missing_ok=True)
    except OSError:
        return False        # left queued: the bridge posts it or drops it as before
    return True


def _moved_path(bridge_state, event: str) -> Path:
    return Path(bridge_state).joinpath(*MOVED) / hashlib.sha256(event.encode("utf-8")).hexdigest()[:32]


def moved_to(bridge_state, event: str) -> str:
    """The event the answer to a tap on `event` was posted at; `event` when it was answered in place. One hop
    only: a card answered below and then tapped again is its own card, and an edit of the first stays there."""
    try:
        nxt = _moved_path(bridge_state, event).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return event
    return nxt if _EVENT_RE.match(nxt) else event


def pointer(card: dict) -> dict:
    """What the tapped card becomes once its answer is posted below: its header, one line, no buttons."""
    header = {k: v for k, v in (card.get("header") or {}).items() if k != "shuffle"}
    return {"kind": card["kind"], "v": card["v"], "id": POINTER_CARD, "state": "info", "header": header,
            "text": ANSWERED_BELOW}


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
                _settle(self.state, path.stem, "dropped")
                path.unlink(missing_ok=True)
                continue
            # A card waits behind an earlier one of the same card in the same room that is not posted
            # yet: posted out of order, the earlier one would arrive last and replace it.
            target = str(record.get("edit") or "")
            target = moved_to(self.state, target) if _EVENT_RE.match(target) else ""
            # Answered under the tap: only the first answer to a tapped card moves; later ones edit it there.
            below = bool(record.get("below")) and target == str(record.get("edit") or "")
            # So does an edit behind an earlier edit of the same event, whatever card each one carries.
            same = {(record["room"], "card", str(card.get("id") or ""))} | ({(record["room"], "event", target)} if target else set())
            if same & held:
                continue
            # Already posted, by a sweep that then failed to finish: that stands, whatever its age now.
            if outcome(self.state, path.stem) == "posted" or (self.state.joinpath(*POSTED) / path.name).is_file():
                _settle(self.state, path.stem, "posted")
                path.unlink(missing_ok=True)
                continue
            if self.clock() - float(record.get("queued_at") or 0) > MAX_QUEUED_S:
                self.log("agent-card: dropped a card queued over %d s ago" % MAX_QUEUED_S)
                _settle(self.state, path.stem, "unsure" if record.get("attempted") else "dropped")
                path.unlink(missing_ok=True)
                continue
            if self.clock() < float(record.get("not_before") or 0):
                held |= same            # backing off after a send with no verdict: nothing overtakes it
                continue
            if not record.get("attempted"):     # saved before the send: an expiry after it is never a clean drop
                record = dict(record, attempted=True)
                _write(path, record)
            payload = {"op": "message", "room_id": record["room"], "body": str(record.get("body") or ""),
                       "extra_content": {CARD_KEY: card}, "dedupe_key": "agent-card:" + path.stem}
            try:
                _, event_id = self.send_below(payload, target, card) if below else self.send(payload, target)
            except Exception as e:  # noqa: BLE001 - stays queued, retried under the same key
                self.log("agent-card: post failed (will retry): %s" % type(e).__name__)
                code = getattr(e, "code", None)
                if isinstance(e, (NoVerdict, TimeoutError, socket.timeout)) or (
                        code if isinstance(code, int) else getattr(e, "status", None)) in THROTTLED:
                    # Told to come back later, timed out, or answered with no verdict: the next try waits, doubling.
                    tries = int(record.get("tries") or 0) + 1
                    _write(path, dict(record, tries=tries, not_before=self.clock() + min(RETRY_MAX_S, RETRY_BASE_S * 2 ** (tries - 1))))
                held |= same
                continue
            if not event_id.startswith("$"):
                self.log("agent-card: the post returned no event id (will retry)")
                held |= same
                continue
            _write(self.state.joinpath(*POSTED) / path.name,
                   dict({"event_id": event_id, "room": record["room"], "card": card, "posted_at": self.clock()},
                        **({"edited": True} if target and event_id == target else {})))
            if below and event_id != target:
                _private_dirs(self.state.joinpath(*MOVED))
                _moved_path(self.state, target).write_text(event_id, encoding="utf-8")
                # The tapped card's buttons stop acting, as after an edit in place: its record becomes the pointer.
                stub = hashlib.sha256((path.stem + ":moved").encode("utf-8")).hexdigest()[:32] + ".json"
                _write(self.state.joinpath(*POSTED) / stub, {"event_id": target, "room": record["room"],
                                                             "card": pointer(card), "posted_at": self.clock(), "edited": True})
            _settle(self.state, path.stem, "posted")
            path.unlink(missing_ok=True)
            posted += 1
        return posted

    def send_below(self, payload, target: str, card: dict):
        """(answer, event id) for a card answering a tap on `target`: posted as a new card, then `target`
        edited down to a line pointing to it. That edit is best effort: the answer stands without it."""
        answer = self.post(payload)
        event_id = str((answer or {}).get("event_id") or "") if isinstance(answer, dict) else ""
        if event_id.startswith("$"):
            stub = dict(payload, body=ANSWERED_BELOW, extra_content={CARD_KEY: pointer(card)}, op="edit",
                        event_id=target, dedupe_key="agent-card-moved:" + payload["dedupe_key"].split(":", 1)[1])
            try:
                if edit_verdict(self.post(stub)) != "accepted":
                    self.log("agent-card: the tapped card was not pointed to its answer")
            except Exception as e:  # noqa: BLE001 - the answer is posted; the old card keeps its words
                self.log("agent-card: the tapped card was not pointed to its answer (%s)" % type(e).__name__)
        return answer, event_id

    def send(self, payload, target: str = ""):
        """(answer, event id the card now shows at). With `target` the card is sent as an edit of that
        event, and stays recorded under it, once the relay answers with the edit's own event id. Only an
        edit the relay refuses for good (EDIT_REFUSED, or an answer saying so) is posted as a new card. A
        throttled, timed-out or unreadable answer raises: the edit is tried again under its own key."""
        if target:
            try:
                answer = self.post(dict(payload, op="edit", event_id=target,
                                        dedupe_key="agent-card-edit:" + payload["dedupe_key"].split(":", 1)[1]))
            except Exception as e:  # noqa: BLE001 - only a refusal for good falls back
                code = getattr(e, "code", None)
                code = code if isinstance(code, int) else getattr(e, "status", None)
                if code not in EDIT_REFUSED:
                    raise
                self.log("agent-card: edit refused (%s), posted as a new card" % code)
            else:
                verdict = edit_verdict(answer)
                if verdict == "accepted":
                    return answer, target
                if verdict != "refused":
                    raise NoVerdict("the edit has no verdict yet (%s)" % verdict)
                self.log("agent-card: edit not accepted, posted as a new card")
        answer = self.post(payload)
        return answer, str((answer or {}).get("event_id") or "") if isinstance(answer, dict) else ""


def edit_verdict(answer) -> str:
    """What the relay's answer to an edit says: 'accepted' (not a refusal, and it carries the edit's own
    event id), 'refused' (ok false or an error, for good), 'throttled' (told to come back later) or
    'unknown' (success-shaped with no event id, or nothing readable). Only 'refused' may become a new card."""
    if not isinstance(answer, dict):
        return "unknown"
    limited = str(answer.get("errcode") or answer.get("error") or "")
    if any(k in answer for k in ("retry_after_ms", "retry_after")) or "LIMIT_EXCEEDED" in limited.upper() \
            or answer.get("status") in THROTTLED:
        return "throttled"
    if answer.get("ok") is False or any(answer.get(k) for k in ("is_error", "error", "errcode")):
        return "refused"
    return "accepted" if str(answer.get("event_id") or "").startswith("$") else "unknown"


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
