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


def declared(value) -> str:
    """The text a roster field STATES: stripped, or "" when it states nothing.

    The single spelling of blank-is-absent. Every reader of a row and the union's
    own overlay must answer it identically, or a blank local field reads as data
    to one and as silence to the next — and erases a peer's explicit refusal.
    """
    return value.strip() if isinstance(value, str) else ""


def is_declared(value) -> bool:
    """`declared` widened past text, for callers that overlay whole values.

    Same rule for strings; a non-string states itself, so `allowlisted: false`
    survives while `None` and a blank string remain the absence readers assume.
    """
    return bool(declared(value)) if isinstance(value, str) else value is not None


# The ONE statement of which roster fields carry TEXT rather than a value.
# Identity included: a `False` or a list in one reads as blank to every reader.
TEXT_FIELDS = ("refusal_basis", "note", "authority_caveat",
               "same_actor_as") + IDENTITY_FIELDS


def states_field(field, value) -> bool:
    """Whether a NAMED field states something, by that field's OWN type.

    `is_declared` stays the default because its permissiveness is what keeps
    `allowlisted: false` meaningful. A text field asks `declared` instead: a
    list or a `False` in one prints as no reason at all, so letting it overlay
    would erase a peer's stated refusal with a value no reader can read.
    """
    return bool(declared(value)) if field in TEXT_FIELDS else is_declared(value)


def roster_login(row) -> "tuple[str, str]":
    """(GitHub login this row declares, the field it came from); ("", "") if none.

    Measured on a live roster: 5 rows spell it `gh`, 2 spell it `github`, in one
    file. A reader that knows one spelling reads the other rows as having no
    login at all — an absence indistinguishable from a row nobody filled in.
    """
    if not isinstance(row, dict):
        return "", ""
    for field in IDENTITY_FIELDS:
        login = declared(row.get(field))
        if login:
            return login, field
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


# Every route kind a row can declare, and the field groups it must fill -- any
# one spelling per group satisfies its group. The ONE statement of route shape.
ROUTE_FIELDS = (
    ("matrix", (("stand",), ("room",))),
    ("discord", (("discord_id", "stand_discord_id"), ("home_channel",))),
)


# Discord addresses are numeric on the wire; every other route field is text.
NUMERIC_ROUTE_FIELDS = frozenset(f for g in dict(ROUTE_FIELDS)["discord"]
                                 for f in g)


def _names_route(field, value) -> bool:
    """Whether a routing FIELD names a route, by `declared`'s rule — not a
    second spelling of it.

    Text that states nothing states no route, so a blank, a list and a dict all
    fail here rather than reaching a consumer that assumes a string. `false` and
    `0` are still how a row says "no route"; only an id field may be numeric.
    """
    if declared(value):
        return True
    return (field in NUMERIC_ROUTE_FIELDS and isinstance(value, int)
            and not isinstance(value, bool) and bool(value))


def routing_fields(kinds=None) -> "tuple[str, ...]":
    """Every field the named routes read, deduped, in ROUTE_FIELDS order.

    `kinds=None` means every route. A caller that narrows it is stating which
    transports its question is about, which is the part that used to be an
    unwritten assumption inside each consumer.
    """
    return tuple(dict.fromkeys(field for kind, groups in ROUTE_FIELDS
                               if kinds is None or kind in kinds
                               for group in groups for field in group))


def declared_routes(row, kinds=None) -> "tuple[str, ...]":
    """The route kinds this row can be delivered on, PREFERRED FIRST.

    The union asks whether a row has ANY; the notifier asks WHICH. Reading one
    table is what stops either deciding alone that a declared route does not
    exist -- the union calling a usable Discord row a placeholder let a synced
    peer's Matrix row take over a destination the local row already named.

    `kinds=None` means every route, and a caller that narrows it states which
    transports it can DRIVE -- the same declaration `states_routing` takes. A
    consumer answering that question differently is how the two disagreed.
    """
    if not isinstance(row, dict):
        return ()
    return tuple(kind for kind, groups in ROUTE_FIELDS
                 if (kinds is None or kind in kinds)
                 and all(any(_names_route(field, row.get(field))
                             for field in group)
                         for group in groups))


def states_routing(row, kinds=None) -> bool:
    """Any routing value at all for the named routes, complete or not.

    A partial route still names an identity, so promoting over it routes under
    a different one. Which transports that protection covers is the CALLER's
    declaration, not this module's guess -- see the union's placeholder test.
    """
    if not isinstance(row, dict):
        return False
    return any(_names_route(field, row.get(field))
               for field in routing_fields(kinds))


def _usable(row, kinds=None) -> bool:
    """Addressable ON `kinds`, OR a deliberate refusal. A blank `stand` carrying
    `refusal_basis`/`note` is DO-NOT-ROUTE and must not lose to a peer row."""
    if not isinstance(row, dict):
        return False
    if any(declared(row.get(k)) for k in TEXT_FIELDS):
        return True
    return bool(declared_routes(row, kinds))


def _is_routing_placeholder(row) -> bool:
    """No routing value of its own -- nothing of that kind is lost by promoting.

    Applied WITH `not _usable(row)`, never instead of it: `_usable` is also true
    for a refusal row, which carries no routing value and must still never be
    overwritten. This adds the partial-identity case -- a row naming a stand but
    no room states an identity, and promoting over it routes under the wrong one.

    Scoped to `matrix`: a COMPLETE Discord route is already protected by
    `_usable`, while a lone Discord id is deliberately still promotable, so a
    peer with a working Stand can reach someone this host cannot address.
    """
    return not states_routing(row, ("matrix",))


def _promote(winner: dict, local: dict) -> dict:
    """Peer routing, local everything-else. `allowlisted: false` is a refusal and
    survives; consumers check it AFTER the bare-key lookup, so an @local copy
    does not protect delivery."""
    out = dict(winner)
    loc = local if isinstance(local, dict) else {}
    # Identity is preserved semantically: roster_login ranks `gh` over `github`,
    # so a surviving peer alias outranks the local spelling.
    if roster_login(loc)[0] or declared(loc.get("same_actor_as")):
        for alias in IDENTITY_FIELDS + ("same_actor_as",):
            out.pop(alias, None)
    # Per FIELD, never `is not None`: absence is the field's own type's answer,
    # so overlaying erases neither the refusal nor the identity the peer stated.
    routing = routing_fields()
    for field, value in loc.items():
        if field not in routing and states_field(field, value):
            out[field] = value
    return out


def roster_union(paths, kinds=None) -> dict:
    """(host, path) pairs, NEAREST FIRST -> merged rows.

    LOCAL WINS a key collision; the differing peer row is KEPT under
    `<key>@<host>` rather than dropped, because a lost row and a row nobody
    wrote are indistinguishable afterwards. An identical peer row is not
    suffixed — agreement is not a conflict. `_`-prefixed schema notes are
    overwritten rather than suffixed, so they are not duplicated per host.

    `kinds` is the CALLER's declaration of what it can deliver on (default:
    every route). A caller that cannot drive a transport must say so, or the
    tie-break hands it a winner it will then refuse, and the reachable row for
    that person survives only under a suffix its own resolver never reads.
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
                if (_usable(row, kinds) and not _usable(merged[key], kinds)
                        and _is_routing_placeholder(merged[key])):
                    merged[f"{key}@local"] = merged[key]
                    merged[key] = _promote(row, merged[key])
                else:
                    merged[f"{key}@{host or 'legacy'}"] = row
    return merged
