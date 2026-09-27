"""A room's Kanban board as a Yjs document, for agents.

A third surface — `?kind=kanban` — holding two maps: `cards` keyed by
card id, and `columns` keyed by column id. The schema and the convergence rule
are the web panel's, not invented here, so an agent and a person moving the
same card agree about which move won.

The rule: the later `updated` wins; on an exact tie the higher `by` wins, the
same way on every client so there is no coordinator. `updated` is INTEGER
milliseconds since the epoch — a float would compare fine here and then fail
Matrix's canonical JSON if the value ever travelled as an event.

Imports nothing: the rules are pure, so they are testable without pycrdt.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

KANBAN_KIND = "kanban"
CARDS_KEY = "cards"
COLUMNS_KEY = "columns"


def _int(value: Any) -> int | None:
    """Integer milliseconds, however the CRDT gave them back.

    pycrdt returns a stored `300` as `300.0`, so rejecting floats outright
    refuses every card that has been through the board — including ones this
    client wrote. A non-integral value is still refused: the schema says ms.
    """
    if isinstance(value, bool):  # bool is an int; a True timestamp is not one
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, float):
        return None
    # NaN and the infinities FIRST: int() raises on all three, so any check
    # ordered after an int() call is unreachable for the values it guards.
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return int(value) if value == int(value) else None


def is_card(value: Any, key: str | None = None) -> bool:
    """Exactly what the panel accepts — it fails closed, so a card missing
    `text`, `assignee` or `by` is dropped by every viewer with no error."""
    if not isinstance(value, dict):
        return False
    ident = value.get("id")
    if not isinstance(ident, str) or not ident:
        return False
    if key is not None and ident != key:
        return False
    if not isinstance(value.get("column"), str) or not value["column"]:
        return False
    if _int(value.get("order")) is None:
        return False
    if not isinstance(value.get("text"), str) or len(value["text"]) > 4000:
        return False
    if not isinstance(value.get("assignee"), str) or len(value["assignee"]) > 255:
        return False
    updated = _int(value.get("updated"))
    if updated is None or updated < 0:
        return False
    if not isinstance(value.get("by"), str):
        return False
    # A tombstone is still a card. Spelling differs from the board's `isDeleted`.
    if "deleted" in value and value["deleted"] is not None \
            and not isinstance(value["deleted"], bool):
        return False
    return True


def is_column(value: Any, key: str | None = None) -> bool:
    if not isinstance(value, dict):
        return False
    ident = value.get("id")
    if not isinstance(ident, str) or not ident:
        return False
    if key is not None and ident != key:
        return False
    if not isinstance(value.get("title"), str) or len(value["title"]) > 200:
        return False
    if _int(value.get("order")) is None:
        return False
    updated = _int(value.get("updated"))
    if updated is None or updated < 0:
        return False
    return isinstance(value.get("by"), str)


def is_newer(incoming: dict, stored: dict | None) -> bool:
    """Whether `incoming` replaces `stored`. The panel's rule, ported."""
    if stored is None:
        return True
    mine, theirs = _int(incoming.get("updated")) or 0, _int(stored.get("updated")) or 0
    if mine != theirs:
        return mine > theirs
    # An exact tie is broken on the author id, identically on every client, so
    # two writers in the same millisecond still agree.
    return str(incoming.get("by") or "") > str(stored.get("by") or "")


def changed(records: Iterable[dict], stored: Callable[[str], Any],
            valid: Callable[[Any, str | None], bool] = is_card) -> list[dict]:
    """Which records are worth writing. A stored value that is not valid counts
    as absent, so a good copy repairs a poisoned key."""
    out = []
    for record in records:
        if not valid(record, None):
            continue
        current = stored(record["id"])
        if not valid(current, record["id"]):
            current = None
        if is_newer(record, current):
            out.append(record)
    return out


def describe_invalid(value: Any, key: str | None = None) -> str:
    """Why a card was refused, so a silent drop never happens."""
    if not isinstance(value, dict):
        return f"not an object: {type(value).__name__}"
    if not isinstance(value.get("id"), str) or not value.get("id"):
        return "missing a non-empty string 'id'"
    if key is not None and value["id"] != key:
        return f"id {value['id']!r} does not match its key {key!r}"
    if not isinstance(value.get("column"), str) or not value.get("column"):
        return "missing a non-empty string 'column' — a card must live somewhere"
    if _int(value.get("updated")) is None:
        return ("'updated' must be INTEGER milliseconds since the epoch, got "
                f"{value.get('updated')!r}")
    return "valid"


def live_cards(cards: Iterable[tuple[str, Any]]) -> list[dict]:
    """Cards still on the board. A tombstone is a card the panel does not draw,
    so an agent listing work must not offer one back."""
    return [v for k, v in cards if is_card(v, k) and not v.get("deleted")]


def delete_card(card: dict, now: int, by: str) -> dict:
    """A deletion is a newer WRITE, not a removal: removing the key loses to a
    concurrent write, and the card comes back."""
    return {**card, "deleted": True, "updated": int(now), "by": by}


def orphaned_cards(cards: Iterable[tuple[str, Any]],
                   columns: Iterable[tuple[str, Any]]) -> list[dict]:
    """Live cards naming a column this board does not have.

    Deleting a column does not delete its cards, and a card filtered on an
    unknown column is in the board and visible nowhere — so an agent listing
    work would report it as done. The panel shows them under "No column".
    """
    known = {k for k, v in columns if is_column(v, k)}
    return [c for c in live_cards(cards) if c.get("column") not in known]


def in_column(cards: Iterable[tuple[str, Any]], column: str) -> list[dict]:
    """The cards of one column, in the panel's order. Tombstones excluded."""
    rows = [v for k, v in cards
            if is_card(v, k) and v.get("column") == column and not v.get("deleted")]
    return sorted(rows, key=lambda c: (c.get("order") if isinstance(
        c.get("order"), (int, float)) else float("inf"), c.get("id") or ""))


# The panel's defaults for a fresh board, and its spacing between positions.
DEFAULT_COLUMNS = (("todo", "To do"), ("doing", "Doing"), ("done", "Done"))
ORDER_GAP = 1024


def order_between(before: int | None, after: int | None) -> int:
    """A position between two neighbours, the panel's rule: gaps of ORDER_GAP
    so an insert needs no renumbering, a midpoint when squeezed between."""
    if before is None and after is None:
        return 0
    if before is None:
        return int(after) - ORDER_GAP
    if after is None:
        return int(before) + ORDER_GAP
    return (int(before) + int(after)) // 2


def order_after_last(cards: Iterable[tuple[str, Any]], column: str) -> int:
    rows = in_column(cards, column)
    return order_between(int(rows[-1]["order"]) if rows else None, None)


def new_card(ident: str, column: str, text: str, now: int, by: str,
             order: int, assignee: str = "") -> dict:
    return {"id": ident, "column": column, "order": int(order), "text": text,
            "assignee": assignee, "updated": int(now), "by": by}


def move_card(card: dict, column: str, order: int, now: int, by: str) -> dict:
    """A move is a newer version, so it wins or loses by the same rule as an edit."""
    return {**card, "column": column, "order": int(order), "updated": int(now), "by": by}


def assign_card(card: dict, assignee: str, now: int, by: str) -> dict:
    return {**card, "assignee": assignee, "updated": int(now), "by": by}


def default_columns(now: int, by: str) -> list[dict]:
    return [{"id": cid, "title": title, "order": i * ORDER_GAP, "updated": int(now), "by": by}
            for i, (cid, title) in enumerate(DEFAULT_COLUMNS)]


def normalized(card: dict) -> dict:
    """A card read back from the CRDT carries its integers as floats (pycrdt
    returns 300 as 300.0); a write carries them back as integers."""
    out = dict(card)
    for key in ("order", "updated"):
        if key in out and isinstance(out[key], float) and out[key] == int(out[key]):
            out[key] = int(out[key])
    return out
