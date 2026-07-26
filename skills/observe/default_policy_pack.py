#!/usr/bin/env python3
"""default_policy_pack — factory-default subscription policies for the events
/observe lane. Auto-registered on agent first-connect (no manual config),
owner-visible and individually disable-able.

Design (owner commission 2026-07-26, my Stage-2/events lane; air builds the
sparrow consumer sonichi/sutando#2319 in parallel — we meet on the "rooms are
default-subscribed" line):

  - Each pack entry is DESIGNED to fall INSIDE observe_policy's standing-approval
    locked scope (owner-created, mode in STANDING_MODES, cost_cap <= default).
    So seeding REUSES validate_draft + evaluate_standing_approval rather than
    bypassing that safety boundary: a pack entry that fails the boundary is
    REFUSED, never silently activated. The pack is a set of *pre-blessed*
    standing-approval policies, not a back door around the approval logic.

  - Fan-out model (seam agreed with air 2026-07-26): observe_policy.room_id must
    be a concrete !room id, and the sparrow consumer reacts per-envelope room_id
    (it never enumerates subscriptions). So a cross-room pack entry is fanned OUT
    to one concrete per-room policy record per member room at connect time, and
    incrementally on each room-join. Per-room records preserve the room_id
    invariant; room-level authz stays server-side (events plane's four-way authz
    at subscribe time — this module never re-implements it).

  - Disable/re-enable: observe_policy's store makes `cancelled` terminal, so a
    disabled entry's per-room records are cancelled and the entry's *generation*
    is bumped; re-enabling seeds a fresh generation. Deterministic per-room ids
    within a generation keep connect-time re-seeding idempotent (skip if the
    current-generation record is already active).

First entry: 👀 react baseline — observe m.reaction in every member room.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

import observe_policy as op

# ── The factory pack ────────────────────────────────────────────────────────
# Each entry is authored to satisfy the standing-approval locked scope. Keep
# `mode` in op.STANDING_MODES and `cost_cap.evals_per_day` <= DEFAULT so the
# reused boundary auto-activates it; anything outside would (correctly) be
# refused rather than silently widened.
PACK_ENTRIES: "list[dict]" = [
    {
        "key": "react_baseline",
        "label": "👀 React baseline",
        "description": "Observe reactions across every room the agent is in.",
        "event_types": ["m.reaction"],
        "mode": "observe",
        "cost_cap": {"evals_per_day": op.DEFAULT_EVALS_PER_DAY},
        "scope": "all_member_rooms",
        "default_enabled": True,
    },
]

_PACK_STATE_FILE = "_pack_state.json"  # not obs_*.json → never listed as a policy


def _entry(key: str) -> "dict | None":
    return next((e for e in PACK_ENTRIES if e["key"] == key), None)


# ── Pack meta-state (enable/disable + generation) ───────────────────────────
def _state_path(store_dir: str) -> str:
    return os.path.join(store_dir, _PACK_STATE_FILE)


def load_pack_state(store_dir: str) -> dict:
    try:
        with open(_state_path(store_dir)) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("entries", {})
    return st


def save_pack_state(store_dir: str, st: dict) -> None:
    os.makedirs(store_dir, exist_ok=True)
    path = _state_path(store_dir)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _entry_state(st: dict, key: str) -> dict:
    """Return the mutable per-entry state (defaults: enabled, generation 1)."""
    e = st["entries"].setdefault(key, {})
    e.setdefault("disabled", False)
    e.setdefault("generation", 1)
    return e


def is_enabled(store_dir: str, key: str) -> bool:
    entry = _entry(key)
    if not entry:
        return False
    st = load_pack_state(store_dir)
    es = st["entries"].get(key)
    if es is None:
        return bool(entry.get("default_enabled", True))
    return not es.get("disabled", False)


# ── Deterministic per-room policy id (idempotent within a generation) ────────
def _policy_id_for(entry_key: str, generation: int, room_id: str) -> str:
    h = hashlib.sha1(f"{entry_key}|{generation}|{room_id}".encode()).hexdigest()
    return f"obs_{h[:16]}"  # matches observe_policy._POLICY_ID_RE (obs_ + 16 hex)


def _draft_for(entry: dict, room_id: str, owner_mxid: str, generation: int) -> dict:
    return {
        "policy_id": _policy_id_for(entry["key"], generation, room_id),
        "room_id": room_id,
        "event_types": list(entry["event_types"]),
        "mode": entry["mode"],
        "cost_cap": dict(entry.get("cost_cap") or {}),
        "created_by": owner_mxid,
        "source_text": f"factory default [{entry['key']}]: {entry['description']}",
    }


# ── Seeding (connect-time + join-time), authz via the reused boundary ────────
def seed_room(store: "op.SubscriptionStore", store_dir: str, entry: dict,
              room_id: str, owner_mxid: str, owner_rooms: "list[str]") -> dict:
    """Seed one per-room policy for `entry` in `room_id`. Idempotent: skips if
    the current-generation record is already active. Reuses observe_policy's
    validate_draft + evaluate_standing_approval — a draft that fails either is
    REFUSED (returned with status='refused'), never activated. Returns a result
    dict {status, policy_id, reason}."""
    st = load_pack_state(store_dir)
    es = _entry_state(st, entry["key"])
    gen = es["generation"]
    pid = _policy_id_for(entry["key"], gen, room_id)

    existing = store.get(pid)
    if existing and existing.get("status") == "active":
        return {"status": "skipped", "policy_id": pid, "reason": "already active"}

    draft = _draft_for(entry, room_id, owner_mxid, gen)
    normalized, errors = op.validate_draft(draft)
    if errors:
        return {"status": "refused", "policy_id": pid,
                "reason": f"schema: {errors}"}
    auto, reason = op.evaluate_standing_approval(
        normalized, owner_mxid=owner_mxid, owner_rooms=owner_rooms)
    if not auto:
        # A factory default is authored to pass the boundary; if it doesn't,
        # that is a misconfigured pack entry or an out-of-scope room — refuse,
        # do NOT silently activate (respects the never-widen-the-boundary rule).
        return {"status": "refused", "policy_id": pid, "reason": reason}

    # Tag with pack provenance AFTER validate_draft (which drops unknown keys);
    # the store persists extra fields and observe_policy ignores them.
    normalized["pack"] = {"entry": entry["key"], "generation": gen}
    store.save(normalized)
    store.transition(pid, "active", note="factory default (standing approval)")
    return {"status": "seeded", "policy_id": pid, "reason": reason}


def seed_defaults(store_dir: str, owner_mxid: str,
                  member_rooms: "list[str]") -> "list[dict]":
    """Connect-time: seed every ENABLED pack entry across all member rooms.
    `member_rooms` is the owner-scoped set for the standing-approval check."""
    store = op.SubscriptionStore(store_dir)
    results = []
    for entry in PACK_ENTRIES:
        if not is_enabled(store_dir, entry["key"]):
            continue
        if entry.get("scope") != "all_member_rooms":
            continue
        for room_id in member_rooms:
            r = seed_room(store, store_dir, entry, room_id, owner_mxid,
                          owner_rooms=member_rooms)
            r["room_id"] = room_id
            r["entry"] = entry["key"]
            results.append(r)
    return results


def on_room_join(store_dir: str, owner_mxid: str, room_id: str,
                 member_rooms: "list[str] | None" = None) -> "list[dict]":
    """Join-time incremental: seed every enabled all-member-rooms entry into the
    newly-joined room. `member_rooms` (if given) is the owner-scoped set passed
    to the standing-approval check; defaults to just the new room."""
    store = op.SubscriptionStore(store_dir)
    owner_rooms = member_rooms if member_rooms is not None else [room_id]
    results = []
    for entry in PACK_ENTRIES:
        if not is_enabled(store_dir, entry["key"]):
            continue
        if entry.get("scope") != "all_member_rooms":
            continue
        r = seed_room(store, store_dir, entry, room_id, owner_mxid,
                      owner_rooms=owner_rooms)
        r["room_id"] = room_id
        r["entry"] = entry["key"]
        results.append(r)
    return results


# ── Owner controls: list + disable/enable ───────────────────────────────────
def _active_records_for(store: "op.SubscriptionStore", key: str) -> "list[dict]":
    return [r for r in store.list(status="active")
            if (r.get("pack") or {}).get("entry") == key]


def list_pack(store_dir: str) -> "list[dict]":
    """Owner-visible pack listing: each entry + enabled + count of live per-room
    subscriptions."""
    store = op.SubscriptionStore(store_dir)
    out = []
    for entry in PACK_ENTRIES:
        active = _active_records_for(store, entry["key"])
        out.append({
            "key": entry["key"],
            "label": entry["label"],
            "description": entry["description"],
            "enabled": is_enabled(store_dir, entry["key"]),
            "active_rooms": sorted(r["room_id"] for r in active),
        })
    return out


def set_enabled(store_dir: str, key: str, enabled: bool) -> dict:
    """Owner disable/enable. Disabling cancels the entry's live per-room
    subscriptions and bumps its generation (so a later enable seeds a fresh set
    — `cancelled` is terminal in the store). Enabling only clears the flag; the
    caller re-seeds via seed_defaults on the next connect (or immediately)."""
    entry = _entry(key)
    if not entry:
        return {"ok": False, "reason": f"unknown pack entry: {key}"}
    store = op.SubscriptionStore(store_dir)
    st = load_pack_state(store_dir)
    es = _entry_state(st, key)
    was_disabled = es["disabled"]
    if not enabled:
        cancelled = 0
        for rec in _active_records_for(store, key):
            if store.transition(rec["policy_id"], "cancelled",
                                 note="pack entry disabled by owner"):
                cancelled += 1
        if not was_disabled:
            es["generation"] += 1  # next enable seeds a fresh generation
        es["disabled"] = True
        save_pack_state(store_dir, st)
        return {"ok": True, "key": key, "enabled": False,
                "cancelled_rooms": cancelled}
    es["disabled"] = False
    save_pack_state(store_dir, st)
    return {"ok": True, "key": key, "enabled": True,
            "note": "re-seed via seed_defaults on next connect"}


# ── CLI (owner ops) ─────────────────────────────────────────────────────────
def _default_store_dir() -> str:
    import sys
    from pathlib import Path
    here = Path(__file__).resolve()
    repo = next((p for p in here.parents if (p / "src" / "workspace_default.py").is_file()), None)
    if repo:
        sys.path.insert(0, str(repo / "src"))
        from workspace_default import resolve_workspace  # noqa: E402
        return str(Path(resolve_workspace()) / "state" / "observe")
    return os.path.join(os.getcwd(), "workspace", "state", "observe")


def main(argv: "list[str] | None" = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Default policy pack — owner controls")
    ap.add_argument("cmd", choices=["list", "enable", "disable", "seed"])
    ap.add_argument("key", nargs="?", help="pack entry key (enable/disable)")
    ap.add_argument("--store", default=None, help="store dir (default <workspace>/state/observe)")
    ap.add_argument("--owner", help="owner mxid (seed)")
    ap.add_argument("--rooms", help="comma-separated !room ids (seed)")
    args = ap.parse_args(argv)
    store_dir = args.store or _default_store_dir()

    if args.cmd == "list":
        for row in list_pack(store_dir):
            mark = "on " if row["enabled"] else "off"
            print(f"[{mark}] {row['key']:16} {row['label']:20} "
                  f"{len(row['active_rooms'])} room(s): {row['description']}")
        return 0
    if args.cmd in ("enable", "disable"):
        if not args.key:
            print("key required"); return 2
        print(json.dumps(set_enabled(store_dir, args.key, args.cmd == "enable")))
        return 0
    if args.cmd == "seed":
        if not (args.owner and args.rooms):
            print("--owner and --rooms required for seed"); return 2
        rooms = [r.strip() for r in args.rooms.split(",") if r.strip()]
        for r in seed_defaults(store_dir, args.owner, rooms):
            print(json.dumps(r))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
