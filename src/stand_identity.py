#!/usr/bin/env python3
"""Is a `Stand:` trailer mine?

Ownership was being decided by comparing a trailer against one remembered
spelling. The fleet writes several per agent, so a variant reads as a peer and
an agent disowns its own PR. Measured on one branch of mine: 49 commits say
`Echo Act IV Pro` and 9 say `Sutando-Pro` — same author, same work.

Authority is `hosts/<host-label>/stand-identity.json`, never recall.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def _norm(s: str) -> str:
    """Fold case, spacing and punctuation so spelling variants converge."""
    s = _PAREN.sub("", (s or "").strip())
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def my_stand_aliases(workspace: Path, host_label: str) -> set[str]:
    """Every spelling of THIS host's stand, normalised."""
    p = Path(workspace) / "hosts" / host_label / "stand-identity.json"
    d = json.loads(p.read_text())
    name = d["name"]
    out = {_norm(name), _norm(d.get("machine", ""))}
    # "Echo Act IV Pro" also ships as "Sutando-Pro"; the suffix is the node.
    node = name.split()[-1] if name.split() else ""
    if node:
        out |= {_norm(node), _norm(f"Sutando-{node}")}
    return {a for a in out if a}


def is_my_stand(trailer: str, workspace: Path, host_label: str) -> bool:
    """True when `trailer` names THIS host's stand in any known spelling.

    A trailing parenthetical is metadata, not identity: `Sutando-Pro (principal)`
    is the same agent as `Sutando-Pro`.
    """
    return _norm(trailer) in my_stand_aliases(workspace, host_label)
