"""The human-readable face of a card: what a person reads, and in what order.

A card's human-facing strings — `message`, `title` and every action `label` —
carry no links, no machine or host names and no digests, so what is left is
what a person needs in order to decide. Disappearing from human-readable
content is not disappearing from the protocol: `guard`, `device`, `subject`
and every other wire field keep the producer's values, and no key is added or
dropped. One owner, because the projector renders its plain-text body from the
dict `card()` returns and the follow-up notices take their headline from
`headline()` — neither can say something the card does not.

Numbering is the terminal's own: an action bound to a terminal option is
labelled with that option's number and ordered by it. Nothing is invented,
padded or appended for a client's sake.

A follow-up notice is not a card — it stands alone, one per seat — so
`headline()` drops links and digests but keeps the seat's name, which is how
the owner knows which machine is still waiting.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .schema import HumanRequirement, wire_safe

# The card fields a person reads; every action's `label` is the third.
HUMAN_FIELDS = ("message", "title")
# Under this length a device's name is as likely to be an ordinary word.
MIN_MACHINE_WORD = 3

# A link or a machine name is usually introduced ("for session <name>"); taking the words
# that introduce it with it leaves a sentence behind rather than a dangling preposition.
_INTRODUCES = (r"(?:\b(?:for|from|of|on|in|at|session|host|machine|device|pane|window|seat|tmux)"
               r"\s+){0,2}")
_URL = re.compile(_INTRODUCES + r"<?\b(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>]+>?", re.I)
# 12+ hex digits with a digit among them, alone or behind a producer's prefix (`auth:…`).
_DIGEST = re.compile(r"(?<![\w-])(?:[a-z][a-z0-9_-]*:)?(?=[0-9a-f]*\d)[0-9a-f]{12,}(?![\w-])", re.I)
_SEPARATORS = "·—–"
_NUMBERED = re.compile(r"^\s*(\d{1,3})[.)]\s+")


def machine_words(req: HumanRequirement) -> List[str]:
    """This card's own machine names, longest first so a shorter one cannot eat a prefix."""
    return _words(req.wire_device() or {}, req.subject)


def _words(device: Dict[str, Any], subject: Any) -> List[str]:
    words = [str(device.get(k) or "") for k in ("id", "name", "host")]
    words.append(str(subject.get("session") or "") if isinstance(subject, dict) else "")
    return sorted({w for w in words if len(w) >= MIN_MACHINE_WORD}, key=len, reverse=True)


def _machines(words: List[str]) -> Optional["re.Pattern"]:
    """One pattern per card: `wire_device()` asks the host for its label, so it is
    resolved once and reused, never per string."""
    if not words:
        return None
    alternation = "|".join(re.escape(w) for w in words)
    return re.compile(_INTRODUCES + r"(?<![\w-])(?:" + alternation + r")(?![\w-])", re.I)


def _tidy(text: str) -> str:
    out = re.sub(r"[(\[{]\s*[)\]}]", " ", text)
    out = re.sub(r"[^\S\n]+", " ", out)
    out = re.sub(r"([(\[{]) | ([)\]}])", r"\1\2", out)
    out = re.sub(r" ([,;:.!?])", r"\1", out)
    out = re.sub(r"([" + _SEPARATORS + r"])(?:\s*[" + _SEPARATORS + r"])+", r"\1", out)
    out = re.sub(r"(?m)^[ \t]*[" + _SEPARATORS + r"]+[ \t]*", "", out)
    out = re.sub(r"(?m)[ \t]*[" + _SEPARATORS + r"]+[ \t]*$", "", out)
    return out.strip()


def redact(text: Any) -> str:
    """`text` with the links and digests taken out of it: neither helps a human decide."""
    return _clean(text, None)


def clean(text: Any, req: HumanRequirement) -> str:
    """`redact`, plus this card's own machine names — the card carries them in `device`."""
    return _clean(text, _machines(machine_words(req)))


def _clean(text: Any, machines: Optional["re.Pattern"]) -> str:
    out = _DIGEST.sub(" ", _URL.sub(" ", str(text or "")))
    return _tidy(machines.sub(" ", out) if machines is not None else out)


def headline(req: HumanRequirement) -> str:
    """The one line that speaks for this card: its title, else its first message line.

    Machine names stay: a follow-up notice stands alone and is per seat, so the
    seat's name is how the owner knows which machine is still waiting.
    """
    first = next((ln for ln in (req.message or "").splitlines() if ln.strip()), "")
    for candidate in (req.title, first):
        line = redact(candidate).rstrip(".")
        if line:
            return line
    return req.kind


def option_number(action: Dict[str, Any], req: HumanRequirement) -> Optional[int]:
    """The number the terminal prints against this action's option, if it prints one."""
    mapped = (req.subject or {}).get("option_for_action")
    index = mapped.get(action.get("id")) if isinstance(mapped, dict) else None
    if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
        return index + 1
    # A TUI option's id is the key the human would press: on a numbered list, its number.
    aid = str(action.get("id") or "")
    return int(aid) if aid.isdigit() and 0 < int(aid) <= 99 else None


def canonical_actions(actions: List[Dict[str, Any]], req: HumanRequirement) -> List[Dict[str, Any]]:
    """The actions in the terminal's order, each labelled with the terminal's number.

    An action the terminal does not number keeps its place after the numbered ones.
    """
    return _canonical(actions, req, _machines(machine_words(req)))


def _canonical(actions: List[Dict[str, Any]], req: HumanRequirement,
               machines: Optional["re.Pattern"]) -> List[Dict[str, Any]]:
    numbered: List[Tuple[int, int, Dict[str, Any]]] = []
    plain: List[Dict[str, Any]] = []
    for position, action in enumerate(actions):
        raw = str(action.get("label") or "")
        carried = _NUMBERED.match(raw)
        number = option_number(action, req)
        if number is None and carried:
            number = int(carried.group(1))
        label = _clean(raw[carried.end():] if carried else raw, machines)
        if number is None:
            plain.append(dict(action, label=label))
            continue
        numbered.append((number, position, dict(action, label=("%d. %s" % (number, label)).strip())))
    numbered.sort(key=lambda item: (item[0], item[1]))
    return [action for _, _, action in numbered] + plain


def card(req: HumanRequirement) -> Dict[str, Any]:
    """`req.to_wire()` with only its human-facing strings rewritten — same keys, same
    guard, same device: what a person reads changes, the protocol does not."""
    wire = req.to_wire()
    machines = _machines(_words(wire.get("device") or {}, req.subject))
    for key in HUMAN_FIELDS:
        if key in wire:
            wire[key] = _clean(wire[key], machines)
    if "actions" in wire:
        wire["actions"] = _canonical(wire["actions"], req, machines)
    return wire_safe(wire)
