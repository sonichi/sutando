#!/usr/bin/env python3
"""Tests for skills/observe/default_policy_pack.py — the factory-default
subscription pack for the events/observe lane. Exit 0/1."""
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
    # A reconnect reseed must not flip an owner-cancelled same-generation
    # record back to active.
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
    # A crash between save() and transition(active) leaves a non-terminal draft;
    # reconnect must RESUME it to active, not skip it forever.
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
    """Sum evals/day of ACTIVE pack records straight from the store on disk —
    independent of committed_evals_per_day(), so test and code can disagree."""
    total = 0
    for rec in op.SubscriptionStore(d).list(status="active"):
        if not (rec.get("pack") or {}).get("entry"):
            continue
        cap = (rec.get("cost_cap") or {}).get("evals_per_day")
        if isinstance(cap, int):
            total += cap
    return total


def test_multi_room_fanout_stays_inside_the_aggregate_budget():
    """N rooms each pass the per-draft cap individually while the authorized
    total grows to N x default; the aggregate budget must bound the fan-out."""
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
    # The refusal must name the budget — a bare "refused" is indistinguishable
    # from a scope or mode rejection.
    check(refused and "aggregate budget" in refused[0]["reason"],
          f"refusal names the budget as the cause: {refused[0]['reason'] if refused else 'n/a'}")


def test_a_later_join_cannot_widen_a_pre_blessed_aggregate():
    """on_room_join() runs long after connect; an unbounded per-room grant would
    keep widening the authorized total with no renewed approval."""
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
    """CALIBRATION: the refusal assertions above would pass on a blanket refusal;
    pin the positive case — under budget, a join still seeds."""
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
    """A reconnect reseed is idempotent and must not consume budget again, or
    repeated reconnects would starve the pack of its own allowance."""
    d = _store()
    rooms = [f"!r{i}:ag2.space" for i in range(5)]
    dpp.seed_defaults(d, OWNER, rooms)
    first = _aggregate(d)
    dpp.seed_defaults(d, OWNER, rooms)
    dpp.seed_defaults(d, OWNER, rooms)
    check(_aggregate(d) == first,
          f"aggregate unchanged across repeated reseeds ({first} -> {_aggregate(d)})")


def test_budget_counts_only_pack_provenance_records():
    """An owner-approved policy must not shrink the pack's automatic allowance;
    only records carrying pack provenance count toward the budget."""
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


def test_over_budget_room_leaves_an_APPROVABLE_record():
    """The over-budget refusal promises explicit approval; that must be backed
    by a persisted draft the owner can transition to active."""
    d = _store()
    rooms = [f"!r{i}:ag2.space" for i in range(11)]
    res = dpp.seed_defaults(d, OWNER, rooms)
    refused = [r for r in res if r["status"] == "refused"]
    check(len(refused) == 1, f"one room lands over budget (got {len(refused)})")
    store = op.SubscriptionStore(d)
    rec = store.get(refused[0]["policy_id"])
    check(rec is not None and rec.get("status") == "draft",
          f"the over-budget policy is persisted as a draft (got {rec and rec.get('status')})")
    check("draft" in refused[0] and refused[0].get("awaiting") == "owner-approval",
          "the refusal result carries the draft + an explicit awaiting marker")
    # The approval path must work end to end.
    check(store.transition(refused[0]["policy_id"], "active", note="owner approved") is True,
          "transition(pid,'active') now has a record to act on")
    # A draft must not consume budget, or the refusal would shrink the allowance
    # every time it fired and the pack would starve itself.
    d2 = _store()
    dpp.seed_defaults(d2, OWNER, rooms)
    check(_aggregate(d2) <= dpp.PACK_AGGREGATE_EVALS_PER_DAY,
          f"a persisted draft consumes no budget ({_aggregate(d2)})")


def test_a_persisted_draft_does_not_self_activate_while_over_budget():
    """CONTROL: the resume path re-runs the budget check, so a persisted
    over-budget draft must not self-activate on the next reconnect."""
    d = _store()
    rooms = [f"!r{i}:ag2.space" for i in range(11)]
    dpp.seed_defaults(d, OWNER, rooms)
    before = _aggregate(d)
    again = dpp.seed_defaults(d, OWNER, rooms)
    check(not [r for r in again if r["status"] == "resumed"],
          "no draft resumes while the budget is still saturated")
    check(_aggregate(d) == before,
          f"aggregate unchanged across the reseed ({before} -> {_aggregate(d)})")


def test_the_draft_resumes_once_budget_frees_up():
    """CALIBRATION: the queued draft must take freed allowance on the next seed
    — self-healing, not a dead end."""
    d = _store()
    rooms = [f"!r{i}:ag2.space" for i in range(11)]
    dpp.seed_defaults(d, OWNER, rooms)
    store = op.SubscriptionStore(d)
    victim = store.list(status="active")[0]
    store.transition(victim["policy_id"], "cancelled", note="owner cancel")
    after = dpp.seed_defaults(d, OWNER, rooms)
    check(any(r["status"] == "resumed" for r in after),
          "the queued draft activates once a room is cancelled")
    check(_aggregate(d) <= dpp.PACK_AGGREGATE_EVALS_PER_DAY,
          f"...and the budget still holds ({_aggregate(d)})")


def test_disable_revokes_pending_drafts_so_a_stale_card_cannot_activate():
    """Disable must revoke pending over-budget drafts too: draft->active is a
    legal transition, so a stale approval card could otherwise activate a room."""
    d = _store()
    rooms = [f"!r{i}:ag2.space" for i in range(11)]
    res = dpp.seed_defaults(d, OWNER, rooms)
    refused = [r for r in res if r["status"] == "refused"]
    check(len(refused) == 1, "precondition: one room is over budget and left as a draft")
    pid = refused[0]["policy_id"]
    store = op.SubscriptionStore(d)
    check(store.get(pid)["status"] == "draft", "precondition: it really is a draft")

    dpp.set_enabled(d, "react_baseline", False)
    check(dpp.is_enabled(d, "react_baseline") is False, "entry reads disabled")
    check(store.get(pid)["status"] == "cancelled",
          f"disable REVOKES the pending draft (got {store.get(pid)['status']})")
    # The consequence is the real assertion: `cancelled` is terminal, so a late
    # click on a stale card must not resurrect the room.
    check(store.transition(pid, "active", note="stale card clicked after disable") is False,
          "a stale approval CANNOT activate a room after the owner disabled the entry")
    check(store.get(pid)["status"] == "cancelled", "...and the record stays cancelled")


def test_disable_then_reenable_still_seeds_a_fresh_generation():
    """CALIBRATION: the guard above is satisfied by a disable that destroys the
    entry permanently; re-enabling must still seed."""
    d = _store()
    rooms = [f"!r{i}:ag2.space" for i in range(11)]
    dpp.seed_defaults(d, OWNER, rooms)
    dpp.set_enabled(d, "react_baseline", False)
    dpp.set_enabled(d, "react_baseline", True)
    again = dpp.seed_defaults(d, OWNER, rooms)
    check(any(r["status"] == "seeded" for r in again),
          "re-enable seeds a fresh generation after a disable")
    check(_aggregate(d) <= dpp.PACK_AGGREGATE_EVALS_PER_DAY,
          f"...and the aggregate budget still holds ({_aggregate(d)})")


def test_a_seed_racing_the_disable_cannot_survive_it():
    """A seed in flight during a disable must be refused in both variants: over
    budget (stranded draft) and under budget (straight to active, no click)."""
    entry = dpp._entry("react_baseline")
    rooms = [f"!r{i}:ag2.space" for i in range(11)]

    # --- variant A: inject at the exact window (right after the draft snapshot)
    d = _store()
    dpp.seed_defaults(d, OWNER, rooms)
    store = op.SubscriptionStore(d)
    caught = {}
    orig_drafts = dpp._pending_drafts_for
    def hooked(s, k):
        out = orig_drafts(s, k)
        if not caught:
            r = dpp.seed_room(op.SubscriptionStore(d), d, entry, "!late:ag2.space",
                              owner_mxid=OWNER, owner_rooms=rooms + ["!late:ag2.space"])
            caught["pid"] = r["policy_id"]; caught["status"] = r["status"]
        return out
    dpp._pending_drafts_for = hooked
    try:
        dpp.set_enabled(d, "react_baseline", False)
    finally:
        dpp._pending_drafts_for = orig_drafts
    check(caught.get("status") == "refused",
          f"A: a seed racing the sweep is REFUSED (got {caught.get('status')})")
    check(store.get(caught["pid"]) is None,
          "A: ...and nothing is persisted for it at all")

    # --- variant B: inject mid-sweep, once budget has been freed by cancellations
    d2 = _store()
    dpp.seed_defaults(d2, OWNER, rooms)
    store2 = op.SubscriptionStore(d2)
    caught2 = {}
    orig_tr = op.SubscriptionStore.transition
    def racing(self, pid, to, note=""):
        r = orig_tr(self, pid, to, note)
        if not caught2 and to == "cancelled":
            x = dpp.seed_room(op.SubscriptionStore(d2), d2, entry, "!late:ag2.space",
                              owner_mxid=OWNER, owner_rooms=rooms + ["!late:ag2.space"])
            caught2["pid"] = x["policy_id"]; caught2["status"] = x["status"]
        return r
    op.SubscriptionStore.transition = racing
    try:
        dpp.set_enabled(d2, "react_baseline", False)
    finally:
        op.SubscriptionStore.transition = orig_tr
    check(caught2.get("status") == "refused",
          f"B: an under-budget racing seed is REFUSED, not auto-ACTIVATED (got {caught2.get('status')})")
    check(store2.get(caught2["pid"]) is None,
          "B: ...and no live subscription is left on a disabled entry")


def test_disable_commits_its_flag_before_sweeping():
    """If the flag were written last, a mid-sweep reader would see the entry as
    ENABLED while its records were already being cancelled."""
    d = _store()
    dpp.seed_defaults(d, OWNER, [f"!r{i}:ag2.space" for i in range(3)])
    seen = []
    orig_tr = op.SubscriptionStore.transition
    def observe(self, pid, to, note=""):
        seen.append(dpp.is_enabled(d, "react_baseline"))
        return orig_tr(self, pid, to, note)
    op.SubscriptionStore.transition = observe
    try:
        dpp.set_enabled(d, "react_baseline", False)
    finally:
        op.SubscriptionStore.transition = orig_tr
    check(seen and not any(seen),
          f"the entry already reads DISABLED during every cancellation (saw {seen})")


def test_owner_approval_does_not_consume_the_automatic_allowance():
    """Approving an over-budget draft must not count against the pack's budget,
    or approvals would permanently starve subsequent auto-seeds."""
    d = _store()
    rooms = [f"!r{i}:ag2.space" for i in range(13)]      # 10 fit the budget, 3 do not
    res = dpp.seed_defaults(d, OWNER, rooms)
    store = op.SubscriptionStore(d)
    drafts = [r for r in res if r["status"] == "refused"]
    check(len(drafts) == 3, f"precondition: 3 rooms land over budget (got {len(drafts)})")
    check(_aggregate(d) <= dpp.PACK_AGGREGATE_EVALS_PER_DAY,
          f"precondition: auto-seeding is capped ({_aggregate(d)})")

    for r in drafts:
        check(store.transition(r["policy_id"], "active", note="owner approved") is True,
              "the owner CAN approve a queued room (approval is not blocked)")
    check(dpp.committed_evals_per_day(store) <= dpp.PACK_AGGREGATE_EVALS_PER_DAY,
          f"owner approvals do not consume the automatic allowance "
          f"(got {dpp.committed_evals_per_day(store)})")

    # The consequence that makes it matter: the pack must still be able to seed.
    auto = [x for x in store.list(status="active")
            if (x.get("pack") or {}).get("entry") and not (x.get("pack") or {}).get("over_budget")]
    store.transition(auto[0]["policy_id"], "cancelled", note="owner cancels an auto room")
    r2 = dpp.on_room_join(d, OWNER, "!fresh:ag2.space",
                          member_rooms=rooms + ["!fresh:ag2.space"])
    check(r2 and r2[0]["status"] == "seeded",
          f"...so freeing an auto slot still lets a new room seed (got {r2 and r2[0]['status']})")


def test_the_cap_still_bounds_AUTOMATIC_grants():
    """CALIBRATION: the guard above is satisfied by removing the budget entirely;
    the cap must still bind automatic seeds."""
    d = _store()
    rooms = [f"!r{i}:ag2.space" for i in range(13)]
    res = dpp.seed_defaults(d, OWNER, rooms)
    seeded = [r for r in res if r["status"] == "seeded"]
    check(len(seeded) * op.DEFAULT_EVALS_PER_DAY <= dpp.PACK_AGGREGATE_EVALS_PER_DAY,
          f"automatic seeding is still capped at the budget ({len(seeded)} auto-seeds)")
    check(len(seeded) < len(rooms),
          "...and rooms beyond it are still refused, not silently granted")


def test_list_pack_shows_rooms_awaiting_the_owners_approval():
    """Rooms awaiting approval must be visible in the owner view — a decision
    the owner cannot see is not a decision the owner has."""
    d = _store()
    rooms = [f"!r{i}:ag2.space" for i in range(13)]
    res = dpp.seed_defaults(d, OWNER, rooms)
    queued = sorted(r["room_id"] for r in res if r["status"] == "refused")
    check(len(queued) == 3, f"precondition: 3 rooms are queued (got {len(queued)})")

    view = dpp.list_pack(d)[0]
    check(view.get("awaiting_approval") == queued,
          f"the owner view lists every room awaiting her approval (got {view.get('awaiting_approval')})")
    check(len(view["active_rooms"]) == 10,
          "...and still reports the auto-activated rooms separately")

    # Approving one must MOVE it, not duplicate it into both lists.
    store = op.SubscriptionStore(d)
    pid = [r for r in res if r["status"] == "refused"][0]["policy_id"]
    store.transition(pid, "active", note="owner approved")
    v2 = dpp.list_pack(d)[0]
    check(not (set(v2["active_rooms"]) & set(v2["awaiting_approval"])),
          "an approved room moves from awaiting -> active, never appears in both")
    check(len(v2["active_rooms"]) == 11 and len(v2["awaiting_approval"]) == 2,
          f"counts move together (active {len(v2['active_rooms'])}, awaiting {len(v2['awaiting_approval'])})")

    # CALIBRATION: the field must be able to be EMPTY, or "always lists 3" would pass.
    dpp.set_enabled(d, "react_baseline", False)
    v3 = dpp.list_pack(d)[0]
    check(v3["awaiting_approval"] == [],
          f"a disabled entry shows nothing pending — drafts were revoked (got {v3['awaiting_approval']})")


def test_seed_racing_a_disable_after_its_live_check_never_activates():
    """A disable landing between _entry_still_live() (a read) and the following
    save/activate writes must never leave the seed active."""
    d = _store()
    entry = dpp._entry("react_baseline")
    rooms = [f"!r{i}:ag2.space" for i in range(2)]
    dpp.seed_defaults(d, OWNER, rooms)

    real = dpp._entry_still_live
    fired = []

    def racing(store_dir, key, gen):
        ok = real(store_dir, key, gen)
        if not fired:                      # only the FIRST (pre-persist) check
            fired.append(True)
            dpp.set_enabled(store_dir, key, False)   # the owner disables RIGHT HERE
        return ok

    dpp._entry_still_live = racing
    try:
        r = dpp.seed_room(op.SubscriptionStore(d), d, entry, "!late:ag2.space",
                          owner_mxid=OWNER, owner_rooms=rooms + ["!late:ag2.space"])
    finally:
        dpp._entry_still_live = real

    # CONTROL FIRST: without this, every assertion below passes vacuously on a
    # run where the injection never fired.
    check(bool(fired), "control: the disable injection actually fired")
    check(not dpp.is_enabled(d, "react_baseline"),
          "control: the entry really is disabled by the end of the race")

    pid = r.get("policy_id")
    rec = op.SubscriptionStore(d).get(pid) if pid else None
    status = (rec or {}).get("status")
    check(status != "active",
          f"a seed that lost the race to a disable is never left ACTIVE (got {status!r})")


def test_over_budget_draft_racing_a_disable_is_not_left_live():
    """Same after-check/before-save window on the over-budget branch: a disable
    there must not strand a live draft (draft->active stays legal)."""
    d = _store()
    entry = dpp._entry("react_baseline")
    # Fill the aggregate budget so the next room takes the over-budget branch.
    n = dpp.PACK_AGGREGATE_EVALS_PER_DAY // op.DEFAULT_EVALS_PER_DAY
    rooms = [f"!r{i}:ag2.space" for i in range(n)]
    dpp.seed_defaults(d, OWNER, rooms)

    committed_before = dpp.committed_evals_per_day(op.SubscriptionStore(d))

    real = dpp._entry_still_live
    fired = []

    def racing(store_dir, key, gen):
        ok = real(store_dir, key, gen)
        if not fired:
            fired.append(True)
            dpp.set_enabled(store_dir, key, False)
        return ok

    dpp._entry_still_live = racing
    try:
        r = dpp.seed_room(op.SubscriptionStore(d), d, entry, "!over:ag2.space",
                          owner_mxid=OWNER, owner_rooms=rooms + ["!over:ag2.space"])
    finally:
        dpp._entry_still_live = real

    check(bool(fired), "control: the disable injection fired on the over-budget path")
    # A "status == refused" check cannot prove the over-budget branch ran;
    # assert the precondition — the aggregate was exhausted before the seed.
    check(committed_before == dpp.PACK_AGGREGATE_EVALS_PER_DAY,
          f"control: budget really was exhausted first (got {committed_before}/"
          f"{dpp.PACK_AGGREGATE_EVALS_PER_DAY})")

    pid = r.get("policy_id")
    rec = op.SubscriptionStore(d).get(pid) if pid else None
    status = (rec or {}).get("status")
    check(status in (None, "cancelled"),
          f"no live draft is left on a disabled entry (got {status!r})")


def test_resume_reports_refused_when_the_record_was_cancelled_mid_flight():
    """The resume branch must honour transition()'s return value: a record
    cancelled mid-flight must not be reported as resumed."""
    d = _store()
    entry = dpp._entry("react_baseline")
    room = ROOMS[0]
    gen = dpp._entry_state(dpp.load_pack_state(d), entry["key"])["generation"]
    pid = dpp._policy_id_for(entry["key"], gen, room)
    store = op.SubscriptionStore(d)

    # Simulate the crash: validated record saved, never transitioned.
    normalized, errors = op.validate_draft(dpp._draft_for(entry, room, OWNER, gen))
    check(not errors, "setup: factory draft validates")
    normalized["pack"] = {"entry": entry["key"], "generation": gen}
    store.save(normalized)
    check(store.get(pid)["status"] == "draft",
          "precondition: a crash leaves a non-terminal draft record")

    # The owner disables while the resume is in flight. Because the draft already
    # exists, the sweep CANCELS it -- a real interleaving, not a stubbed return.
    real = dpp._entry_still_live
    fired = []

    def racing(store_dir, key, g):
        ok = real(store_dir, key, g)
        if not fired:
            fired.append(True)
            dpp.set_enabled(store_dir, key, False)
        return ok

    dpp._entry_still_live = racing
    try:
        r = dpp.seed_room(store, d, entry, room, owner_mxid=OWNER, owner_rooms=ROOMS)
    finally:
        dpp._entry_still_live = real

    check(bool(fired), "control: the disable injection fired on the resume path")
    # Assert the end state: a disabled entry must never be left with a live
    # record, however many writers touched it on the way.
    check(store.get(pid)["status"] != "active",
          f"a disabled entry is never left ACTIVE by the resume path "
          f"(got {store.get(pid)['status']!r})")
    check(r["status"] != "resumed",
          f"a cancelled record is never reported as resumed (got {r['status']!r})")


def test_resume_branch_holds_the_store_lock_for_its_budget_check():
    """The resume branch's own budget check must run under the store lock,
    asserted cross-process — a nested in-process call passes by design."""
    import json as _json
    import subprocess
    import textwrap

    d = _store()
    entry = dpp._entry("react_baseline")
    dpp.seed_defaults(d, OWNER, ["!r0:ag2.space"])
    store = op.SubscriptionStore(d)
    gen = dpp._entry_state(dpp.load_pack_state(d), entry["key"])["generation"]
    rec, errs = op.validate_draft(dpp._draft_for(entry, "!cc:ag2.space", OWNER, gen))
    check(not errs, "setup: resume draft validates")
    rec["pack"] = {"entry": entry["key"], "generation": gen}
    store.save(rec)

    child_src = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {os.path.dirname(op.__file__)!r})
        import observe_policy as op
        try:
            with op.store_lock({d!r}, timeout=1.0):
                print(json.dumps({{"r": "ACQUIRED"}}))
        except op.StoreLockUnavailable:
            print(json.dumps({{"r": "REFUSED"}}))
    """)

    def probe():
        r = subprocess.run([sys.executable, "-c", child_src],
                           capture_output=True, text=True, timeout=30)
        try:
            return _json.loads(r.stdout.strip())["r"]
        except Exception:
            return f"ERR:{r.stderr.strip()[:80]}"

    check(probe() == "ACQUIRED",
          "control: with the lock free, another process ACQUIRES (the probe can say yes)")

    seen = {}
    orig = dpp._budget_allows

    def probing(store_, draft):
        seen.setdefault("r", probe())      # fires INSIDE the resume critical section
        return orig(store_, draft)

    dpp._budget_allows = probing
    try:
        dpp.seed_room(op.SubscriptionStore(d), d, entry, "!cc:ag2.space",
                      owner_mxid=OWNER, owner_rooms=["!r0:ag2.space", "!cc:ag2.space"])
    finally:
        dpp._budget_allows = orig

    check(seen.get("r") == "REFUSED",
          f"the resume branch's budget check runs under the store lock "
          f"(got {seen.get('r')!r})")


def test_owner_cancel_racing_a_resume_cannot_resurrect_the_record():
    """A direct cancellation landing after the resume's pre-lock read must not
    be resurrected by its blind save; the record is re-read inside the lock."""
    d = _store()
    entry = dpp._entry("react_baseline")
    dpp.seed_defaults(d, OWNER, ["!r0:ag2.space"])
    store = op.SubscriptionStore(d)
    gen = dpp._entry_state(dpp.load_pack_state(d), entry["key"])["generation"]
    room = "!crash:ag2.space"
    pid = dpp._policy_id_for(entry["key"], gen, room)

    rec, errs = op.validate_draft(dpp._draft_for(entry, room, OWNER, gen))
    check(not errs, "setup: crash draft validates")
    rec["pack"] = {"entry": entry["key"], "generation": gen}
    store.save(rec)
    check(store.get(pid)["status"] == "draft", "precondition: record is a draft")

    real_lock = op.store_lock
    fired = []

    def racing_lock(store_dir, **kw):
        # the owner cancels AFTER the pre-lock read, BEFORE the lock is taken
        if not fired:
            fired.append(1)
            op.SubscriptionStore(d).transition(pid, "cancelled", note="owner direct cancel")
        return real_lock(store_dir, **kw)

    op.store_lock = racing_lock
    try:
        r = dpp.seed_room(op.SubscriptionStore(d), d, entry, room,
                          owner_mxid=OWNER, owner_rooms=["!r0:ag2.space", room])
    finally:
        op.store_lock = real_lock

    check(bool(fired), "control: the cancel injection actually fired")
    check(op.SubscriptionStore(d).get(pid)["status"] == "cancelled"
          or r["status"] != "resumed",
          "control: the record really was cancelled before the lock")
    final = op.SubscriptionStore(d).get(pid)["status"]
    check(final != "active",
          f"a cancelled record is never resurrected by a resume (got {final!r})")
    check(r["status"] != "resumed",
          f"and the caller is not told 'resumed' (got {r['status']!r})")


def test_concurrent_seed_of_the_same_room_is_idempotent_not_a_downgrade():
    """Two writers racing one room both read None pre-lock; the loser must skip,
    not blind-save the same policy_id and downgrade the winner's active record."""
    d = _store()
    entry = dpp._entry("react_baseline")
    n = (dpp.PACK_AGGREGATE_EVALS_PER_DAY // op.DEFAULT_EVALS_PER_DAY) - 1   # 18/20
    rooms = [f"!r{i}:ag2.space" for i in range(n)]
    dpp.seed_defaults(d, OWNER, rooms)
    gen = dpp._entry_state(dpp.load_pack_state(d), entry["key"])["generation"]
    room = "!final:ag2.space"
    pid = dpp._policy_id_for(entry["key"], gen, room)
    before = dpp.committed_evals_per_day(op.SubscriptionStore(d))
    check(before == dpp.PACK_AGGREGATE_EVALS_PER_DAY - op.DEFAULT_EVALS_PER_DAY,
          f"control: exactly one budget slot is left (got {before})")

    real_lock = op.store_lock
    fired, res = [], {}

    def racing_lock(store_dir, **kw):
        # writer B has already read existing=None; let writer A finish entirely
        if not fired:
            fired.append(1)
            res["A"] = dpp.seed_room(op.SubscriptionStore(d), d, entry, room,
                                     owner_mxid=OWNER, owner_rooms=rooms + [room])["status"]
        return real_lock(store_dir, **kw)

    op.store_lock = racing_lock
    try:
        res["B"] = dpp.seed_room(op.SubscriptionStore(d), d, entry, room,
                                 owner_mxid=OWNER, owner_rooms=rooms + [room])["status"]
    finally:
        op.store_lock = real_lock

    check(bool(fired), "control: the concurrent writer actually ran")
    check(res.get("A") == "seeded", f"control: writer A did seed (got {res.get('A')!r})")

    rec = op.SubscriptionStore(d).get(pid) or {}
    check(rec.get("status") == "active",
          f"the first writer's ACTIVE record survives the second writer "
          f"(got {rec.get('status')!r})")
    check(not (rec.get("pack") or {}).get("over_budget"),
          "and it is not rewritten as an over_budget draft")
    check(res.get("B") != "seeded",
          f"the second writer does not also claim seeded (got {res.get('B')!r})")


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
