"""The board document's rules, ported from the web client's boardDoc.ts.

A room's whiteboard is a SECOND document kind — its own Yjs doc reached with
`?kind=board` — holding a map of elements keyed by id, not a text. The
convergence rule (`is_newer`) and the validity rule (`is_board_element`) must
match the web client exactly, or an agent and a person editing one board
disagree about which version of a shape won.

Imports nothing: the rules are pure, so they are testable without pycrdt.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

BOARD_KIND = "board"
ELEMENTS_KEY = "elements"
FILES_KEY = "files"

# Excalidraw's union also has embeddable/iframe/magicframe. Those render
# third-party content, so a planted one would load an attacker's origin.
ELEMENT_TYPES = frozenset({
    "selection", "rectangle", "diamond", "ellipse", "text",
    "line", "arrow", "freedraw", "image", "frame",
})

IMAGE_MIME_RE = re.compile(r"^image/(?:png|jpeg|gif|webp|svg\+xml)$")
DATA_URL_RE = re.compile(r"^data:(image/(?:png|jpeg|gif|webp|svg\+xml));base64,[A-Za-z0-9+/=]+$")


def _finite(value: Any) -> bool:
    # bool is an int in Python; a True width is not a geometry.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and value not in (float("inf"), float("-inf"))


def is_board_element(value: Any, key: str | None = None) -> bool:
    """Whether the map may hold this. A raw Yjs writer — an agent — can put
    anything, and one half-built record crashes every viewer on load."""
    if not isinstance(value, dict):
        return False
    ident = value.get("id")
    if not isinstance(ident, str) or not ident:
        return False
    if key is not None and ident != key:
        return False
    if value.get("type") not in ELEMENT_TYPES:
        return False
    for field in ("x", "y", "width", "height", "version"):
        if not _finite(value.get(field)):
            return False
    for field in ("angle", "versionNonce"):
        if field in value and value[field] is not None and not _finite(value[field]):
            return False
    if "isDeleted" in value and value["isDeleted"] is not None \
            and not isinstance(value["isDeleted"], bool):
        return False
    if "index" in value and value["index"] is not None and not isinstance(value["index"], str):
        return False
    return True


def is_board_file(value: Any, key: str | None = None) -> bool:
    """Same rule for a pasted image, plus what its bytes may be: the data URL
    reaches the editor as an image source, so a remote URL would make every
    viewer fetch it."""
    if not isinstance(value, dict):
        return False
    ident = value.get("id")
    if not isinstance(ident, str) or not ident:
        return False
    if key is not None and ident != key:
        return False
    mime = value.get("mimeType")
    if not isinstance(mime, str) or not IMAGE_MIME_RE.fullmatch(mime):
        return False
    data_url = value.get("dataURL")
    if not isinstance(data_url, str):
        return False
    match = DATA_URL_RE.fullmatch(data_url)
    if not match or match.group(1) != mime:
        return False
    return _finite(value.get("created"))


def is_newer(incoming: dict, stored: dict | None) -> bool:
    """Whether `incoming` replaces `stored`. The same rule on every client, so
    two writers converge with no coordinator."""
    if stored is None:
        return True
    if incoming.get("version") != stored.get("version"):
        return incoming.get("version", 0) > stored.get("version", 0)
    # Same version from two writers: a deterministic tie-break, not last-write.
    return (incoming.get("versionNonce") or 0) > (stored.get("versionNonce") or 0)


def changed_elements(elements: Iterable[dict],
                     stored: Callable[[str], Any]) -> list[dict]:
    """Which of `elements` are worth writing. A stored value that is not a valid
    element counts as absent, so a good copy repairs a poisoned key."""
    out = []
    for element in elements:
        if not is_board_element(element):
            continue
        current = stored(element["id"])
        if not is_board_element(current, element["id"]):
            current = None
        if is_newer(element, current):
            out.append(element)
    return out


def sort_elements(elements: Iterable[dict]) -> list[dict]:
    """Drawing order by fractional index. Elements without one sort after those
    with one, by id, so the order is stable across clients."""
    def key(element: dict) -> tuple:
        index = element.get("index")
        ident = element.get("id") or ""
        if isinstance(index, str):
            return (0, index, ident)
        return (1, "", ident)

    return sorted(elements, key=key)


def elements_from_map(items: Iterable[tuple[str, Any]]) -> list[dict]:
    """Every valid element, deleted ones included — the editor reconciles on
    `isDeleted` and needs to see them."""
    return sort_elements([v for k, v in items if is_board_element(v, k)])


def live_elements(items: Iterable[tuple[str, Any]]) -> list[dict]:
    return [e for e in elements_from_map(items) if not e.get("isDeleted")]


def describe_invalid(value: Any, key: str | None = None) -> str:
    """Why an element was refused. A silent drop is the failure this whole
    module exists to prevent, so the caller gets a reason it can print."""
    if not isinstance(value, dict):
        return f"not an object: {type(value).__name__}"
    ident = value.get("id")
    if not isinstance(ident, str) or not ident:
        return "missing a non-empty string 'id'"
    if key is not None and ident != key:
        return f"id {ident!r} does not match its key {key!r}"
    if value.get("type") not in ELEMENT_TYPES:
        return (f"type {value.get('type')!r} is not one this board draws "
                f"({', '.join(sorted(ELEMENT_TYPES))})")
    for field in ("x", "y", "width", "height", "version"):
        if not _finite(value.get(field)):
            return f"{field!r} must be a finite number, got {value.get(field)!r}"
    for field in ("angle", "versionNonce"):
        if field in value and value[field] is not None and not _finite(value[field]):
            return f"{field!r} must be a finite number when present"
    if "isDeleted" in value and value["isDeleted"] is not None \
            and not isinstance(value["isDeleted"], bool):
        return "'isDeleted' must be a boolean when present"
    if "index" in value and value["index"] is not None and not isinstance(value["index"], str):
        return "'index' must be a string when present"
    return "valid"
