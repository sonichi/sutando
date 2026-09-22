"""What changed in a text document, and which of it is addressed to me.

An agent holding a document open receives every remote edit as it lands, but
an edit is a delta of characters, not a message. These rules turn two
snapshots into the lines that appeared and the ones that name a handle, so a
watcher can act on "a new line says @mars" without a person pinging it.

Imports nothing: pure text rules, testable without pycrdt.
"""
from __future__ import annotations

import re
from collections import Counter


def new_lines(before: str, after: str) -> list[str]:
    """Lines present in `after` that were not in `before`, in document order.

    Counted, not set-differenced: a line that already appeared once and now
    appears twice was written once more, and the second copy is new.
    """
    left = Counter(line for line in before.splitlines() if line.strip())
    out = []
    for line in after.splitlines():
        if not line.strip():
            continue
        if left[line] > 0:
            left[line] -= 1
        else:
            out.append(line)
    return out


def _handle_pattern(handle: str) -> re.Pattern:
    # An @-mention, as a whole token: "@mars" must not fire on "@marshall" nor
    # on a bare "mars" in prose — a summon always writes the @.
    body = handle.lstrip("@")
    # ".", ":" and "-" continue a handle only when a word character follows
    # ("@mars.b", "@mars:x"); "@mars." at a sentence end is still @mars.
    return re.compile(r"(?<!\w)@" + re.escape(body) + r"(?!\w|[.:-]\w)", re.I)


def addressed_to(lines: list[str], handles: list[str]) -> list[str]:
    """The lines that @-mention any of `handles` (given with or without the @)."""
    patterns = [_handle_pattern(h) for h in handles if h.strip("@").strip()]
    return [line for line in lines if any(p.search(line) for p in patterns)]


# --- the other documents, and presence: what changed, and which of it is mine

def board_mentions(before: list[dict], after: list[dict], handles: list[str]) -> list[dict]:
    """Text elements that newly @-mention a handle: new, or whose text changed
    to include one. A label that already mentioned me and merely moved is not
    news."""
    old = {e.get("id"): e.get("text") or "" for e in before if e.get("type") == "text"}
    out = []
    for e in after:
        if e.get("type") != "text" or e.get("isDeleted"):
            continue
        text = e.get("text") or ""
        if text == old.get(e.get("id")):
            continue
        if addressed_to([text], handles):
            out.append({"kind": "mention", "where": "board", "element": e.get("id"), "text": text})
    return out


def _mine(value: object, handles: list[str]) -> bool:
    if not isinstance(value, str) or not value:
        return False
    v = value.lstrip("@").lower()
    return any(v == h.lstrip("@").lower() for h in handles)


def kanban_changes(before: dict, after: dict, handles: list[str]) -> list[dict]:
    """Cards that became mine, or that are mine and moved column. `before` and
    `after` are the cards map (id -> card). A card deleted (tombstoned) is
    reported as `unassigned` so a watcher can drop it from its own list."""
    out = []
    for cid, card in after.items():
        if not isinstance(card, dict):
            continue
        prev = before.get(cid) if isinstance(before.get(cid), dict) else None
        mine_now = _mine(card.get("assignee"), handles) and not card.get("deleted")
        mine_before = bool(prev) and _mine(prev.get("assignee"), handles) and not prev.get("deleted")
        if mine_now and not mine_before:
            out.append({"kind": "assigned", "where": "kanban", "card": cid,
                        "column": card.get("column"), "text": card.get("text") or ""})
        elif mine_before and not mine_now:
            out.append({"kind": "unassigned", "where": "kanban", "card": cid})
        elif mine_now and prev and card.get("column") != prev.get("column"):
            out.append({"kind": "moved", "where": "kanban", "card": cid,
                        "from": prev.get("column"), "to": card.get("column"),
                        "text": card.get("text") or ""})
    return out


def peer_changes(before: list[dict], after: list[dict]) -> list[dict]:
    """Who arrived and who left, by the identity presence carries (mxid when
    present, else the display name). The same person on two devices is one
    peer for this purpose."""
    def key(p: dict) -> str:
        return str(p.get("id") or p.get("user_id") or p.get("mxid") or p.get("name") or "")
    was = {key(p): p for p in before if key(p)}
    now = {key(p): p for p in after if key(p)}
    out = [{"kind": "peer_joined", "who": k, "name": now[k].get("name")} for k in now if k not in was]
    out += [{"kind": "peer_left", "who": k, "name": was[k].get("name")} for k in was if k not in now]
    return out
