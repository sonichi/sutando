"""Agent panel cards (`space.ag2.card`, kind `agent_panel`, v 1): build, check, send, and read the replies.

A card rides on an ordinary room message as `content["space.ag2.card"]` beside a one-line plain `body`
that older clients show. `build` returns that pair, `problem` checks a card the way the web client does
(unknown fields allowed, required keys present, only http(s) links), and `send_card` posts one through
the s.space Action `room.message.send` with an injected Action client, so this module holds no HTTP
client of its own. A card button of kind `reply` comes back as the clicker's reply to the card: an
ordinary task whose message is a `card:<kind>:<target>:<verb>[:confirmed]` payload, read by
`reply_payload` and gated by `owner_may_act`. Standard library only, plus local_task_protocol.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

CARD_KEY = "space.ag2.card"
KIND, VERSION = "agent_panel", 1
STATES = ("pending", "done", "expired", "error", "info")
BUTTON_KINDS = ("url", "reply", "open_room", "open_dm", "menu")
ROW_STATES = ("on", "off", "warn", "none")
BADGE_STATES = ("on", "off", "warn")
STYLES = ("primary", "secondary")
NOTE_TONES = ("dim", "warn", "ok")
AVATAR_KEYS = ("mxid", "url", "seed", "initial", "app")
REPLY_PREFIX = "card:"
CONFIRMED = "confirmed"
# room.message.edit takes no extra_content, and room.message.send keeps only space.ag2.* keys,
# so no Action can carry a card in an m.replace today; an update posts a new card instead.
EDIT_CARRIES_CARD = False
_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.I)
_PAYLOAD_PART = re.compile(r"[A-Za-z0-9._@!$=+-]{1,200}")
_MXID_RE = re.compile(r"@[A-Za-z0-9._=/+-]{1,200}:[A-Za-z0-9.-]{1,200}(:[0-9]{1,5})?")

# execute(action, arguments, *, operation_id, room_id) -> the Action's answer, from an injected Action client.
Execute = Callable[..., Mapping[str, object]]


class CardError(ValueError):
    """A card that would not render, or a send the server did not accept."""


class CardPrivacyError(CardError):
    """A private card addressed to a room that is not the owner's checked DM."""


# ── building ──────────────────────────────────────────────────────────────────

def _drop_empty(d: Mapping[str, object]) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def avatar(*, mxid: str = "", url: str = "", seed: str = "", initial: str = "", app: str = "",
           shape: str = "") -> dict:
    """One avatar shape; the first given of mxid, url, seed, initial, app wins."""
    for key, value in (("mxid", mxid), ("url", url), ("seed", seed), ("initial", initial), ("app", app)):
        if value:
            return _drop_empty({key: value, "shape": shape})
    raise CardError("an avatar needs one of mxid, url, seed, initial or app")


def button(label: str, kind: str, *, style: str = "", url: str = "", reply: str = "", room_id: str = "",
           user_id: str = "", items: Optional[list] = None, confirm: str = "", disabled: bool = False,
           uses_input: bool = False) -> dict:
    """A button, row action or menu item; `confirm` makes the client ask before it acts."""
    out = _drop_empty({"label": label, "kind": kind, "style": style, "url": url, "reply": reply,
                       "room_id": room_id, "user_id": user_id, "items": items, "confirm": confirm})
    if disabled:
        out["disabled"] = True
    if uses_input:
        out["uses_input"] = True
    return out


def row(title: str, *, subtitle: str = "", tag: str = "", state: str = "", avatar_: Optional[dict] = None,
        actions: Optional[list] = None) -> dict:
    return _drop_empty({"title": title, "subtitle": subtitle, "tag": tag, "state": state, "avatar": avatar_,
                        "actions": actions})


def group(label: str, rows: list) -> dict:
    return {"label": label, "rows": list(rows)}


def body_line(text: str, *, link_label: str = "", url: str = "") -> str:
    """The plain fallback: one line, any link labelled, never a bare URL."""
    line = " ".join(_URL_RE.sub("", str(text or "")).split())
    if url:
        line = f"{line} [{link_label or 'Open'}]({url})".strip()
    return line


def build(card_id: str, state: str, *, title: str, body: str, subtitle: str = "",
          avatar_: Optional[dict] = None, badges: Optional[list] = None, shuffle_reply: str = "",
          text: str = "", fields: Optional[list] = None, input_: Optional[dict] = None,
          groups: Optional[list] = None, buttons: Optional[list] = None, note: str = "", note_tone: str = "",
          expires_at_ms: Optional[int] = None, footer_link: Optional[dict] = None) -> Tuple[str, dict]:
    """(body, card) for one agent panel, checked: a card the client would not draw raises CardError."""
    header = _drop_empty({"title": title, "subtitle": subtitle, "avatar": avatar_, "badges": badges,
                          "shuffle": {"reply": shuffle_reply} if shuffle_reply else None})
    card = {"kind": KIND, "v": VERSION, "id": card_id, "state": state, "header": header}
    card.update(_drop_empty({"text": text, "fields": fields, "input": input_, "groups": groups,
                             "buttons": buttons, "note": note, "note_tone": note_tone,
                             "footer_link": footer_link}))
    if expires_at_ms is not None:
        card["expires_at"] = int(expires_at_ms)
    if not body or "\n" in body:
        raise CardError("the fallback body must be one non-empty line")
    found = problem(card)
    if found:
        raise CardError(f"the card would not render: {found}")
    return body, card


# ── checking ──────────────────────────────────────────────────────────────────

def _is_http(url: object) -> bool:
    return isinstance(url, str) and re.fullmatch(r"https?://\S+", url, re.I) is not None


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _avatar_problem(av: object, where: str) -> Optional[str]:
    if not isinstance(av, dict) or not any(_text(av.get(k)) for k in AVATAR_KEYS):
        return f"{where} needs one of {', '.join(AVATAR_KEYS)}"
    if "url" in av and not _is_http(av["url"]):
        return f"{where}.url is not an http(s) link"
    if "shape" in av and av["shape"] not in ("round", "square"):
        return f"{where}.shape must be round or square"
    return None


def _action_problem(b: object, where: str, *, in_menu: bool = False) -> Optional[str]:
    if not isinstance(b, dict) or not _text(b.get("label")):
        return f"{where} needs a label"
    kind = b.get("kind")
    if kind not in BUTTON_KINDS or (in_menu and kind == "menu"):
        return f"{where}.kind {kind!r} is not one of {', '.join(BUTTON_KINDS)}"
    need = {"url": "url", "reply": "reply", "open_room": "room_id", "open_dm": "user_id"}.get(kind)
    if need and not _text(b.get(need)):
        return f"{where} of kind {kind} needs {need}"
    if kind == "url" and not _is_http(b.get("url")):
        return f"{where}.url is not an http(s) link"
    if "style" in b and b["style"] not in STYLES:
        return f"{where}.style must be primary or secondary"
    if kind == "menu":
        items = b.get("items")
        if not isinstance(items, list) or not items:
            return f"{where} of kind menu needs items"
        for i, item in enumerate(items):
            found = _action_problem(item, f"{where}.items[{i}]", in_menu=True)
            if found:
                return found
    return None


def problem(card: object) -> Optional[str]:
    """Why a client would fall back to the plain body for `card`, or None when it draws it."""
    if not isinstance(card, dict):
        return "the card is not an object"
    if card.get("kind") != KIND or card.get("v") != VERSION:
        return f"kind must be {KIND} and v {VERSION}"
    if not _text(card.get("id")):
        return "id is missing"
    if card.get("state") not in STATES:
        return f"state must be one of {', '.join(STATES)}"
    header = card.get("header")
    if not isinstance(header, dict) or not _text(header.get("title")):
        return "header.title is missing"
    if "avatar" in header:
        found = _avatar_problem(header["avatar"], "header.avatar")
        if found:
            return found
    for i, badge in enumerate(header.get("badges") or []):
        if not isinstance(badge, dict) or not _text(badge.get("label")) or badge.get("state", "on") not in BADGE_STATES:
            return f"header.badges[{i}] needs a label and a state of on, off or warn"
    shuffle = header.get("shuffle")
    if shuffle is not None and not (isinstance(shuffle, dict) and _text(shuffle.get("reply"))):
        return "header.shuffle needs a reply"
    for i, field in enumerate(card.get("fields") or []):
        if not isinstance(field, dict) or not _text(field.get("label")) or not isinstance(field.get("value"), str):
            return f"fields[{i}] needs a label and a value"
    inp = card.get("input")
    if inp is not None and not (isinstance(inp, dict) and _text(inp.get("name"))):
        return "input needs a name"
    for g, grp in enumerate(card.get("groups") or []):
        if not isinstance(grp, dict) or not isinstance(grp.get("rows"), list):
            return f"groups[{g}] needs rows"
        for r, item in enumerate(grp["rows"]):
            where = f"groups[{g}].rows[{r}]"
            if not isinstance(item, dict) or not _text(item.get("title")):
                return f"{where} needs a title"
            if "state" in item and item["state"] not in ROW_STATES:
                return f"{where}.state must be one of {', '.join(ROW_STATES)}"
            if "avatar" in item:
                found = _avatar_problem(item["avatar"], f"{where}.avatar")
                if found:
                    return found
            for a, act in enumerate(item.get("actions") or []):
                found = _action_problem(act, f"{where}.actions[{a}]")
                if found:
                    return found
    for i, b in enumerate(card.get("buttons") or []):
        found = _action_problem(b, f"buttons[{i}]")
        if found:
            return found
    if "note_tone" in card and card["note_tone"] not in NOTE_TONES:
        return "note_tone must be dim, warn or ok"
    if "expires_at" in card and (isinstance(card["expires_at"], bool) or not isinstance(card["expires_at"], int)):
        return "expires_at must be milliseconds since the epoch"
    footer = card.get("footer_link")
    if footer is not None:
        found = _action_problem(footer, "footer_link")
        if found:
            return found
    return None


# ── sending ───────────────────────────────────────────────────────────────────

def _event_id(answer: Mapping[str, object]) -> str:
    result = answer.get("result")
    for source in (result if isinstance(result, dict) else {}, answer):
        eid = source.get("event_id") if isinstance(source, dict) else None
        if isinstance(eid, str) and eid.startswith("$"):
            return eid
    return ""


def send_card(execute: Execute, room_id: str, body: str, card: dict, *, operation_id: str,
              reply_to: str = "", thread_root: str = "", owner_dm: Optional[str] = None) -> str:
    """Post `card` in `room_id` with `room.message.send`; the new event's id. With `owner_dm` (the
    owner's checked DM, "" when none is known) the card is private and any other room is refused."""
    if owner_dm is not None and (not owner_dm or room_id != owner_dm):
        raise CardPrivacyError("this card carries the owner's own data and goes only to their DM; "
                               "the room it was addressed to is not that DM")
    found = problem(card)
    if found:
        raise CardError(f"the card would not render: {found}")
    if not operation_id:
        raise CardError("room.message.send needs an operation_id")
    arguments = {"body": body, "extra_content": {CARD_KEY: card}}
    if reply_to:
        arguments["reply_to"] = reply_to
    if thread_root:
        arguments["thread_root"] = thread_root
    answer = execute("room.message.send", arguments, operation_id=operation_id, room_id=room_id)
    eid = _event_id(answer) if isinstance(answer, Mapping) else ""
    if not eid or (isinstance(answer, Mapping) and answer.get("is_error")):
        code = ""
        if isinstance(answer, Mapping):
            code = str(answer.get("code") or answer.get("error") or answer.get("status") or "")
        raise CardError(f"room.message.send did not post the card{f' ({code})' if code else ''}")
    return eid


def update_card(execute: Execute, room_id: str, previous_event_id: str, body: str, card: dict, *,
                operation_id: str, owner_dm: Optional[str] = None) -> str:
    """Show `card` in place of the one at `previous_event_id`; the id of the event that now shows it.
    No Action carries a card in an edit (EDIT_CARRIES_CARD), so this posts the new card as a reply to
    the old one, which stays as it was."""
    return send_card(execute, room_id, body, card, operation_id=operation_id, reply_to=previous_event_id,
                     owner_dm=owner_dm)


# ── replies ───────────────────────────────────────────────────────────────────

def reply(kind: str, target: str, verb: str, *, confirmed: bool = False) -> str:
    """The payload a `reply` button sends: card:<kind>:<target>:<verb>[:confirmed]."""
    parts = [kind, target or "-", verb] + ([CONFIRMED] if confirmed else [])
    for part in parts:
        if not _PAYLOAD_PART.fullmatch(part) or ":" in part:
            raise CardError(f"{part!r} cannot be part of a card reply")
    return REPLY_PREFIX + ":".join(parts)


@dataclass(frozen=True)
class Reply:
    kind: str
    target: str
    verb: str
    confirmed: bool


def parse_reply(text: str) -> Optional[Reply]:
    """The card reply `text` is, or None when it is not exactly one."""
    text = (text or "").strip()
    if not text.startswith(REPLY_PREFIX) or any(c.isspace() for c in text):
        return None
    parts = text[len(REPLY_PREFIX):].split(":")
    confirmed = len(parts) == 4 and parts[3] == CONFIRMED
    if len(parts) != 3 and not confirmed:
        return None
    if not all(_PAYLOAD_PART.fullmatch(p) for p in parts):
        return None
    return Reply(parts[0], "" if parts[1] == "-" else parts[1], parts[2], confirmed)


def reply_payload(task_text: str) -> Tuple[dict, Optional[Reply]]:
    """(headers, card reply) of a gateway task file: the sender's own words, the reply quote stripped."""
    import local_task_protocol as ltp
    parsed = ltp.parse_task_headers_trusted(task_text)
    words = ltp.sender_words(parsed)
    said = ltp.reply_action_text(words) or ltp.split_reply_context(words)[1]
    return dict(parsed.headers), parse_reply(said.strip())


def owner_may_act(headers: Mapping[str, str], owner_mxid: str = "", *, dm_only: bool = True) -> Optional[str]:
    """Why a card reply in this task must not act, or None when its sender is the owner."""
    import local_task_protocol as ltp
    if ltp.canonical_access_tier(headers.get("access_tier")) != "owner":
        return "only the owner can use this card"
    sender = str(headers.get("user_id") or "").strip()
    if owner_mxid and sender != owner_mxid:
        return "only the owner can use this card"
    if sender and not _MXID_RE.fullmatch(sender):
        return "the reply names no sender"
    if dm_only and str(headers.get("channel_kind") or "").strip() != "dm":
        return "this card acts only from the owner's DM"
    return None
