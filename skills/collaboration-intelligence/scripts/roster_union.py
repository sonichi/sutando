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


def _usable(row) -> bool:
    """Addressable, OR a deliberate refusal. A blank `stand` carrying
    `refusal_basis`/`note` is DO-NOT-ROUTE and must not lose to a peer row."""
    if not isinstance(row, dict):
        return False
    if any(str(row.get(k) or "").strip() for k in ("refusal_basis", "note")):
        return True
    # Only a route both consumers can actually deliver on counts. A discord id
    # is not one here: resolve() builds Matrix targets from stand+room alone.
    return bool(row.get("stand") and row.get("room"))


_ROUTING = ("stand", "room")


def _is_routing_placeholder(row) -> bool:
    """No routing value of its own -- nothing of that kind is lost by promoting.

    Applied WITH `not _usable(row)`, never instead of it: `_usable` is also true
    for a refusal row, which carries no routing value and must still never be
    overwritten. This adds the partial-identity case -- a row naming a stand but
    no room states an identity, and promoting over it routes under the wrong one.
    """
    if not isinstance(row, dict):
        return True
    return not any(str(row.get(k) or "").strip() for k in _ROUTING)


def _promote(winner: dict, local: dict) -> dict:
    """Peer routing, local everything-else. `allowlisted: false` is a refusal and
    survives; consumers check it AFTER the bare-key lookup, so an @local copy
    does not protect delivery."""
    out = dict(winner)
    loc = local if isinstance(local, dict) else {}
    # Identity is preserved semantically: roster_login ranks `gh` over `github`,
    # so a surviving peer alias outranks the local spelling.
    if roster_login(loc)[0] or str(loc.get("same_actor_as") or "").strip():
        for alias in IDENTITY_FIELDS + ("same_actor_as",):
            out.pop(alias, None)
    for field, value in loc.items():
        if field not in _ROUTING and value is not None:
            out[field] = value
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
                # Precedence is by origin EXCEPT when exactly one row is usable:
                # `stand: null` is a row, so it won a collision like a filled one.
                if (_usable(row) and not _usable(merged[key])
                        and _is_routing_placeholder(merged[key])):
                    merged[f"{key}@local"] = merged[key]
                    merged[key] = _promote(row, merged[key])
                else:
                    merged[f"{key}@{host or 'legacy'}"] = row
    return merged
