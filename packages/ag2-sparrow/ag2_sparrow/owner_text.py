"""Text the owner reads, in one place: say the cause in one plain sentence, keep the rest behind it.

Every sender of engine-written owner text (cards, their text fallback, reminders, supervisor and
health notices, usage-limit notices) builds its text here, so one rule holds for all of them:

- `card_text(sentence, details)`: the sentence is the first line; ids, session names, paths and
  raw terminal or provider lines go below a blank line. A card shows its first line and folds
  the rest behind "Details"; a text-only reader gets the first line alone (`fallback_text`,
  for a card whose subject carries `FOLDED`).
- `redact(text)`: key-like fragments (`sk-...`, `Bearer ...`, `Key=...`, every provider signature
  `chat_secret_filter` knows) are replaced wherever they appear, details included.
- `internals(text)`: which internal shapes a text still carries (a hex id, a session name, a
  path, a state code, a key). A sender's first line must carry none; `owner_line` falls back to
  a generic sentence when one does, so a record written before this rule cannot leak either.
- `named()`: a display name with any bracketed id dropped; `agent_local_name.local_name()`
  gives this agent's own, from local records.
- `clock()` / `resets()`: a reset time as a person reads it.
"""
from __future__ import annotations

import calendar
import re
import time
from typing import Iterable, List, Optional

try:
    from .chat_secret_filter import redact_known_patterns   # the vendored package layout
except ImportError:
    from chat_secret_filter import redact_known_patterns     # the repo's src/ on sys.path

REDACTED = "[redacted]"
# Subject key a card sets when everything below its first line is detail, not the ask itself.
FOLDED = "details_folded"
DEFAULT_AGENT_NAME = "Your agent"
GENERIC_LINE = "An agent needs your attention"

_SK = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}")
_BEARER = re.compile(r"\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}", re.I)
_KEY_VALUE = re.compile(r"\b([A-Za-z0-9_-]*(?:key|token|secret|password|passwd))(\s*[=:]\s*)([\"']?)(?!\[)"
                        r"[^\s,;&'\"]{4,}\3", re.I)

_BRACKETED = re.compile(r"\s*\([^()]*\)")

_INTERNAL_SHAPES = (
    ("key", re.compile(r"\bsk-[A-Za-z0-9_-]{6,}|\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.I)),
    ("hex-id", re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b")),
    ("record-id", re.compile(r"\b(?:task|hitl|ask)[-_][0-9a-z]*\d[0-9a-z-]*\b")),
    ("session", re.compile(r"\btmux\b|\bsutando-(?:core|worker)\b[\w-]*", re.I)),
    ("path", re.compile(r"(?:^|[\s(`'\"])(?:~|\.)?/[\w.-]+/[\w./-]+")),
    ("state-code", re.compile(r"\b(?:blocked-human|logged-out|needs-login|api-error|network-error)\b")),
    ("command", re.compile(r"\b(?:python3|launchctl|bash)\s+\S")),
)


def redact_deep(value: object) -> object:
    """`value` with every string in it redacted, through lists and mappings; keys are kept."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_deep(v) for v in value]
    return value


def redact(text: object) -> str:
    """`text` with every key-like fragment replaced."""
    out = redact_known_patterns(_SK.sub(REDACTED, "" if text is None else str(text)))
    out = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    return _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)


def headline(text: object) -> str:
    """The first non-empty line: what a card shows before "Details"."""
    return next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")


def internals(text: object) -> List[str]:
    """The internal shapes `text` carries, by name; empty for text a person can read."""
    value = str(text or "")
    return [name for name, shape in _INTERNAL_SHAPES if shape.search(value)]


def card_text(sentence: str, details: Iterable[object] = ()) -> str:
    """One plain sentence, then (below a blank line) the detail lines, redacted."""
    lines = [redact(d).rstrip() for d in details if str(d or "").strip()]
    head = redact(sentence).strip()
    return head if not lines else head + "\n\n" + "\n".join(lines)


def fallback_text(message: object, folded: bool) -> str:
    """What a text-only client reads of a card: its first line when the sender folded the
    details below it (`FOLDED` in the subject), else the whole text; redacted either way."""
    return owner_line(message) if folded else redact(message)


# What stands in for each internal shape in a line a person reads; "" drops the token.
_STAND_INS = {"record-id": "a task", "hex-id": "", "session": "", "path": "", "state-code": "",
              "command": "a command", "key": REDACTED}
_COMMAND_RUN = re.compile(r"\b(?:python3|launchctl|bash)\s+\S.*$")
_LEFTOVERS = re.compile(r"\(\s*\)|`\s*`|\s+(?=[.,;:!?])|(?<=\s)[·—–-]\s*(?=[·—–-]|$)|^\s*[·—–:-]+\s*")
_MEANING = re.compile(r"[A-Za-z]{3,}")


def without_internals(text: object) -> str:
    """`text` with each internal token replaced by a neutral word or dropped, the rest kept:
    "Commit d698b19d7 failed CI" is "Commit failed CI"."""
    given = redact(text)
    if not internals(given):
        return " ".join(given.split())
    out = _BRACKETED.sub(lambda m: "" if internals(m.group(0)) else m.group(0), given)
    out = _COMMAND_RUN.sub(_STAND_INS["command"], out)
    shapes = dict(_INTERNAL_SHAPES)
    for name in _STAND_INS:
        if name != "command":
            shape = shapes[name]
            out = shape.sub(lambda m, word=_STAND_INS[name]: (m.group(0)[:1] if m.group(0)[:1].isspace() else "")
                            + word, out)
    out = " ".join(out.split())
    for _ in range(3):
        out = " ".join(_LEFTOVERS.sub("", out).split())
    return out[:1].upper() + out[1:]


def owner_line(text: object, fallback: str = GENERIC_LINE) -> str:
    """The one line of `text` a notification or a text-only client shows: its first line with
    each internal token taken out; `fallback` only when too little is left to mean anything."""
    line = without_internals(headline(text))
    return line if len(_MEANING.findall(line)) >= 2 and not internals(line) else fallback


def clock(epoch: float, now: Optional[float] = None) -> str:
    """A local time a person reads: "3:40 PM" today, "Nov 12, 3:40 PM" on another day."""
    at, today = time.localtime(epoch), time.localtime(time.time() if now is None else now)
    hour = f"{at.tm_hour % 12 or 12}:{at.tm_min:02d} {'AM' if at.tm_hour < 12 else 'PM'}"
    if (at.tm_year, at.tm_yday) == (today.tm_year, today.tm_yday):
        return hour
    return f"{calendar.month_abbr[at.tm_mon]} {at.tm_mday}, {hour}"


def resets(reset_at: Optional[float], now: Optional[float] = None) -> str:
    """"It resets at 3:40 PM." when the reset time is known and ahead; '' otherwise."""
    now = time.time() if now is None else now
    if not isinstance(reset_at, (int, float)) or isinstance(reset_at, bool) or reset_at <= now:
        return ""
    return f"It resets at {clock(reset_at, now)}."


def named(name: Optional[str], default: str = DEFAULT_AGENT_NAME) -> str:
    """`name` as a display name: a bracketed id after it is dropped ("Reviewer (7c70...)" is
    "Reviewer"); `default` when nothing readable is left."""
    value = _BRACKETED.sub(lambda m: "" if internals(m.group(0)) else m.group(0), str(name or ""))
    value = " ".join(value.split())
    return value if value and not internals(value) else default
