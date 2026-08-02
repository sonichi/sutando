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


def test_reseed_resumes_crash_interrupted_draft():
    # Regression (#2320, my inline finding): seed_room does store.save() then a
    # SEPARATE store.transition(active). A crash between them leaves a
    # non-terminal DRAFT for this (entry, generation, room). The any-state-exists
    # guard returned "skipped", so reconnect found the deterministic draft and
    # left the room UNSUBSCRIBED FOREVER. Reconnect must RESUME the draft to
    # active (self-heal), distinct from skipping terminal cancelled above.
    d = _store()
    entry = dpp._entry("react_baseline")
    room = ROOMS[0]
    gen = dpp._entry_state(dpp.load_pack_state(d), entry["key"])["generation"]
    pid = dpp._policy_id_for(entry["key"], gen, room)
    store = op.SubscriptionStore(d)
    # Simulate the crash: the validated record is saved but never transitioned.
    normalized, errors = op.validate_draft(dpp._draft_for(entry, room, OWNER, gen))
    check(not errors, "setup: factory draft validates")
    normalized["pack"] = {"entry": entry["key"], "generation": gen}
    store.save(normalized)
    check(store.get(pid)["status"] == "draft",
          "precondition: a crash leaves a non-terminal draft record")
    # Reconnect reseed must resume, not skip.
    r = dpp.seed_room(store, d, entry, room, owner_mxid=OWNER, owner_rooms=ROOMS)
    check(r["status"] == "resumed",
          "reseed RESUMES a crash-interrupted draft (not 'skipped')")
    check(op.SubscriptionStore(d).get(pid)["status"] == "active",
          "the resumed draft is now active — room subscribed, not stranded forever")
    # A subsequent reseed is idempotent again (now active -> skipped).
    r2 = dpp.seed_room(op.SubscriptionStore(d), d, entry, room,
                       owner_mxid=OWNER, owner_rooms=ROOMS)
    check(r2["status"] == "skipped",
          "a resumed (now active) record is skipped on the next reseed (idempotent)")


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


def _aggregate(d):
    """Sum the evals/day of every ACTIVE pack record, computed from the store on
    disk rather than by calling dpp.committed_evals_per_day().

    Deliberate: using the module's own accounting to verify the module's own
    budget would be self-referential — if that helper miscounted, the assertion
    would agree with it and the suite would go green on a broken budget. This
    reads the persisted records directly, so the test and the code can disagree.
    """
    total = 0
    for rec in op.SubscriptionStore(d).list(status="active"):
        if not (rec.get("pack") or {}).get("entry"):
            continue
        cap = (rec.get("cost_cap") or {}).get("evals_per_day")
        if isinstance(cap, int):
            total += cap
    return total


def test_multi_room_fanout_stays_inside_the_aggregate_budget():
    """john-the-dev's #2320 blocker. evaluate_standing_approval() checks ONE
    draft's cap, so N rooms each at the default cap all pass individually while
    the total the owner authorized grows to N x default. Reproduced on 144ea820:
    three rooms -> caps [2,2,2], aggregate 6, advertised default 2."""
    d = _store()
    rooms = [f"!r{i}:ag2.space" for i in range(15)]
    res = dpp.seed_defaults(d, OWNER, rooms)
    seeded = [r for r in res if r["status"] == "seeded"]
    refused = [r for r in res if r["status"] == "refused"]

    budget = getattr(dpp, "PACK_AGGREGATE_EVALS_PER_DAY", None)
    check(budget is not None, "pack declares an aggregate budget")
    check(budget is not None and _aggregate(d) <= budget,
          f"15-room fan-out stays within the aggregate budget "
          f"({_aggregate(d)} <= {budget})")
    check(len(seeded) * op.DEFAULT_EVALS_PER_DAY == _aggregate(d),
          "aggregate equals the sum of what was actually activated")
    check(len(refused) > 0,
          "rooms beyond the budget are REFUSED, not silently activated")
    # The refusal must be legible. A bare "refused" would leave the owner unable
    # to tell a budget stop from a scope or mode rejection, which are different
    # problems with different fixes.
    check(refused and "aggregate budget" in refused[0]["reason"],
          f"refusal names the budget as the cause: {refused[0]['reason'] if refused else 'n/a'}")


def test_a_later_join_cannot_widen_a_pre_blessed_aggregate():
    """The second half of the blocker: on_room_join() runs long after connect,
    so an unbounded per-room grant means the authorized total keeps growing with
    no policy edit and no renewed approval."""
    d = _store()
    rooms = [f"!r{i}:ag2.space" for i in range(15)]
    dpp.seed_defaults(d, OWNER, rooms)
    before = _aggregate(d)
    res = dpp.on_room_join(d, OWNER, "!late:ag2.space",
                           member_rooms=rooms + ["!late:ag2.space"])
    check(res and res[0]["status"] == "refused",
          f"a join at the budget is refused (got {res[0]['status'] if res else 'no result'})")
    check(_aggregate(d) == before,
          f"aggregate is unchanged by the refused join ({before} -> {_aggregate(d)})")


def test_a_join_under_budget_still_seeds():
    """CALIBRATION. Both assertions above are satisfied by a blanket refusal, so
    they would still pass if the budget check rejected everything and broke the
    feature outright. Pin the positive case: under budget, a join still works."""
    d = _store()
    dpp.seed_defaults(d, OWNER, ["!x:ag2.space"])
    before = _aggregate(d)
    res = dpp.on_room_join(d, OWNER, "!y:ag2.space",
                           member_rooms=["!x:ag2.space", "!y:ag2.space"])
    check(res and res[0]["status"] == "seeded",
          f"an under-budget join still seeds (got {res[0]['status'] if res else 'no result'})")
    check(_aggregate(d) == before + op.DEFAULT_EVALS_PER_DAY,
          "...and the aggregate grows by exactly one room's cap")


def test_reseeding_does_not_double_count_the_budget():
    """The budget sums ACTIVE records, and re-seed is idempotent, so a reconnect
    must not consume budget a second time — otherwise repeated reconnects would
    starve the pack out of its own allowance."""
    d = _store()
    rooms = [f"!r{i}:ag2.space" for i in range(5)]
    dpp.seed_defaults(d, OWNER, rooms)
    first = _aggregate(d)
    dpp.seed_defaults(d, OWNER, rooms)
    dpp.seed_defaults(d, OWNER, rooms)
    check(_aggregate(d) == first,
          f"aggregate unchanged across repeated reseeds ({first} -> {_aggregate(d)})")


def test_budget_counts_only_pack_provenance_records():
    """An explicitly owner-approved policy must not shrink the pack's automatic
    allowance — approving something should never make the next automatic grant
    harder. Only records carrying pack provenance count."""
    d = _store()
    store = op.SubscriptionStore(d)
    draft, errs = op.validate_draft({
        "room_id": "!manual:ag2.space", "created_by": OWNER,
        "event_types": ["message.created"], "mode": "observe",
        "cost_cap": {"evals_per_day": op.DEFAULT_EVALS_PER_DAY},
    })
    check(not errs, f"fixture draft validates ({errs})")
    store.save(draft)
    store.transition(draft["policy_id"], "active", note="owner-approved, not pack")
    check(_aggregate(d) == 0,
          f"a non-pack active policy contributes 0 to the pack budget (got {_aggregate(d)})")


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
