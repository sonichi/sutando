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


def test_over_budget_room_leaves_an_APPROVABLE_record():
    """john-the-dev's follow-up [P1] on #2320. The refusal copy says explicit
    approval is required; that sentence must be backed by a record. Before the
    fix, seed_room() returned before store.save(), so the deterministic policy_id
    did not exist, transition(pid,'active') had nothing to act on, and the result
    carried none of the fields a confirmation card needs. Verified on 08cedd9c:
    11 rooms -> 10 seeded, 1 refused, store.get(refused) is None. Rooms past the
    budget were then neither auto-subscribed NOR owner-actionable — the silent
    drop the budget exists to prevent."""
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
    # The grammar the reviewer showed was broken must now work end to end.
    check(store.transition(refused[0]["policy_id"], "active", note="owner approved") is True,
          "transition(pid,'active') now has a record to act on")
    # A draft must not consume budget, or the refusal would shrink the allowance
    # every time it fired and the pack would starve itself.
    d2 = _store()
    dpp.seed_defaults(d2, OWNER, rooms)
    check(_aggregate(d2) <= dpp.PACK_AGGREGATE_EVALS_PER_DAY,
          f"a persisted draft consumes no budget ({_aggregate(d2)})")


def test_a_persisted_draft_does_not_self_activate_while_over_budget():
    """CONTROL for the fix above. seed_room() treats an existing draft as a
    crash-interrupted seed and RESUMES it, so persisting one over budget could
    have created a back door that activates on the next reconnect. The resume
    path re-runs the same budget check, so it must stay refused."""
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
    """CALIBRATION. The control above is satisfied by a draft that can NEVER
    activate, which would make the 'awaiting approval' state a dead end. Cancel
    one active room and the queued draft must take the freed allowance on the
    next seed — self-healing rather than requiring an owner re-seed dance."""
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
    """john-the-dev's follow-up [P1] on #2320, created BY the previous fix.

    set_enabled(False) cancelled only ACTIVE records. The over-budget draft is
    not active, so it survived the disable — and `draft -> active` is a legal
    transition, so an approval card minted before the disable still activated a
    room afterwards. Reproduced on adbd1b56: after_disable=draft,
    activate_stale=True, final_status=active, while the entry read enabled=False.
    The owner's disable was advisory, which is the one thing a disable must not be.
    """
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
    # The real assertion is the consequence, not the status: a late click on a
    # card minted before the disable must not resurrect the room. `cancelled` is
    # terminal in the store, so the guard lives in the state machine rather than
    # in a caller that could forget to ask.
    check(store.transition(pid, "active", note="stale card clicked after disable") is False,
          "a stale approval CANNOT activate a room after the owner disabled the entry")
    check(store.get(pid)["status"] == "cancelled", "...and the record stays cancelled")


def test_disable_then_reenable_still_seeds_a_fresh_generation():
    """CALIBRATION. The guard above is satisfied by a disable that destroys the
    entry permanently, which would be a worse bug. Re-enabling must still seed."""
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
    """john-the-dev's TOCTOU follow-up on #2320, in BOTH variants.

    The sweep added by the previous fix read the draft list, then cancelled, then
    committed disabled=True. A seed already in flight could persist a record into
    that gap: absent from the sweep, and not yet gated by the flag.

    Reproduced on 4d50448b two ways from one root cause:
      over budget  -> persisted as `draft`, survived, and a late approval click
                      ACTIVATED it (activate_late True) on a disabled entry;
      under budget -> the sweep had already freed allowance, so the racing seed
                      went straight to `active` and needed no click at all.
    The second is worse and ordering alone cannot catch it, which is why the fix
    is ordering (commit the flag first) PLUS revalidation at every persist point.
    """
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
    """The ordering half, asserted directly rather than inferred from the race.

    If the flag were still written last, a reader observing mid-sweep would see
    the entry as ENABLED while its records were already being cancelled.
    """
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
    """Found by enumerating the lifecycles rather than waiting for a review.

    committed_evals_per_day()'s own docstring says approving something must
    never make the next automatic grant harder. But an over-budget draft kept
    plain pack provenance, so approving it counted against the pack's budget.
    Measured on df4b7b3b: approving 3 queued rooms took the aggregate to 26/20
    and then refused EVERY subsequent auto-seed — permanently, even after a room
    was cancelled. The pack could never auto-seed again.
    """
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
    """CALIBRATION. The guard above is satisfied by removing the budget entirely,
    which would undo the original blocker. The cap must still bind auto-seeds."""
    d = _store()
    rooms = [f"!r{i}:ag2.space" for i in range(13)]
    res = dpp.seed_defaults(d, OWNER, rooms)
    seeded = [r for r in res if r["status"] == "seeded"]
    check(len(seeded) * op.DEFAULT_EVALS_PER_DAY <= dpp.PACK_AGGREGATE_EVALS_PER_DAY,
          f"automatic seeding is still capped at the budget ({len(seeded)} auto-seeds)")
    check(len(seeded) < len(rooms),
          "...and rooms beyond it are still refused, not silently granted")


def test_list_pack_shows_rooms_awaiting_the_owners_approval():
    """Continued lifecycle enumeration — the `list_pack` (owner-view) cell.

    The over-budget refusal says the room "surfaces as an explicit card the owner
    can approve". The one view built for her listed only `active_rooms`, so the
    rooms actually awaiting her decision were invisible in it. Same defect
    already fixed once on this PR at the RECORD layer (a promise of approval with
    nothing approvable behind it), reappearing at the VIEW layer: a decision she
    cannot see is not a decision she has.
    """
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
