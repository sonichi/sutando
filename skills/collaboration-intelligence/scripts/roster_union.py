#!/usr/bin/env python3
"""The per-host roster merge, owned once.

Two readers consult reviewer-stands.json — lookup.py and notify_reviewers.py.
A store whose readers disagree about what a collision MEANS is worse than one
with no union at all, so the policy lives here and neither reader restates it.
"""
from __future__ import annotations

import json
from pathlib import Path

ROSTER_LEAF = Path("data") / "collaboration-intelligence" / "reviewer-stands.json"

# Both spellings are deployed. A row carrying only one must read identically to
# every consumer, so the choice is made here rather than in each reader.
IDENTITY_FIELDS = ("gh", "github")


def roster_login(row) -> "tuple[str, str]":
    """(GitHub login this row declares, the field it came from); ("", "") if none.

    Measured on a live roster: 5 rows spell it `gh`, 2 spell it `github`, in one
    file. A reader that knows one spelling reads the other rows as having no
    login at all — an absence indistinguishable from a row nobody filled in.
    """
    if not isinstance(row, dict):
        return "", ""
    for field in IDENTITY_FIELDS:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip(), field
    return "", ""


def host_rosters(workspace) -> "list[tuple[str, Path]]":
    """Every peer host's roster under `workspace`, then the shared legacy file.

    Sorted so the union is deterministic across filesystems; the caller puts its
    own host first, since only the caller knows which host it is.
    """
    ws = Path(workspace)
    out = [(p.parents[2].name, p)
           for p in sorted(ws.glob(f"hosts/*/{ROSTER_LEAF}"))]
    legacy = ws / ROSTER_LEAF
    if legacy.is_file():
        # A real label: an empty one made the collision branch below write the
        # BARE key, overwriting local instead of keeping the row under a suffix.
        out.append(("legacy", legacy))
    return out


def roster_union(paths) -> dict:
    """(host, path) pairs, NEAREST FIRST -> merged rows.

    LOCAL WINS a key collision; the differing peer row is KEPT under
    `<key>@<host>` rather than dropped, because a lost row and a row nobody
    wrote are indistinguishable afterwards. An identical peer row is not
    suffixed — agreement is not a conflict. `_`-prefixed schema notes are
    overwritten rather than suffixed, so they are not duplicated per host.
    """
    merged: dict = {}
    for host, p in paths:
        data = json.loads(Path(p).read_text())
        if not isinstance(data, dict):
            raise SystemExit(f"roster at {p} is not an object")
        for key, row in data.items():
            if key.startswith("_") or key not in merged:
                merged[key] = row
            elif merged[key] != row:
                merged[f"{key}@{host or 'legacy'}"] = row
    return merged
