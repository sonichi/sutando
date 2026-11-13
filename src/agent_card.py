"""Agent panel cards (`space.ag2.card`, kind `agent_panel`, v 1): build, check, send, and read the replies.

A card rides on an ordinary room message as `content["space.ag2.card"]` beside a one-line plain `body`
that older clients show. `build` returns that pair, `problem` checks a card the way the web client does
(unknown fields allowed, required keys present, only http(s) links), and `send_card` posts one through
the s.space Action `room.message.send` with an injected Action client, so this module holds no HTTP
client of its own. `connect` and `connect_picker` actions sign the owner in to an app in the client and
send nothing back: whoever posts one arms the connect-apps wait first. A card button of kind `reply` sends its `reply` text (a sentence the owner can read)
as the clicker's reply to the card, with `space.ag2.card_click` beside it; its `command` (a
`card:<kind>:<target>:<verb>[:confirmed]` code) stays with this agent's record of the card
(issued_cards). `click_verdict` is the one decision on such a task: the owner's click on a card this
agent posted, in the room it was posted in, is its command or the owner's own words; any other click,
and card action text sent without one, is forged or stale and nothing acts on it; the rest is ordinary
chat. A v2 card's `pick` or `input` choice acts the same way when its card, revision, control and
value are the ones recorded, and only for the first task that claims its interaction id. `python3
src/agent_card.py click --task-file PATH` prints that verdict and the script that runs the command.
Standard library only, plus local_task_protocol, issued_cards and connections.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

CARD_KEY = "space.ag2.card"
KIND, VERSION = "agent_panel", 1
STATES = ("pending", "done", "expired", "error", "info")
BUTTON_KINDS = ("url", "reply", "open_room", "open_dm", "connect", "connect_picker", "menu")
# Where each placed kind may sit; every other kind may sit anywhere.
PLACES = {"menu": ("row",), "connect": ("button", "row"), "connect_picker": ("button", "footer")}
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
    """One avatar; the first given of mxid, url, seed, initial, app wins. The app picks the shape of a
    user's (mxid) and of generated (seed) artwork, so `shape` is sent only for the other three."""
    for key, value in (("mxid", mxid), ("url", url), ("seed", seed), ("initial", initial), ("app", app)):
        if value:
            return _drop_empty({key: value, "shape": "" if key in ("mxid", "seed") else shape})
    raise CardError("an avatar needs one of mxid, url, seed, initial or app")


def button(label: str, kind: str, *, style: str = "", url: str = "", reply: str = "", room_id: str = "",
           user_id: str = "", items: Optional[list] = None, confirm: str = "", disabled: Optional[bool] = None,
           uses_input: bool = False, command: str = "", toolkit: str = "", for_: str = "",
           suggest: Optional[list] = None) -> dict:
    """A button, row action or menu item; `confirm` makes the client ask first. On a done or expired
    card the client disables a button unless it says `disabled: False`. A `reply` button with a
    `command` acts as that command when the owner clicks it; without one its text is plain words. A
    `connect` action signs `for_` (default: the owner) in to `toolkit`, an app slug, on the card; a
    `connect_picker` searches every app there, listing the `suggest` slugs first."""
    if command and parse_reply(command) is None:
        raise CardError(f"{command!r} is not a card command")
    out = _drop_empty({"label": label, "kind": kind, "style": style, "url": url, "reply": reply,
                       "room_id": room_id, "user_id": user_id, "items": items, "confirm": confirm,
                       "command": command, "toolkit": toolkit, "for": for_,
                       "suggest": list(dict.fromkeys(suggest or []))})
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
          status: str = "", text: str = "", fields: Optional[list] = None, input_: Optional[dict] = None,
          groups: Optional[list] = None, buttons: Optional[list] = None, note: str = "", note_tone: str = "",
          expires_at_ms: Optional[int] = None, footer_link: Optional[dict] = None) -> Tuple[str, dict]:
    """(body, card) for one agent panel, checked: a card the client would not draw raises CardError.
    `status` is the pill beside the title ("Approved"): a short word with no glyph, since the client
    takes its tone and icon from `state`. A done card shows a check where the shuffle would be."""
    header = _drop_empty({"title": title, "subtitle": subtitle, "avatar": avatar_, "badges": badges,
                          "shuffle": {"reply": shuffle_reply} if shuffle_reply and state != "done" else None,
                          "status": {"label": status} if status else None})
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
          "pattern": 300, "label": 80, "confirm": 300, "status": 40, "badge": 40, "tag": 40, "field_label": 60,
          "field_value": 300, "group_label": 80, "note": 600, "suggest": 8, "mxid": 255, "seed": 128,
          "initial": 8, "avatar_url": 512, "url": 4096, "input_label": 200, "affix": 60, "hint": 300}
_APP_RE = re.compile(r"[a-z0-9_]{1,64}")
_INPUT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_USER_RE = re.compile(r"@[^\s:]+:\S+")
_ROOM_RE = re.compile(r"![^\s:]+:\S+")
_MXC_RE = re.compile(r"mxc://[^/\s?#]+/[^/\s?#]+")
_MAX_SAFE_INT = 2 ** 53 - 1


def _link_ok(url: object, schemes: Tuple[str, ...] = ("http", "https")) -> bool:
    """An absolute link in one of `schemes`, with a host and no embedded credentials."""
    if not isinstance(url, str) or len(url) > LIMITS["url"] or any(c.isspace() for c in url):
        return False
    import urllib.parse
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in schemes and bool(parts.netloc) and "@" not in parts.netloc


def _text(value: object, limit: str = "") -> bool:
    """Non-blank text, within LIMITS[limit] when one is named."""
    return isinstance(value, str) and bool(value.strip()) and (not limit or len(value) <= LIMITS[limit])


def _opt(value: object, limit: str) -> bool:
    """Absent, or bounded text: a present value of another shape is a bad card."""
    return value is None or _text(value, limit)


def _is(value: object, pattern: "re.Pattern", limit: str = "") -> bool:
    return isinstance(value, str) and bool(pattern.fullmatch(value)) and (not limit or len(value) <= LIMITS[limit])


def _avatar_problem(av: object, where: str) -> Optional[str]:
    """The client reads the first of mxid, url, seed, initial, app. It would draw the title's initial for
    an avatar with none of them or with an http(s) picture; a sender never means that, so both are refused."""
    if not isinstance(av, dict):
        return f"{where} needs one of {', '.join(AVATAR_KEYS)}"
    if av.get("shape") is not None and av["shape"] not in ("round", "square"):
        return f"{where}.shape must be round or square"
    key = next((k for k in AVATAR_KEYS if av.get(k) is not None), None)
    if key is None:
        return f"{where} needs one of {', '.join(AVATAR_KEYS)}"
    value = av[key]
    if key == "mxid" and not _is(value, _USER_RE, "mxid"):
        return f"{where}.mxid is not a user id"
    if key == "url" and not _is(value, _MXC_RE, "avatar_url"):
        return f"{where}.url must be an mxc:// link"
    if key in ("seed", "initial") and not _text(value, key):
        return f"{where}.{key} must be 1-{LIMITS[key]} characters"
    if key == "app" and not _is(value, _APP_RE):
        return f"{where}.app is not an app slug"
    return None


def _action_problem(b: object, where: str, place: str = "button") -> Optional[str]:
    """`place` is where the action sits: button, row (a row's actions), item (a menu's) or footer."""
    if not isinstance(b, dict) or not _text(b.get("label"), "label"):
        return f"{where} needs a label of at most {LIMITS['label']} characters"
    kind = b.get("kind")
    if kind not in BUTTON_KINDS:
        return f"{where}.kind {kind!r} is not one of {', '.join(BUTTON_KINDS)}"
    if place not in PLACES.get(kind, (place,)):
        return {"menu": f"{where}: a menu belongs only in a row's actions, and menu items do not nest",
                "connect": f"{where}: connect belongs only in buttons and row actions",
                "connect_picker": f"{where}: connect_picker belongs only in buttons and the footer link"}[kind]
    if not _opt(b.get("confirm"), "confirm"):
        return f"{where}.confirm must be 1-{LIMITS['confirm']} characters"
    if b.get("style") is not None and b["style"] not in STYLES:
        return f"{where}.style must be primary or secondary"
    for flag in ("disabled", "uses_input"):
        if b.get(flag) is not None and not isinstance(b[flag], bool):
            return f"{where}.{flag} must be true or false"
    if "command" in b and (kind != "reply" or parse_reply(b["command"]) is None):
        return f"{where}.command must be a card command on a reply button"
    if kind == "url" and not _link_ok(b.get("url")):
        return f"{where}.url is not an http(s) link without credentials"
    if kind == "reply" and not _text(b.get("reply"), "reply"):
        return f"{where} of kind reply needs a reply of at most {LIMITS['reply']} characters"
    if kind == "open_room" and not _is(b.get("room_id"), _ROOM_RE):
        return f"{where} of kind open_room needs a room_id"
    if kind == "open_dm" and not _is(b.get("user_id"), _USER_RE):
        return f"{where} of kind open_dm needs a user_id"
    if kind in ("connect", "connect_picker") and b.get("for") is not None and not _is(b["for"], _USER_RE, "mxid"):
        return f"{where}.for is not a user id"
    if kind == "connect" and not _is(b.get("toolkit"), _APP_RE):
        return f"{where}.toolkit is not an app slug"
    if kind == "connect_picker" and b.get("suggest") is not None:
        slugs = b["suggest"]
        if not isinstance(slugs, list) or len(slugs) > LIMITS["suggest"] or not all(_is(s, _APP_RE) for s in slugs):
            return f"{where}.suggest must be at most {LIMITS['suggest']} app slugs"
    if kind == "menu":
        items = b.get("items")
        if not isinstance(items, list) or not items or len(items) > LIMITS["menu_items"]:
            return f"{where} of kind menu needs 1-{LIMITS['menu_items']} items"
        for i, item in enumerate(items):
            found = _action_problem(item, f"{where}.items[{i}]", "item")
            if found:
                return found
    return None


def _input_problem(inp: object) -> Optional[str]:
    if not isinstance(inp, dict) or not _is(inp.get("name"), _INPUT_NAME_RE):
        return "input.name must be a letter or _ then up to 63 letters, digits or _"
    for key, limit in (("label", "input_label"), ("prefix", "affix"), ("suffix", "affix"), ("hint", "hint")):
        if not _opt(inp.get(key), limit):
            return f"input.{key} must be 1-{LIMITS[limit]} characters"
    value = inp.get("value")
    if value is not None and (not isinstance(value, str) or len(value) > LIMITS["input_value"]):
        return f"input.value is longer than {LIMITS['input_value']} characters"
    if inp.get("pattern") is not None:
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


def _row_problem(item: object, where: str) -> Optional[str]:
    if not isinstance(item, dict) or not _text(item.get("title"), "title"):
        return f"{where} needs a title of at most {LIMITS['title']} characters"
    for key in ("tag", "subtitle"):
        if not _opt(item.get(key), key):
            return f"{where}.{key} must be 1-{LIMITS[key]} characters"
    if item.get("state") is not None and item["state"] not in ROW_STATES:
        return f"{where}.state must be one of {', '.join(ROW_STATES)}"
    if item.get("avatar") is not None:
        found = _avatar_problem(item["avatar"], f"{where}.avatar")
        if found:
            return found
    found = _many(item, "actions", "row_actions")
    if found:
        return f"{where}.{found}"
    for a, act in enumerate(item.get("actions") or []):
        found = _action_problem(act, f"{where}.actions[{a}]", "row")
        if found:
            return found
    return None


def _header_problem(header: object) -> Optional[str]:
    if not isinstance(header, dict) or not _text(header.get("title")):
        return "header.title is missing"
    for key in ("title", "subtitle"):
        if not _opt(header.get(key), key):
            return f"header.{key} must be 1-{LIMITS[key]} characters"
    if header.get("avatar") is not None:
        found = _avatar_problem(header["avatar"], "header.avatar")
        if found:
            return found
    found = _many(header, "badges")
    if found:
        return "header." + found
    for i, badge in enumerate(header.get("badges") or []):
        if (not isinstance(badge, dict) or not _text(badge.get("label"), "badge")
                or (badge.get("state") is not None and badge["state"] not in BADGE_STATES)):
            return f"header.badges[{i}] needs a label and a state of on, off or warn"
    shuffle = header.get("shuffle")
    if shuffle is not None and not (isinstance(shuffle, dict) and _text(shuffle.get("reply"), "reply")):
        return "header.shuffle needs a reply"
    status = header.get("status")
    if status is not None and not (isinstance(status, dict) and _text(status.get("label"), "status")):
        return f"header.status needs a label of at most {LIMITS['status']} characters"
    return None


def problem(card: object) -> Optional[str]:
    """Why the client would fall back to the plain body for `card`, or None when it draws it. Unknown
    fields are allowed; a missing state reads as info and a missing note_tone as dim."""
    if not isinstance(card, dict):
        return "the card is not an object"
    if card.get("kind") != KIND or card.get("v") != VERSION or isinstance(card.get("v"), bool):
        return f"kind must be {KIND} and v {VERSION}"
    if not _text(card.get("id"), "id"):
        return f"id must be 1-{LIMITS['id']} characters"
    if card.get("state") is not None and card["state"] not in STATES:
        return f"state must be one of {', '.join(STATES)}"
    found = _header_problem(card.get("header"))
    if found:
        return found
    if not _opt(card.get("text"), "text"):
        return f"text must be 1-{LIMITS['text']} characters"
    for key in ("fields", "buttons", "groups"):
        found = _many(card, key)
        if found:
            return found
    for i, field in enumerate(card.get("fields") or []):
        if (not isinstance(field, dict) or not _text(field.get("label"), "field_label")
                or not _text(field.get("value"), "field_value")):
            return f"fields[{i}] needs a label and a value"
    if card.get("input") is not None:
        found = _input_problem(card["input"])
        if found:
            return found
    rows = 0
    for g, grp in enumerate(card.get("groups") or []):
        if not isinstance(grp, dict) or not isinstance(grp.get("rows"), list) or not grp["rows"]:
            return f"groups[{g}] needs rows"
        if not _opt(grp.get("label"), "group_label"):
            return f"groups[{g}].label must be 1-{LIMITS['group_label']} characters"
        rows += len(grp["rows"])
        if rows > LIMITS["rows"]:
            return f"the groups hold more than {LIMITS['rows']} rows"
        for r, item in enumerate(grp["rows"]):
            found = _row_problem(item, f"groups[{g}].rows[{r}]")
            if found:
                return found
    for i, b in enumerate(card.get("buttons") or []):
        found = _action_problem(b, f"buttons[{i}]", "button")
        if found:
            return found
    if not _opt(card.get("note"), "note"):
        return f"note must be 1-{LIMITS['note']} characters"
    if card.get("note_tone") is not None and card["note_tone"] not in NOTE_TONES:
        return "note_tone must be dim, warn or ok"
    expires = card.get("expires_at")
    if expires is not None and (isinstance(expires, bool) or not isinstance(expires, int)
                                or not 0 < expires <= _MAX_SAFE_INT):
        return "expires_at must be milliseconds since the epoch"
    if card.get("footer_link") is not None:
        found = _action_problem(card["footer_link"], "footer_link", "footer")
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
    """The message's `space.ag2.card_click` as the bridge passed it on ({card_id, action}, or a v2
    choice), or None when the task carries none or one that cannot be used (`click_tagged` tells those
    apart)."""
    import json
    import local_task_protocol as ltp
    try:
        click = json.loads(str(headers.get("card_click") or ""))
    except ValueError:
        return None
    return ltp.card_click_fields(click)


def click_tagged(headers: Mapping[str, str]) -> bool:
    """Whether the task carries a `card_click` header at all, usable or not: a tap, never typed words."""
    return bool(str(headers.get("card_click") or "").strip())


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


NOT_A_CARD, FORGED, WORDS, COMMAND = "not_a_card", "forged_or_stale_click", "owner_words", "command"
# A v2 choice another task already claimed: nothing runs and nothing is said.
DUPLICATE = "duplicate"
# What the agent says, once, to a click it cannot match; it then does nothing else with the message.
UNMATCHED_CLICK = ("I couldn't match that button to a card I sent, so I haven't done anything. "
                   "Tell me what you'd like in your own words.")
# The same, for a button's own text sent as a reply to the card without a tap.
UNTAPPED_ACTION = ("That reads like a button on my card, but it didn't arrive as a tap, so I haven't done "
                   "anything. Tap the button, or tell me what you'd like in your own words.")
_PICKER_ASK = re.compile(r"Connect \S.{0,200}")
_HANDLER_RE = re.compile(r"skills/[A-Za-z0-9_-]+/scripts/[A-Za-z0-9_-]+\.py( [a-z-]+)?")


@dataclass(frozen=True)
class Click:
    """What a task is to this agent's cards: `verdict`, the `command` of a COMMAND, why a FORGED click
    was refused and the fixed line to `say` for it, and the card's handler script and connection as its
    poster recorded them."""
    verdict: str
    command: Optional[Reply] = None
    why: str = ""
    handler: str = ""
    lane: str = ""
    say: str = UNMATCHED_CLICK
    choice: Optional[Mapping[str, object]] = None


def lane_owner(workspace, default: str = "") -> Callable[[str], str]:
    """lane -> its owner's mxid, from this machine's core connections; `default` for a card with no lane."""
    def owner(lane: str) -> str:
        if not lane:
            return default
        import connections
        return next((str(r.get("owner_mxid") or "") for r in connections.read_rows(workspace)
                     if r.get("instance") == lane and not r.get("worker")), "")
    return owner


_NOT_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _value_problem(control: Mapping[str, object], value: Optional[str]) -> Optional[str]:
    if value is None:
        return "the choice carries no value"
    if control.get("kind") == "pick":
        options = control.get("options")
        return None if isinstance(options, list) and value in options else "that card did not offer that value"
    low, high = control.get("min_length"), control.get("max_length")
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (low, high)):
        return "the field's bounds are unreadable"
    if not value.strip() or _NOT_TEXT.search(value):
        return "the value is not one line of text"
    if not low <= len(value) <= high:
        return "the value is outside the field's bounds"
    return None


def _choice_verdict(headers: Mapping[str, str], click: Mapping[str, object], entry: Optional[dict],
                    workspace, owner_of: Callable[[str], str], now: Optional[float]) -> Click:
    """A v2 pick or input choice: COMMAND with the choice, once per interaction id, else FORGED."""
    import issued_cards
    if entry is None or str(entry.get("card_id") or "") != click["card_id"]:
        return Click(FORGED, why="it does not answer a card I posted")
    owner = owner_of(str(entry.get("lane") or ""))
    why = ("it is not in the room the card was posted in"
           if str(entry.get("room") or "") != str(headers.get("channel_id") or "")
           else owner_may_act(headers, owner, dm_only=True) if owner else "the owner is not known")
    if why:
        return Click(FORGED, why=why)
    if entry.get("v") != 2 or entry.get("revision") != click["revision"]:
        return Click(FORGED, why="the card has changed since that choice was made")
    controls = entry.get("controls")
    control = controls.get(click["action"]) if isinstance(controls, dict) else None
    if not isinstance(control, dict) or control.get("kind") not in issued_cards.V2_CHOICE_PLACES:
        return Click(FORGED, why="that card did not offer it")
    why = _value_problem(control, click.get("value"))
    if why:
        return Click(FORGED, why=why)
    claimant = str(headers.get("id") or "").strip()
    if not claimant:
        return Click(FORGED, why="the task has no id to claim the choice with")
    choice = {"card_id": click["card_id"], "revision": click["revision"], "control": click["action"],
              "kind": control["kind"], "value": click["value"], "interaction_id": click["interaction_id"]}
    claimed = issued_cards.claim(workspace, click["interaction_id"], claimant, choice,
                                 expires_at=float(entry.get("expires_at") or 0), now=now)
    if claimed == issued_cards.DUPLICATE:
        return Click(DUPLICATE, why="that choice was already submitted")
    if claimed not in (issued_cards.CLAIMED, issued_cards.SAME):
        return Click(FORGED, why="the choice could not be claimed")
    return Click(COMMAND, handler=str(entry.get("handler") or ""), lane=str(entry.get("lane") or ""), choice=choice)


def click_verdict(headers: Mapping[str, str], said: str, workspace, owner_of: Callable[[str], str],
                  *, now: Optional[float] = None) -> Click:
    """COMMAND or WORDS for the owner's click on a card this agent posted (the owner of the connection it
    was posted on, in the room it was posted in, in reply to its event, with a text it offered). FORGED
    for any other click, for a click header that cannot be read, and for a card command's text sent
    without a click. Without a click, the owner's other words in reply to their card are WORDS, and
    everything else is NOT_A_CARD."""
    import issued_cards
    entry = issued_cards.live(workspace, str(headers.get("reply_to_event") or ""), now=now)
    click = card_click(headers)
    if click is None and click_tagged(headers):
        return Click(FORGED, why="its click header cannot be read")
    if click is not None and click.get("v") == 2:
        return _choice_verdict(headers, click, entry, workspace, owner_of, now)
    if click is not None and click["action"] != said:
        return Click(FORGED, why="the click names another action than the words")
    if click is None and parse_reply(said) is not None:
        return Click(FORGED, why="it is a card's command without a click", say=UNTAPPED_ACTION)
    if click is None and entry is None:
        return Click(NOT_A_CARD)
    if entry is None or (click is not None and str(entry.get("card_id") or "") != click["card_id"]):
        return Click(FORGED, why="it does not answer a card I posted")
    owner = owner_of(str(entry.get("lane") or ""))
    why = ("it is not in the room the card was posted in"
           if str(entry.get("room") or "") != str(headers.get("channel_id") or "")
           else owner_may_act(headers, owner) if owner else "the owner is not known")
    if click is None:
        if why:
            return Click(NOT_A_CARD)
        if entry["offers"].get(said):
            return Click(FORGED, why="it is a button's text without a click", say=UNTAPPED_ACTION)
        return Click(WORDS)
    if why:
        return Click(FORGED, why=why)
    command = entry["offers"].get(said)
    if command:
        parsed = parse_reply(command)
        if parsed is None:
            return Click(FORGED, why="its command is unreadable")
        return Click(COMMAND, parsed, handler=str(entry.get("handler") or ""), lane=str(entry.get("lane") or ""))
    if command == "" or (entry.get("picker") and _PICKER_ASK.fullmatch(said)):
        return Click(WORDS)
    return Click(FORGED, why="that card did not offer it")


def click_answer(verdict: Click, said: str, mine: Tuple[str, ...] = ()) -> Optional[dict]:
    """What a card handler prints for a task that is not a command of its own kinds (`mine`): the fixed
    refusal for a forged click, the owner's words, ordinary chat, a duplicate choice, or another
    handler's card (with the v2 choice it runs on). None for its own command. A v2 choice is never None
    here: a handler that takes choices reads `verdict.choice` before calling this."""
    if verdict.verdict == FORGED:
        return {"status": FORGED, "say": verdict.say, "why": verdict.why}
    if verdict.verdict == DUPLICATE:
        return {"status": DUPLICATE, "why": verdict.why}
    if verdict.choice is not None:
        return {"status": "other_card", "card": verdict.choice["card_id"], "choice": dict(verdict.choice),
                "run": verdict.handler if _HANDLER_RE.fullmatch(verdict.handler) else None}
    if verdict.verdict == WORDS:
        return {"status": WORDS, "words": said}
    if verdict.verdict == NOT_A_CARD:
        return {"status": NOT_A_CARD}
    if verdict.command.kind not in mine:
        return {"status": "other_card", "card": verdict.command.kind,
                "run": verdict.handler if _HANDLER_RE.fullmatch(verdict.handler) else None}
    return None


def main(argv=None) -> int:
    """`click --task-file PATH [--workspace WS]`: one JSON line, the verdict on that task. Exit 0 for a
    command, the owner's words, ordinary chat or a duplicate choice; 1 for a forged or stale click; 2
    unreadable."""
    import argparse
    import json
    from pathlib import Path
    p = argparse.ArgumentParser(description="What a task is to the cards this agent posted.")
    p.add_argument("verb", choices=("click",))
    p.add_argument("--task-file", required=True)
    p.add_argument("--workspace", default=None)
    args = p.parse_args(argv)
    if args.workspace:
        ws = Path(args.workspace)
    else:
        from workspace_default import resolve_workspace
        ws = Path(resolve_workspace())
    try:
        headers, said = reply_words(Path(args.task_file).read_text(encoding="utf-8"))
    except OSError as e:
        print(json.dumps({"status": "refused", "why": f"the task file is unreadable ({type(e).__name__})"}))
        return 2
    out = click_answer(click_verdict(headers, said, ws, lane_owner(ws)), said)
    if out["status"] == "other_card":
        out["status"] = COMMAND
    print(json.dumps(out, sort_keys=True))
    return 1 if out["status"] == FORGED else 0


if __name__ == "__main__":
    sys.exit(main())
