"""Agent panel cards (`space.ag2.card`, kind `agent_panel`, v 1): build, check, send, and read the replies.

A card rides on an ordinary room message as `content["space.ag2.card"]` beside a one-line plain `body`
that older clients show. `build` returns that pair, `problem` checks a card the way the web client does
(unknown fields allowed, required keys present, only http(s) links), and `send_card` posts one through
the s.space Action `room.message.send` with an injected Action client, so this module holds no HTTP
client of its own. A card button of kind `reply` sends its `reply` text (a sentence the owner can read)
as the clicker's reply to the card, with `space.ag2.card_click` beside it; its `command` (a
`card:<kind>:<target>:<verb>[:confirmed]` code) stays with this agent's record of the card
(issued_cards). `card_command` hands back that command only for the owner's click on a card this agent
posted that offered that text. Standard library only, plus local_task_protocol and issued_cards.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

CARD_KEY = "space.ag2.card"
KIND, VERSION = "agent_panel", 1
STATES = ("pending", "done", "expired", "error", "info")
BUTTON_KINDS = ("url", "reply", "open_room", "open_dm", "menu", "connect")
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
           user_id: str = "", items: Optional[list] = None, confirm: str = "", disabled: Optional[bool] = None,
           uses_input: bool = False, command: str = "", toolkit: str = "") -> dict:
    """A button, row action or menu item; `confirm` makes the client ask first. On a done or expired
    card the client disables a button unless it says `disabled: False`. A `reply` button with a
    `command` acts as that command when the owner clicks it; without one its text is plain words. A
    `connect` button signs the owner in to `toolkit` (an app slug) in place, in the client."""
    if command and parse_reply(command) is None:
        raise CardError(f"{command!r} is not a card command")
    out = _drop_empty({"label": label, "kind": kind, "style": style, "url": url, "reply": reply,
                       "room_id": room_id, "user_id": user_id, "items": items, "confirm": confirm,
                       "command": command, "toolkit": toolkit})
    if disabled is not None:
        out["disabled"] = bool(disabled)
    if uses_input:
        out["uses_input"] = True
    return out


def row(title: str, *, subtitle: str = "", tag: str = "", state: str = "", avatar_: Optional[dict] = None,
        actions: Optional[list] = None) -> dict:
    return _drop_empty({"title": title, "subtitle": subtitle, "tag": tag, "state": state, "avatar": avatar_,
                        "actions": actions})


def group(label: str, rows: list) -> dict:
    return {"label": label, "rows": list(rows)}


_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>])")


def md_escape(text: str) -> str:
    """`text` as literal markdown on one line: emphasis, code and link characters escaped, so a name
    cannot end a link label early or turn a URL into visible text."""
    return _MD_SPECIAL.sub(r"\\\1", " ".join(str(text or "").split()))


def md_url(url: str) -> str:
    """`url` as a markdown link target, which ends at a space or an unbalanced paren."""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def body_line(text: str, *, link_label: str = "", url: str = "") -> str:
    """The plain fallback: one line of literal text, any link labelled, never a bare URL."""
    line = md_escape(_URL_RE.sub("", str(text or "")))
    if url:
        line = f"{line} [{md_escape(link_label or 'Open')}]({md_url(url)})".strip()
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

# The web client's limits: past any of them it shows the plain body instead of the card.
LIMITS = {"id": 200, "title": 120, "subtitle": 300, "text": 4000, "badges": 8, "fields": 12, "buttons": 6,
          "groups": 10, "rows": 100, "row_actions": 4, "menu_items": 8, "reply": 1000, "input_value": 200,
          "pattern": 300}
_APP_RE = re.compile(r"[a-z0-9_]{1,64}")
_INPUT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _link_ok(url: object, schemes: Tuple[str, ...] = ("http", "https")) -> bool:
    """An absolute link in one of `schemes`, with a host and no embedded credentials."""
    if not isinstance(url, str) or any(c.isspace() for c in url):
        return False
    import urllib.parse
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in schemes and bool(parts.netloc) and "@" not in parts.netloc


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _long(value: object, limit: str) -> bool:
    return isinstance(value, str) and len(value) > LIMITS[limit]


def _avatar_problem(av: object, where: str) -> Optional[str]:
    if not isinstance(av, dict) or not any(_text(av.get(k)) for k in AVATAR_KEYS):
        return f"{where} needs one of {', '.join(AVATAR_KEYS)}"
    if "url" in av and not _link_ok(av["url"], ("mxc",)):
        return f"{where}.url must be an mxc:// link"
    if "app" in av and not (isinstance(av["app"], str) and _APP_RE.fullmatch(av["app"])):
        return f"{where}.app is not an app slug"
    if "shape" in av and av["shape"] not in ("round", "square"):
        return f"{where}.shape must be round or square"
    return None


def _action_problem(b: object, where: str, *, menu_ok: bool = False) -> Optional[str]:
    if not isinstance(b, dict) or not _text(b.get("label")):
        return f"{where} needs a label"
    kind = b.get("kind")
    if kind not in BUTTON_KINDS:
        return f"{where}.kind {kind!r} is not one of {', '.join(BUTTON_KINDS)}"
    if kind == "menu" and not menu_ok:
        return f"{where}: a menu belongs only in a row's actions, and menu items do not nest"
    need = {"url": "url", "reply": "reply", "open_room": "room_id", "open_dm": "user_id", "connect": "toolkit"}.get(kind)
    if need and not _text(b.get(need)):
        return f"{where} of kind {kind} needs {need}"
    if kind == "connect" and not (isinstance(b["toolkit"], str) and _APP_RE.fullmatch(b["toolkit"])):
        return f"{where}.toolkit is not an app slug"
    if "url" in b and not _link_ok(b["url"]):
        return f"{where}.url is not an http(s) link without credentials"
    if _long(b.get("reply"), "reply"):
        return f"{where}.reply is longer than {LIMITS['reply']} characters"
    if "style" in b and b["style"] not in STYLES:
        return f"{where}.style must be primary or secondary"
    if "disabled" in b and not isinstance(b["disabled"], bool):
        return f"{where}.disabled must be true or false"
    if "command" in b and (kind != "reply" or parse_reply(b["command"]) is None):
        return f"{where}.command must be a card command on a reply button"
    if kind == "menu":
        items = b.get("items")
        if not isinstance(items, list) or not items or len(items) > LIMITS["menu_items"]:
            return f"{where} of kind menu needs 1-{LIMITS['menu_items']} items"
        for i, item in enumerate(items):
            found = _action_problem(item, f"{where}.items[{i}]")
            if found:
                return found
    return None


def _input_problem(inp: object) -> Optional[str]:
    if not isinstance(inp, dict) or not (isinstance(inp.get("name"), str) and _INPUT_NAME_RE.fullmatch(inp["name"])):
        return "input.name must be a letter or _ then up to 63 letters, digits or _"
    if _long(inp.get("value"), "input_value"):
        return f"input.value is longer than {LIMITS['input_value']} characters"
    if "pattern" in inp:
        if not isinstance(inp["pattern"], str) or len(inp["pattern"]) > LIMITS["pattern"]:
            return f"input.pattern must be a regex of at most {LIMITS['pattern']} characters"
        try:
            re.compile(inp["pattern"])
        except re.error:
            return "input.pattern does not compile"
    return None


def _many(card: Mapping[str, object], key: str, limit: str = "") -> Optional[str]:
    value = card.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        return f"{key} must be a list"
    return f"{key} has more than {LIMITS[limit or key]}" if len(value) > LIMITS[limit or key] else None


def problem(card: object) -> Optional[str]:
    """Why the client would fall back to the plain body for `card`, or None when it draws it. Unknown
    fields are allowed; a missing state reads as info and a missing note_tone as dim."""
    if not isinstance(card, dict):
        return "the card is not an object"
    if card.get("kind") != KIND or card.get("v") != VERSION:
        return f"kind must be {KIND} and v {VERSION}"
    if not _text(card.get("id")) or _long(card["id"], "id"):
        return f"id must be 1-{LIMITS['id']} characters"
    if "state" in card and card["state"] not in STATES:
        return f"state must be one of {', '.join(STATES)}"
    header = card.get("header")
    if not isinstance(header, dict) or not _text(header.get("title")):
        return "header.title is missing"
    for key in ("title", "subtitle"):
        if key in header and (not isinstance(header[key], str) or _long(header[key], key)):
            return f"header.{key} is longer than {LIMITS[key]} characters"
    if _long(card.get("text"), "text") or ("text" in card and not isinstance(card["text"], str)):
        return f"text must be at most {LIMITS['text']} characters"
    if "avatar" in header:
        found = _avatar_problem(header["avatar"], "header.avatar")
        if found:
            return found
    found = _many(header, "badges")
    if found:
        return "header." + found
    for i, badge in enumerate(header.get("badges") or []):
        if not isinstance(badge, dict) or not _text(badge.get("label")) or badge.get("state", "on") not in BADGE_STATES:
            return f"header.badges[{i}] needs a label and a state of on, off or warn"
    shuffle = header.get("shuffle")
    if shuffle is not None and not (isinstance(shuffle, dict) and _text(shuffle.get("reply"))
                                    and not _long(shuffle["reply"], "reply")):
        return "header.shuffle needs a reply"
    for key in ("fields", "buttons", "groups"):
        found = _many(card, key)
        if found:
            return found
    for i, field in enumerate(card.get("fields") or []):
        if not isinstance(field, dict) or not _text(field.get("label")) or not isinstance(field.get("value"), str):
            return f"fields[{i}] needs a label and a value"
    if "input" in card:
        found = _input_problem(card["input"])
        if found:
            return found
    rows = 0
    for g, grp in enumerate(card.get("groups") or []):
        if not isinstance(grp, dict) or not isinstance(grp.get("rows"), list):
            return f"groups[{g}] needs rows"
        rows += len(grp["rows"])
        if rows > LIMITS["rows"]:
            return f"the groups hold more than {LIMITS['rows']} rows"
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
            found = _many(item, "actions", "row_actions")
            if found:
                return f"{where}.{found}"
            for a, act in enumerate(item.get("actions") or []):
                found = _action_problem(act, f"{where}.actions[{a}]", menu_ok=True)
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
    if card.get("footer_link") is not None:
        found = _action_problem(card["footer_link"], "footer_link")
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

    @property
    def payload(self) -> str:
        return REPLY_PREFIX + ":".join([self.kind, self.target or "-", self.verb] + ([CONFIRMED] if self.confirmed else []))


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


def reply_words(task_text: str) -> Tuple[dict, str]:
    """(headers, the sender's own words) of a gateway task file, the reply quote stripped."""
    import local_task_protocol as ltp
    parsed = ltp.parse_task_headers_trusted(task_text)
    words = ltp.sender_words(parsed)
    said = ltp.reply_action_text(words) or ltp.split_reply_context(words)[1]
    return dict(parsed.headers), said.strip()


def card_click(headers: Mapping[str, str]) -> Optional[dict]:
    """The message's `space.ag2.card_click` as the bridge passed it on ({card_id, action}), or None."""
    import json
    try:
        click = json.loads(str(headers.get("card_click") or ""))
    except ValueError:
        return None
    if not isinstance(click, dict) or not all(_text(click.get(k)) for k in ("card_id", "action")):
        return None
    return {"card_id": click["card_id"], "action": click["action"]}


def owner_may_act(headers: Mapping[str, str], owner_mxid: str = "", *, dm_only: bool = True) -> Optional[str]:
    """Why a card reply in this task must not act, or None when its sender is the owner."""
    import local_task_protocol as ltp
    if ltp.canonical_access_tier(headers.get("access_tier")) != "owner" or str(headers.get("collaborator") or "").strip():
        return "only the owner can use this card"
    sender = str(headers.get("user_id") or "").strip()
    if owner_mxid and sender != owner_mxid:
        return "only the owner can use this card"
    if sender and not _MXID_RE.fullmatch(sender):
        return "the reply names no sender"
    if dm_only and str(headers.get("channel_kind") or "").strip() != "dm":
        return "this card acts only from the owner's DM"
    return None


def card_command(headers: Mapping[str, str], said: str, owner_mxid: str, workspace,
                 *, now: Optional[float] = None) -> Tuple[Optional[Reply], str]:
    """(the command, "") for the owner's click on a card this agent posted, else (None, why it is ordinary
    chat). A click is `space.ag2.card_click` naming the card, in reply to that card's own event, whose
    text is one the card offered. Typed words, a forged card and anyone else's click never pass."""
    import issued_cards
    if not owner_mxid:
        return None, "the owner is not known"
    why = owner_may_act(headers, owner_mxid)
    if why:
        return None, why
    click = card_click(headers)
    if click is None or click["action"] != said:
        return None, "it is not a card click"
    command = issued_cards.command_for(workspace, str(headers.get("reply_to_event") or ""), click["card_id"],
                                       said, now=now)
    parsed = parse_reply(command or "")
    if parsed is None:
        return None, "it does not answer a card I posted with that action"
    return parsed, ""
