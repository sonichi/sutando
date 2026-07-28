# Default Policy Pack — design

_Owner commission 2026-07-26 (relayed by air), my Stage-2/events lane. Air builds
the sparrow consumer (`sonichi/sutando#2319`) in parallel; the two sides meet on
the "rooms are default-subscribed" line._

## Goal

A set of **factory-default subscription policies** that:
- are **designed to** auto-register when an agent first connects (no manual
  config by the user) — via a one-line connect/join hook that calls
  `seed_defaults()`. That hook is the **follow-up** described in "Wiring" below;
  this PR ships the pack + seed functions (CLI-invokable, tested), **not** the
  connect-time caller,
- are **owner-visible** and **individually disable-able**,
- follow the `/observe` policy structure (`observe_policy.py`).

First entry: **👀 react baseline** — observe `message.created` in every member room.

## Why it reuses `observe_policy` instead of a new path

Each pack entry is *authored* to fall inside `observe_policy`'s standing-approval
**locked scope**: `created_by == owner`, room in the owner-scoped set,
`mode ∈ {observe, record, notify}`, `cost_cap.evals_per_day ≤ DEFAULT`. So seeding
runs each fanned-out draft through `validate_draft` (schema/path discipline) **and**
`evaluate_standing_approval` (the boundary), and only activates on `auto=True`. A
pack entry that fails the boundary is **refused, never silently activated** — the
pack is a set of *pre-blessed* standing-approval policies, not a back door around
the approval logic. (Tests assert the fail-closed variants: non-owner, out-of-scope
room, empty owner mxid.)

## Fan-out (the seam with air's consumer)

`observe_policy.room_id` must be a concrete `!room` id, and the sparrow consumer
reacts to the **per-envelope `room_id`** on the SSE stream — it never enumerates
subscriptions (air confirmed 2026-07-26). So a cross-room entry (`scope:
all_member_rooms`) is **fanned out to one concrete per-room policy record per member
room**:
- **connect-time** — `seed_defaults(store_dir, owner_mxid, member_rooms)` seeds each
  enabled entry across all current member rooms;
- **join-time** — `on_room_join(store_dir, owner_mxid, room_id, member_rooms)` seeds
  the enabled entries into a newly-joined room.

Per-room records preserve the `room_id` invariant; **room-level authz stays
server-side** (the events plane's four-way authz at subscribe time — this module
never re-implements it). The consumer needs **zero changes**: it keeps seeing
concrete per-room subscriptions.

## Disable / re-enable

`cancelled` is terminal in `observe_policy`'s store, so disabling can't be undone by
re-activating the same record. Instead each entry carries a **generation** counter in
`<store>/_pack_state.json`:
- **disable** → cancel the entry's live per-room records (`active → cancelled`) and
  bump its generation;
- **enable** → clear the flag; the next `seed_defaults` seeds a **fresh generation**.

Deterministic per-room id `obs_<sha1(entry|generation|room)[:16]>` keeps connect-time
re-seeding **idempotent within a generation** (skip if the current-gen record is
already active — no duplicates on reconnect).

## Owner controls (CLI)

```
python3 skills/observe/default_policy_pack.py list
python3 skills/observe/default_policy_pack.py disable react_baseline
python3 skills/observe/default_policy_pack.py enable  react_baseline
```
`list` shows each entry, on/off, and its live per-room subscription count.

## Wiring (follow-up, one-line hooks — same shape as air's consumer)

The connect/join hooks live in the events/room-ops layer. This PR ships the module +
tests + design; the hooks are a thin call each (mirrors air's "standalone module +
one-line bridge hook" for `default_observer.py`):
- on agent connect → `seed_defaults(store_dir, owner_mxid, member_rooms)`
- on room join → `on_room_join(store_dir, owner_mxid, room_id, member_rooms)`

## Files

- `skills/observe/default_policy_pack.py` — the pack, seeding, owner controls.
- `tests/default-policy-pack.test.py` — lifecycle + fail-closed boundary tests.
