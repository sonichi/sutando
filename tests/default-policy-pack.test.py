#!/usr/bin/env python3
"""Tests for skills/observe/default_policy_pack.py — the factory-default
subscription pack for the events/observe lane.

Covers: connect-time seeding across member rooms, idempotent re-seed, per-room
record shape (concrete room_id + pack provenance + observe/notify-only), reuse
of observe_policy's standing-approval boundary (fail-closed on non-owner /
out-of-scope room), owner disable→cancel + re-enable→re-seed (generation bump),
join-time incremental seeding, deterministic id shape. Exit 0/1."""
import os
import sys
import tempfile
from pathlib import Path

_OBS = Path(__file__).resolve().parent.parent / "skills" / "observe"
sys.path.insert(0, str(_OBS))
import observe_policy as op  # noqa: E402
import default_policy_pack as dpp  # noqa: E402

FAILS: list = []
OWNER = "@qingyun:ag2.space"
ROOMS = ["!master:ag2.space", "!dev:ag2.space"]


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _store():
    return tempfile.mkdtemp()


def test_react_baseline_event_type_is_valid():
    entry = dpp._entry("react_baseline")
    check(entry is not None, "react_baseline entry exists")
    check(all(op._EVENT_TYPE_RE.match(t) for t in entry["event_types"]),
          "react baseline event types satisfy observe_policy._EVENT_TYPE_RE")
    check(entry["mode"] in op.STANDING_MODES,
          "react baseline mode is inside the standing-approval mode set")
    check(entry["cost_cap"]["evals_per_day"] <= op.DEFAULT_EVALS_PER_DAY,
          "react baseline cost cap <= default (stays in standing scope)")


def test_seed_defaults_activates_per_room():
    d = _store()
    res = dpp.seed_defaults(d, OWNER, ROOMS)
    check(len(res) == len(ROOMS) and all(r["status"] == "seeded" for r in res),
          "seed_defaults seeds one active policy per member room")
    store = op.SubscriptionStore(d)
    active = store.list(status="active")
    check(len(active) == len(ROOMS), "one active record per room in the store")
    for rec in active:
        check(rec["room_id"] in ROOMS, "record carries a concrete member room_id")
        check(rec["event_types"] == ["message.created"], "record observes message.created")
        check(rec["mode"] == "observe", "record is observe (notify-only)")
        check((rec.get("pack") or {}).get("entry") == "react_baseline",
              "record carries pack provenance")
        check(op._POLICY_ID_RE.match(rec["policy_id"]) is not None,
              "record id has the generated obs_ shape (path-safe)")


def test_seed_is_idempotent():
    d = _store()
    dpp.seed_defaults(d, OWNER, ROOMS)
    res2 = dpp.seed_defaults(d, OWNER, ROOMS)
    check(all(r["status"] == "skipped" for r in res2),
          "re-seeding the same rooms skips (idempotent, no duplicates)")
    check(len(op.SubscriptionStore(d).list(status="active")) == len(ROOMS),
          "still exactly one active record per room after re-seed")


def test_reseed_does_not_resurrect_owner_cancelled_record():
    # Regression for John #2320: after seeding, the owner directly cancels ONE
    # room's record (same generation — NOT a pack disable/re-enable). A
    # reconnect reseed must NOT flip it back to active; that would silently undo
    # the owner's cancellation. The prior guard only skipped `active` records,
    # so it re-seeded + re-activated the cancelled one.
    d = _store()
    dpp.seed_defaults(d, OWNER, ROOMS)
    pid = op.SubscriptionStore(d).list(status="active")[0]["policy_id"]
    op.SubscriptionStore(d).transition(pid, "cancelled", note="owner direct cancel")
    check(op.SubscriptionStore(d).get(pid)["status"] == "cancelled",
          "precondition: owner cancelled the record")
    res = dpp.seed_defaults(d, OWNER, ROOMS)  # reconnect reseed, same generation
    check(op.SubscriptionStore(d).get(pid)["status"] == "cancelled",
          "reseed does NOT resurrect the owner-cancelled current-generation record")
    r = [x for x in res if x["policy_id"] == pid]
    check(bool(r) and r[0]["status"] == "skipped",
          "reseed reports the existing (cancelled) record as skipped, not seeded")


def test_standing_approval_boundary_reused_failclosed():
    d = _store()
    store = op.SubscriptionStore(d)
    entry = dpp._entry("react_baseline")
    # non-owner creator -> refused (boundary: created_by != owner)
    r = dpp.seed_room(store, d, entry, "!x:ag2.space",
                      owner_mxid=OWNER, owner_rooms=["!x:ag2.space"])
    # owner_mxid is OWNER but created_by inside the draft is OWNER too, so this
    # one should PASS; assert the pass, then assert the fail-closed variants.
    check(r["status"] == "seeded", "owner + in-scope room seeds")
    # room not in owner_rooms -> refused
    r2 = dpp.seed_room(store, d, entry, "!y:ag2.space",
                       owner_mxid=OWNER, owner_rooms=["!z:ag2.space"])
    check(r2["status"] == "refused" and "scoped rooms" in r2["reason"],
          "room outside owner-scoped set is refused, not activated")
    # empty owner mxid -> refused (fail-closed)
    r3 = dpp.seed_room(store, d, entry, "!q:ag2.space",
                       owner_mxid="", owner_rooms=["!q:ag2.space"])
    check(r3["status"] == "refused", "empty owner mxid is refused (fail-closed)")


def test_disable_cancels_and_reenable_reseeds():
    d = _store()
    dpp.seed_defaults(d, OWNER, ROOMS)
    out = dpp.set_enabled(d, "react_baseline", False)
    check(out["ok"] and out["cancelled_rooms"] == len(ROOMS),
          "disable cancels every live per-room subscription")
    check(len(op.SubscriptionStore(d).list(status="active")) == 0,
          "no active records remain after disable")
    check(dpp.is_enabled(d, "react_baseline") is False, "entry reads disabled")
    # enabled entries are skipped by seed while disabled
    res_while_disabled = dpp.seed_defaults(d, OWNER, ROOMS)
    check(res_while_disabled == [], "disabled entry is not seeded")
    # re-enable -> fresh generation seeds again
    dpp.set_enabled(d, "react_baseline", True)
    res = dpp.seed_defaults(d, OWNER, ROOMS)
    check(all(r["status"] == "seeded" for r in res) and len(res) == len(ROOMS),
          "re-enable + seed activates a fresh generation")
    check(len(op.SubscriptionStore(d).list(status="active")) == len(ROOMS),
          "active count restored after re-enable")


def test_on_room_join_seeds_new_room():
    d = _store()
    dpp.seed_defaults(d, OWNER, ROOMS)
    new_room = "!newly-joined:ag2.space"
    res = dpp.on_room_join(d, OWNER, new_room, member_rooms=ROOMS + [new_room])
    check(len(res) == 1 and res[0]["status"] == "seeded" and res[0]["room_id"] == new_room,
          "on_room_join seeds the enabled entry into the newly-joined room")
    active_rooms = {r["room_id"] for r in op.SubscriptionStore(d).list(status="active")}
    check(new_room in active_rooms, "new room now has an active subscription")


def test_deterministic_id_shape_and_stability():
    a = dpp._policy_id_for("react_baseline", 1, "!r:ag2.space")
    b = dpp._policy_id_for("react_baseline", 1, "!r:ag2.space")
    c = dpp._policy_id_for("react_baseline", 2, "!r:ag2.space")
    check(a == b, "id is deterministic per (entry, generation, room)")
    check(a != c, "generation bump changes the id")
    check(op._POLICY_ID_RE.match(a) is not None,
          "derived id matches observe_policy._POLICY_ID_RE")


def test_list_pack_and_unknown_entry_branches():
    d = _store()
    dpp.seed_defaults(d, OWNER, ROOMS)
    rows = dpp.list_pack(d)
    check(len(rows) == len(dpp.PACK_ENTRIES) and rows[0]["key"] == "react_baseline",
          "list_pack returns a row per entry with live room counts")
    check(dpp.is_enabled(d, "does_not_exist") is False,
          "is_enabled is False for an unknown entry (fail-closed)")
    out = dpp.set_enabled(d, "does_not_exist", False)
    check(out["ok"] is False and "unknown" in out["reason"],
          "set_enabled refuses an unknown entry")


def test_on_room_join_default_scope_and_disabled_skip():
    d = _store()
    # default member_rooms=None -> owner_rooms defaults to [room_id]
    res = dpp.on_room_join(d, OWNER, "!solo:ag2.space")
    check(len(res) == 1 and res[0]["status"] == "seeded",
          "on_room_join with default member_rooms seeds using the room itself as scope")
    # disabled entry is skipped by on_room_join too
    dpp.set_enabled(d, "react_baseline", False)
    res2 = dpp.on_room_join(d, OWNER, "!another:ag2.space")
    check(res2 == [], "on_room_join skips a disabled entry")


def test_wrong_scope_entry_is_skipped():
    d = _store()
    dummy = {"key": "_dummy_scope", "label": "x", "description": "x",
             "event_types": ["message.created"], "mode": "observe",
             "cost_cap": {"evals_per_day": 1}, "scope": "single_room",
             "default_enabled": True}
    dpp.PACK_ENTRIES.append(dummy)
    try:
        res = dpp.seed_defaults(d, OWNER, ROOMS)
        seeded_keys = {r["entry"] for r in res}
        check("_dummy_scope" not in seeded_keys,
              "seed_defaults skips an entry whose scope != all_member_rooms")
        res2 = dpp.on_room_join(d, OWNER, "!z:ag2.space", member_rooms=["!z:ag2.space"])
        check(all(r["entry"] != "_dummy_scope" for r in res2),
              "on_room_join skips a non-all-member-rooms entry")
    finally:
        dpp.PACK_ENTRIES.remove(dummy)


def test_cli_main():
    d = _store()
    check(dpp.main(["seed", "--owner", OWNER, "--rooms", ",".join(ROOMS), "--store", d]) == 0,
          "CLI seed exits 0")
    check(dpp.main(["list", "--store", d]) == 0, "CLI list exits 0")
    check(dpp.main(["disable", "react_baseline", "--store", d]) == 0, "CLI disable exits 0")
    check(dpp.is_enabled(d, "react_baseline") is False, "CLI disable took effect")
    check(dpp.main(["enable", "react_baseline", "--store", d]) == 0, "CLI enable exits 0")
    # error paths
    check(dpp.main(["disable", "--store", d]) == 2, "CLI disable without key exits 2")
    check(dpp.main(["seed", "--store", d]) == 2, "CLI seed without owner/rooms exits 2")


def test_default_store_dir_resolves():
    p = dpp._default_store_dir()
    check(p.endswith(os.path.join("state", "observe")),
          "_default_store_dir resolves to <workspace>/state/observe")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"# {name}")
            fn()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)})")
        return 1
    print("\nPASS — default policy pack")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
