"""Drafts an agent writes and a client renders as their destination will — rules.

A fourth surface — `?kind=composer` — holding a FEED of posts that grows. Each
post's prose is its own text root, `post:<id>`; a `posts` map carries one row
per post with what the prose is for (`type`, `schema`, `created`) and the
fields prose cannot hold (`subject`, `to`, `cc`).

Every level of that is chosen against last-writer-wins, which is the failure a
collaborative surface exists to prevent:

  - Prose in a text root, not a map value: otherwise a person typing while an
    agent revises loses a paragraph. It also means the caret, the authorship
    ledger and comment anchors work per post with no new code — they address a
    text by name, and the awareness cursor already names its root (`tname`).
  - One map KEY per post, not one value holding every post: a whole-value write
    is last-writer-wins, so two concurrent adds would drop one post entirely.
  - Order derived from `created`, not stored in a list: an `order` array as a
    map value loses a write the same way. Manual reordering would need a Yjs
    array, which merges per element — worth adding when someone asks to drag a
    post, not before.

A post's own fields stay map values deliberately: a subject line has one author
at a time, so last-writer-wins is correct there rather than a compromise.

Imports nothing: the rules are pure, so they are testable without pycrdt.
"""
from __future__ import annotations

import re
from typing import Any

COMPOSER_KIND = "composer"
POSTS_KEY = "posts"
SCHEMA = 1

# A post id becomes a text root name and travels as `tname` in an awareness
# cursor, so it stays short and boring: no separators, no case games.
POST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def root_for(post_id: str) -> str:
    """The text root holding one post's prose."""
    if not POST_ID_RE.fullmatch(str(post_id or "")):
        raise ComposerError(
            f"{post_id!r} is not a usable post id: lowercase letters, digits and dashes, "
            "up to 32 — it becomes the name of a text root and travels in a caret.")
    return f"post:{post_id}"

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

# Where a post is in its life. Nothing here sends, so without this a post
# already published is indistinguishable from one still waiting.
DRAFT, SENT, DROPPED = "draft", "sent", "dropped"
STATUSES = (DRAFT, SENT, DROPPED)
ARCHIVED = (SENT, DROPPED)

# What each destination will not publish past. A draft may exceed it while it
# is being worked on; the renderer shows the overflow, the writer refuses none.
LIMITS: dict[str, int] = {X_POST: 280, LINKEDIN_POST: 3000}

# X's published weights: 1 for these ranges, 2 for everything else — so CJK and
# emoji cost double, and a Chinese post reaches 280 at about 140 characters.
LIGHT_RANGES = ((0x0000, 0x10FF), (0x2000, 0x200D), (0x2010, 0x201F), (0x2032, 0x2037))
WEIGHTED = (X_POST,)


class ComposerError(ValueError):
    """A draft a renderer could not draw, refused where it was written."""


def mark(status: str, at: int | None = None) -> dict:
    """The fields that move a post out of the feed, or back into it."""
    if status not in STATUSES:
        raise ComposerError(f"unknown status {status!r}: one of {', '.join(STATUSES)}")
    out: dict[str, Any] = {"status": status}
    if at is not None:
        out["status_at"] = int(at)
    return out


def status_of(row: Any) -> str:
    """A post's status, defaulting to `draft`.

    An absent or unrecognised status reads as a draft, never as archived: the
    safe direction is "still needs attention". A post is only out of the feed
    because someone said so.
    """
    value = row.get("status") if isinstance(row, dict) else None
    return value if value in STATUSES else DRAFT


def build(artifact_type: str, fields: dict[str, Any] | None = None,
          created: int | None = None) -> dict:
    """One post's row for the `posts` map.

    The prose is NOT in here — it is that post's text root. This is only what
    the text cannot say about itself. `created` orders the feed; it is whole
    UTC minutes, the same unit the write-time ledger uses.
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
    if created is not None:
        out["created"] = int(created)
    for key, value in given.items():
        out[key] = _as_list(value) if key in LIST_FIELDS else value
    return out


def feed(stored: Any) -> list[dict]:
    """Every post in the surface, oldest first, each as `readable()` describes it.

    Ordered by `(created, id)`. The id is the tiebreak, and it is load-bearing:
    an agent filing a batch gives several posts the same minute, and `created`
    alone would leave their order to each client's map iteration — so two people
    would see the same feed differently, intermittently. A missing or malformed
    `created` counts as 0, which puts it at the top and keeps it there rather
    than moving between renders.

    NOTHING here is dropped. A row this reader cannot check — an unknown type,
    an unknown schema, an id shape it does not recognise — comes back `plain`
    with a reason, the same as any other skew. Dropping one would lose a post
    while reporting a healthy shorter feed, which is worse than showing a row
    that says it could not be read.
    """
    rows = stored.items() if hasattr(stored, "items") else []
    out = []
    for post_id, row in rows:
        entry = readable(row)
        entry["id"] = str(post_id)
        entry["root"] = f"post:{post_id}"
        if not POST_ID_RE.fullmatch(entry["id"]):
            entry["plain"] = True
            entry["why"] = "; ".join(filter(None, (
                f"post id {entry['id']!r} is not one this reader knows how to check",
                entry.get("why"))))
        created = entry["fields"].pop("created", None)
        ok = isinstance(created, (int, float)) and not isinstance(created, bool)
        entry["created"] = int(created) if ok and created == int(created) else None
        said = row.get("status") if isinstance(row, dict) else None
        entry["status"] = status_of(row if isinstance(row, dict) else {})
        entry["archived"] = entry["status"] in ARCHIVED
        if said is not None and said != entry["status"]:
            # Normalising to `draft` must not also erase what it said: a newer
            # writer's own word is the only clue an operator has.
            entry["status_said"] = said
        at = entry["fields"].pop("status_at", None)
        at_ok = isinstance(at, (int, float)) and not isinstance(at, bool)
        entry["status_at"] = int(at) if at_ok and at == int(at) else None
        entry["fields"].pop("status", None)
        out.append(entry)
    return sorted(out, key=lambda e: (e["created"] or 0, e["id"]))


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
    # What THIS reader could not check, never what a client cannot draw: the
    # renderers are the client's, and only it knows which ones it has.
    if kind not in TYPES:
        return f"type {kind!r} is not one this reader knows; showing the fields as text"
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
