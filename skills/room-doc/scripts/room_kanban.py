"""A room's Kanban board as a Yjs document, for agents.

A third document kind — `?kind=kanban` — holding two maps: `cards` keyed by
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
    refuses every card that has been through the document — including ones this
    client wrote. A non-integral value is still refused: the schema says ms.
    """
    if isinstance(value, bool):  # bool is an int; a True timestamp is not one
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value) and value == value:
        return int(value)
    return None


def is_card(value: Any, key: str | None = None) -> bool:
    """What the cards map may hold. An agent writes this map directly, so one
    malformed record would reach the panel as a card it cannot draw."""
    if not isinstance(value, dict):
        return False
    ident = value.get("id")
    if not isinstance(ident, str) or not ident:
        return False
    if key is not None and ident != key:
        return False
    if not isinstance(value.get("column"), str) or not value["column"]:
        return False
    if _int(value.get("updated")) is None:
        return False
    for field in ("text", "assignee", "by"):
        if field in value and value[field] is not None and not isinstance(value[field], str):
            return False
    if "order" in value and value["order"] is not None \
            and not isinstance(value["order"], (int, float)):
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
    if not isinstance(value.get("title"), str):
        return False
    return _int(value.get("updated")) is not None


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


def in_column(cards: Iterable[tuple[str, Any]], column: str) -> list[dict]:
    """The cards of one column, in the panel's order."""
    rows = [v for k, v in cards if is_card(v, k) and v.get("column") == column]
    return sorted(rows, key=lambda c: (c.get("order") if isinstance(
        c.get("order"), (int, float)) else float("inf"), c.get("id") or ""))
