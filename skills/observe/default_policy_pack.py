#!/usr/bin/env python3
"""Factory-default subscription policies for the events /observe lane —
owner-visible, individually disable-able, seeded via seed_defaults/on_room_join."""
from __future__ import annotations

import hashlib
import json
import os
import time

import observe_policy as op

# ── The factory pack ────────────────────────────────────────────────────────

# Each entry must stay inside the standing-approval locked scope (mode in
# STANDING_MODES, cap <= default); anything outside is refused, never widened.
PACK_ENTRIES: "list[dict]" = [
    {
        "key": "react_baseline",
        "label": "👀 React baseline",
        "description": "Observe new messages across every room the agent is in (the 👀 observed-receipt subscription).",
        "event_types": ["message.created"],
        "mode": "observe",
        "cost_cap": {"evals_per_day": op.DEFAULT_EVALS_PER_DAY},
        "scope": "all_member_rooms",
        "default_enabled": True,
    },
]

# Aggregate ceiling on what the pack may auto-activate — a per-draft cap cannot
# bound the fan-out total. A policy choice, not a constant derived from fan-out.
PACK_AGGREGATE_EVALS_PER_DAY = 20

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
def committed_evals_per_day(store: "op.SubscriptionStore") -> int:
    """Total evals/day the pack has auto-activated (pack-provenance ACTIVE only);
    owner-approved records never shrink the automatic allowance."""
    total = 0
    for rec in store.list(status="active"):
        prov = rec.get("pack") or {}
        if not prov.get("entry"):
            continue
        if prov.get("over_budget"):
            # Owner-approved past the cap; counting it would let an explicit
            # approval shrink the automatic allowance.
            continue
        cap = (rec.get("cost_cap") or {}).get("evals_per_day")
        if isinstance(cap, int):
            total += cap
    return total


def _entry_still_live(store_dir: str, key: str, generation: int) -> bool:
    """Re-read pack state from disk: is `key` still enabled AND on `generation`?
    Run immediately before any persist/activate to catch an in-flight disable."""
    st = load_pack_state(store_dir)
    es = st["entries"].get(key)
    if es is None:
        entry = _entry(key)
        return bool(entry and entry.get("default_enabled", True)) and generation == 1
    return (not es.get("disabled", False)) and es.get("generation") == generation


def _budget_allows(store: "op.SubscriptionStore", draft: dict) -> "tuple[bool, str]":
    """Would activating `draft` keep the pack inside its aggregate budget?"""
    cap = (draft.get("cost_cap") or {}).get("evals_per_day")
    cap = cap if isinstance(cap, int) else 0
    committed = committed_evals_per_day(store)
    if committed + cap > PACK_AGGREGATE_EVALS_PER_DAY:
        return False, (
            f"pack aggregate budget reached "
            f"({committed} + {cap} > {PACK_AGGREGATE_EVALS_PER_DAY} evals/day) — "
            "explicit approval required for this room"
        )
    return True, ""


def seed_room(store: "op.SubscriptionStore", store_dir: str, entry: dict,
              room_id: str, owner_mxid: str, owner_rooms: "list[str]") -> dict:
    """Seed one per-room policy for `entry` in `room_id`; idempotent within a
    generation. A draft failing the reused boundary is refused, never activated."""
    st = load_pack_state(store_dir)
    es = _entry_state(st, entry["key"])
    gen = es["generation"]
    pid = _policy_id_for(entry["key"], gen, room_id)

    existing = store.get(pid)
    if existing is not None:
        status = existing.get("status")
        if status == "draft":
            # A non-terminal draft is a crash between save() and transition();
            # resume it, re-running the boundary — never widen it on a resume.
            recheck = _draft_for(entry, room_id, owner_mxid, gen)
            normalized, errors = op.validate_draft(recheck)
            if errors:
                return {"status": "refused", "policy_id": pid,
                        "reason": f"schema: {errors}"}
            auto, reason = op.evaluate_standing_approval(
                normalized, owner_mxid=owner_mxid, owner_rooms=owner_rooms)
            if not auto:
                return {"status": "refused", "policy_id": pid, "reason": reason}
            # The budget applies on resume too; reservation and activation are
            # one critical section, same as the first-seed path.
            with op.store_lock(store_dir):
                # Re-read inside the lock: a direct per-record cancellation can
                # land after the pre-lock read that chose this branch.
                fresh = store.get(pid)
                if fresh is None or fresh.get("status") != "draft":
                    return {"status": "refused", "policy_id": pid,
                            "reason": "the draft was cancelled or removed while this "
                                      "resume was in flight"}
                ok, why = _budget_allows(store, normalized)
                if not ok:
                    return {"status": "refused", "policy_id": pid, "reason": why}
                if not _entry_still_live(store_dir, entry["key"], gen):
                    return {"status": "refused", "policy_id": pid,
                            "reason": "entry was disabled or re-generated while this "
                                      "seed was in flight"}
                normalized["pack"] = {"entry": entry["key"], "generation": gen}
                # save() is a blind overwrite that consults no state machine and
                # can resurrect a swept cancel — so re-verify after publishing.
                store.save(normalized)  # re-assert content (idempotent for this gen)
                if not _entry_still_live(store_dir, entry["key"], gen):
                    store.transition(pid, "cancelled",
                                     note="pack entry disabled while this resume was in flight")
                    return {"status": "refused", "policy_id": pid,
                            "reason": "entry was disabled or re-generated while this "
                                      "seed was in flight"}
                if not store.transition(pid, "active",
                                        note="factory default (resumed crash-interrupted draft)"):
                    return {"status": "refused", "policy_id": pid,
                            "reason": "activation refused by the store — the record was already cancelled"}
                return {"status": "resumed", "policy_id": pid, "reason": reason}
        # active → already seeded; cancelled → a direct cancellation, never
        # resurrected (a re-enable bumps the generation, yielding a fresh pid).
        return {"status": "skipped", "policy_id": pid,
                "reason": f"generation record exists ({status})"}

    draft = _draft_for(entry, room_id, owner_mxid, gen)
    normalized, errors = op.validate_draft(draft)
    if errors:
        return {"status": "refused", "policy_id": pid,
                "reason": f"schema: {errors}"}
    auto, reason = op.evaluate_standing_approval(
        normalized, owner_mxid=owner_mxid, owner_rooms=owner_rooms)
    if not auto:
        # A boundary failure is a misconfigured entry or out-of-scope room —
        # refuse, never silently activate.
        return {"status": "refused", "policy_id": pid, "reason": reason}

    # The per-draft boundary cannot see the aggregate; budget reservation must
    # be atomic with activation — one critical section, not two correct steps.
    with op.store_lock(store_dir):
        # Idempotency is part of the critical section: two writers racing the
        # same room both read None pre-lock; the loser must skip, not downgrade.
        fresh = store.get(pid)
        if fresh is not None:
            return {"status": "skipped", "policy_id": pid,
                    "reason": f"generation record exists ({fresh.get('status')}) "
                              f"— created by a concurrent seed"}
        ok, why = _budget_allows(store, normalized)
        if not ok:
            # Persist the over-budget policy as a DRAFT so an approvable record
            # backs the refusal; drafts consume no budget and resume when it frees.
            if not _entry_still_live(store_dir, entry["key"], gen):
                return {"status": "refused", "policy_id": pid,
                        "reason": "entry was disabled or re-generated while this seed was in flight"}
            # `over_budget` = requires explicit approval; committed_evals_per_day()
            # skips it forever so an approval never shrinks the automatic allowance.
            normalized["pack"] = {"entry": entry["key"], "generation": gen,
                                  "over_budget": True}
            # Publish, then verify — same compare-and-commit as the activate path;
            # a stranded draft on a disabled entry could still legally activate.
            store.save(normalized)
            if not _entry_still_live(store_dir, entry["key"], gen):
                store.transition(pid, "cancelled",
                                 note="pack entry disabled while this over-budget draft was in flight")
                return {"status": "refused", "policy_id": pid,
                        "reason": "entry was disabled or re-generated while this seed was in flight"}
            return {"status": "refused", "policy_id": pid, "reason": why,
                    "draft": normalized, "awaiting": "owner-approval"}

        # Provenance is tagged AFTER validate_draft (which drops unknown keys).
        # Cheap path first: refuse before writing anything at all.
        if not _entry_still_live(store_dir, entry["key"], gen):
            return {"status": "refused", "policy_id": pid,
                "reason": "entry was disabled or re-generated while this seed was in flight"}
        normalized["pack"] = {"entry": entry["key"], "generation": gen}

        # Compare-and-commit: save first so a concurrent disable's sweep can see
        # and cancel this record, then re-check and honour transition()'s result.
        store.save(normalized)
        if not _entry_still_live(store_dir, entry["key"], gen):
            store.transition(pid, "cancelled",
                             note="pack entry disabled while this seed was in flight")
            return {"status": "refused", "policy_id": pid,
                    "reason": "entry was disabled or re-generated while this seed was in flight"}
        if not store.transition(pid, "active",
                                note="factory default (standing approval)"):
            return {"status": "refused", "policy_id": pid,
                    "reason": "activation refused by the store — the record was already cancelled"}
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
    newly-joined room. `member_rooms` defaults to just the new room."""
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
def _pending_drafts_for(store: "op.SubscriptionStore", key: str) -> "list[dict]":
    """Pack drafts awaiting owner approval — not active, so not budget-counted,
    but draft->active is legal, so authority-revoking sweeps must include them."""
    return [r for r in store.list(status="draft")
            if (r.get("pack") or {}).get("entry") == key]


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
        # Rooms held past the budget await the owner's decision and must be
        # visible in this view, not only in the store.
        out.append({
            "key": entry["key"],
            "label": entry["label"],
            "description": entry["description"],
            "enabled": is_enabled(store_dir, entry["key"]),
            "active_rooms": sorted(r["room_id"] for r in active),
            "awaiting_approval": sorted(
                r["room_id"] for r in _pending_drafts_for(store, entry["key"])),
        })
    return out


def set_enabled(store_dir: str, key: str, enabled: bool) -> dict:
    """Owner disable/enable. Disabling cancels live records + pending drafts and
    bumps the generation (`cancelled` is terminal); enabling only clears the flag."""
    entry = _entry(key)
    if not entry:
        return {"ok": False, "reason": f"unknown pack entry: {key}"}
    store = op.SubscriptionStore(store_dir)
    # Commit + sweep are one critical section against a concurrent seed: the
    # lock closes the window where a seed writes between commit and sweep.
    with op.store_lock(store_dir):
        return _set_enabled_locked(store, store_dir, key, enabled)


def _set_enabled_locked(store, store_dir: str, key: str, enabled: bool) -> dict:
    entry = _entry(key)
    st = load_pack_state(store_dir)
    es = _entry_state(st, key)
    was_disabled = es["disabled"]
    if not enabled:
        cancelled = 0
        # Commit the flag FIRST, then sweep active records AND pending drafts —
        # draft->active is legal, and `cancelled` is terminal in the store.
        if not was_disabled:
            es["generation"] += 1  # next enable seeds a fresh generation
        es["disabled"] = True
        save_pack_state(store_dir, st)

        for rec in _active_records_for(store, key) + _pending_drafts_for(store, key):
            if store.transition(rec["policy_id"], "cancelled",
                                 note="pack entry disabled by owner"):
                cancelled += 1
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
