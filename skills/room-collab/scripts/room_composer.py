"""A draft an agent writes and a client renders as its destination will — rules.

A fourth surface — `?kind=composer` — holding one draft: the PROSE is the
surface's own text, and a `composer` map beside it carries what the prose is
for (`type`, `schema`) and the fields prose cannot hold (`subject`, `to`, `cc`).

Prose in the text, not in the map, is the whole point. A map value is
last-writer-wins: a person typing while an agent revises would lose a
paragraph, silently, which is the defect the collaborative surface exists to
prevent. Keeping the body in the surface's text also means the caret, the
authorship ledger, comment anchors and presence work here with no new code —
they all address the text by name.

The other fields stay map values deliberately: a subject line or a recipient
list has one author at a time, so last-writer-wins is the correct semantics
for them rather than a compromise.

Imports nothing: the rules are pure, so they are testable without pycrdt.
"""
from __future__ import annotations

from typing import Any

COMPOSER_KIND = "composer"
COMPOSER_KEY = "composer"
SCHEMA = 1

X_POST = "x_post"
LINKEDIN_POST = "linkedin_post"
EMAIL = "email"
# Closed on purpose: a type lands here when a renderer for it ships. An agent
# cannot invent one, because the client would have nothing to draw it with.
TYPES = (X_POST, LINKEDIN_POST, EMAIL)

# What each type needs beyond the prose. The prose itself is the text, so it is
# never named here; `email` is the only type with fields of its own.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    X_POST: (),
    LINKEDIN_POST: (),
    EMAIL: ("subject", "to"),
}
# Fields a type may carry at all. Anything else is refused at the writer rather
# than stored where a renderer would have to decide what to do with it.
ALLOWED_FIELDS: dict[str, tuple[str, ...]] = {
    X_POST: (),
    LINKEDIN_POST: (),
    EMAIL: ("subject", "to", "cc"),
}
# Addresses are plain strings — `Name <a@b.c>` if a display name is wanted,
# which is what a mail client shows and what a person pastes.
LIST_FIELDS = ("to", "cc")

# What each destination will not publish past. A draft may exceed it while it
# is being worked on; the renderer shows the overflow, the writer refuses none.
LIMITS: dict[str, int] = {X_POST: 280, LINKEDIN_POST: 3000}

# X's published weights: 1 for these ranges, 2 for everything else — so CJK and
# emoji cost double, and a Chinese post reaches 280 at about 140 characters.
LIGHT_RANGES = ((0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037))
WEIGHTED = (X_POST,)


class ComposerError(ValueError):
    """A draft a renderer could not draw, refused where it was written."""


def build(artifact_type: str, fields: dict[str, Any] | None = None) -> dict:
    """The `composer` map for a draft of `artifact_type`.

    The prose is NOT in here — it is the surface's text. This is only what the
    text cannot say about itself.
    """
    if artifact_type not in TYPES:
        raise ComposerError(
            f"unknown draft type {artifact_type!r}. The renderers are ours and the set is "
            f"closed: {', '.join(TYPES)}.")
    given = dict(fields or {})
    allowed = ALLOWED_FIELDS[artifact_type]
    for key in sorted(given):
        if key not in allowed:
            raise ComposerError(
                f"{artifact_type} has no field {key!r}"
                + (f"; it takes {', '.join(allowed)}" if allowed else " — its content is the text"))
    for key in REQUIRED_FIELDS[artifact_type]:
        if key not in given or _empty(given[key]):
            raise ComposerError(
                f"{artifact_type} needs {key!r}. An absent field is the writer's bug: a "
                "renderer must never have to guess whether it is missing or merely empty.")
    out: dict[str, Any] = {"type": artifact_type, "schema": SCHEMA}
    for key, value in given.items():
        out[key] = _as_list(value) if key in LIST_FIELDS else value
    return out


def _empty(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        return not [v for v in value if str(v).strip()]
    return not str(value or "").strip()


def _as_list(value: Any) -> list[str]:
    """One address per entry, whitespace-trimmed, empties dropped."""
    items = value if isinstance(value, (list, tuple)) else [value]
    return [str(v).strip() for v in items if str(v).strip()]


def readable(stored: Any) -> dict:
    """What a reader can say about a stored draft, including one it cannot draw.

    Never raises: a draft written by a newer skill than this reader is the
    normal case, not an error — an agent's skill is pulled per host while a
    client is deployed, so the two ends are never in step. Skew is reported as
    `plain`, which means "show the fields as text", not "fail".
    """
    row = dict(stored) if isinstance(stored, dict) else {}
    kind = row.get("type")
    schema = row.get("schema")
    known = kind in TYPES and schema == SCHEMA
    out = {
        "type": kind if isinstance(kind, str) else None,
        "schema": int(schema) if isinstance(schema, (int, float)) and schema == int(schema) else None,
        "plain": not known,
        "fields": {k: v for k, v in row.items() if k not in ("type", "schema")},
    }
    if not known:
        out["why"] = _why_plain(kind, schema)
    return out


def _why_plain(kind: Any, schema: Any) -> str:
    if kind not in TYPES:
        return f"no renderer for type {kind!r}; showing the fields as text"
    return f"schema {schema!r} is not {SCHEMA}; showing the fields as text"


def counted_length(artifact_type: str, prose: str) -> int:
    """What the destination will say the length is.

    X weighs each character 1 or 2 by its published ranges; everywhere else
    counts UTF-16 units, which is what a browser's `.length` reports (a count
    in Python characters disagrees for anything outside the basic plane).

    One known approximation, in the safe direction: X counts every URL as a
    fixed 23 however long it is, which is not modelled here, so a draft with
    links is reported LONGER than X will call it. Erring the other way is what
    matters and does not happen — a draft never looks shorter than it is.
    """
    text = prose or ""
    if artifact_type in WEIGHTED:
        return sum(1 if _light(ord(ch)) else 2 for ch in text)
    return len(text.encode("utf-16-le")) // 2


def _light(code: int) -> bool:
    return any(low <= code <= high for low, high in LIGHT_RANGES)


def over_limit(artifact_type: str, prose: str) -> int:
    """How far past the destination's limit the prose is, or 0."""
    limit = LIMITS.get(artifact_type)
    if not limit:
        return 0
    return max(0, counted_length(artifact_type, prose) - limit)
