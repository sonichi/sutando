#!/usr/bin/env python3
"""Tests for skills/observe/observe_policy.py — the /observe deterministic core.
Covers: draft validation discipline, standing-approval scope lock (fail-closed
on every boundary), store transitions + terminal immutability, effect-card
rendering (confirm vs auto-activated visibility, A2UI real-contract opt-in).
Exit 0/1."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "observe"))
import observe_policy as op  # noqa: E402

FAILS: list = []
OWNER = "@qingyun:ag2.space"
ROOMS = ["!master:ag2.space", "!dev:ag2.space"]


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _draft(**over):
    d = {"room_id": "!master:ag2.space", "event_types": ["artifact.updated"],
         "mode": "observe", "created_by": OWNER,
         "source_text": "watch doc changes here"}
    d.update(over)
    return d


def test_validate_good_draft():
    rec, errs = op.validate_draft(_draft())
    check(errs == [], "valid draft has no errors")
    check(rec["policy_id"].startswith("obs_") and rec["status"] == "draft",
          "normalized draft gets obs_ id + draft status")
    check(rec["cost_cap"] == {"evals_per_day": op.DEFAULT_EVALS_PER_DAY},
          "cost cap defaults to the default cap")


def test_validate_error_branches():
    cases = [
        (_draft(room_id="master"), "room_id"),
        (_draft(event_types=[]), "event_types"),
        (_draft(event_types=["OK NOT"]), "malformed"),
        (_draft(mode="explode"), "mode"),
        (_draft(cost_cap={"evals_per_day": -1}), "evals_per_day"),
        (_draft(created_by="qingyun"), "mxid"),
    ]
    for bad, needle in cases:
        _, errs = op.validate_draft(bad)
        check(any(needle in e for e in errs), f"rejects: {needle}")
    # long source_text is truncated, not rejected
    rec, errs = op.validate_draft(_draft(source_text="x" * 999))
    check(errs == [] and len(rec["source_text"]) == 400,
          "source_text truncates at 400 (LLM output discipline)")


def test_standing_approval_scope_lock():
    ok_rec, _ = op.validate_draft(_draft())
    ok, why = op.evaluate_standing_approval(ok_rec, owner_mxid=OWNER, owner_rooms=ROOMS)
    check(ok is True and "standing approval" in why,
          "in-scope draft auto-activates (self + scoped room + notify-only + default cap)")
    denials = [
        (op.validate_draft(_draft(created_by="@mallory:ag2.space"))[0],
         {"owner_mxid": OWNER, "owner_rooms": ROOMS}, "not the owner"),
        (op.validate_draft(_draft(room_id="!other:ag2.space"))[0],
         {"owner_mxid": OWNER, "owner_rooms": ROOMS}, "scoped rooms"),
        (op.validate_draft(_draft(mode="taskify"))[0],
         {"owner_mxid": OWNER, "owner_rooms": ROOMS}, "explicit confirmation"),
        (op.validate_draft(_draft(cost_cap={"evals_per_day": 99}))[0],
         {"owner_mxid": OWNER, "owner_rooms": ROOMS}, "cost cap"),
        (op.validate_draft(_draft())[0],
         {"owner_mxid": "", "owner_rooms": ROOMS}, "not the owner"),
    ]
    for rec, kw, needle in denials:
        ok, why = op.evaluate_standing_approval(rec, **kw)
        check(ok is False and needle in why,
              f"FAIL-CLOSED outside scope: {needle}")


def test_standing_approval_self_contained_boundary():
    # 001 review: the evaluator must deny — not crash — on an UNVALIDATED
    # draft (missing/malformed cost_cap). The boundary assumes nothing.
    raw = {"created_by": OWNER, "room_id": ROOMS[0], "mode": "observe"}
    ok, why = op.evaluate_standing_approval(raw, owner_mxid=OWNER, owner_rooms=ROOMS)
    check(ok is False and "cost cap" in why,
          "missing cost_cap → DENY (self-contained boundary, no KeyError)")
    raw["cost_cap"] = {"evals_per_day": "two"}
    ok, _ = op.evaluate_standing_approval(raw, owner_mxid=OWNER, owner_rooms=ROOMS)
    check(ok is False, "malformed cap type → DENY")


def test_store_transitions_and_immutability():
    d = tempfile.mkdtemp()
    store = op.SubscriptionStore(d)
    rec, _ = op.validate_draft(_draft())
    store.save(rec)
    pid = rec["policy_id"]
    check(store.get(pid)["status"] == "draft", "save + get round-trips")
    check(store.transition(pid, "active") is True, "draft -> active allowed")
    check(store.transition(pid, "active") is False,
          "active -> active refused (no self-transition)")
    check(store.transition(pid, "cancelled", note="owner said stop") is True,
          "active -> cancelled allowed")
    check(store.transition(pid, "active") is False,
          "TERMINAL IMMUTABILITY — cancelled never reactivates")
    got = store.get(pid)
    check([a["to"] for a in got["audit"]] == ["active", "cancelled"]
          and got["audit"][-1]["note"] == "owner said stop",
          "every transition audited with notes")
    check(store.transition("obs_missing", "active") is False,
          "unknown policy id transitions to nothing")
    rec2, _ = op.validate_draft(_draft(room_id="!dev:ag2.space"))
    store.save(rec2)
    store.transition(rec2["policy_id"], "active")
    check([r["policy_id"] for r in store.list(status="active")] == [rec2["policy_id"]],
          "list filters by status")
    check(len(store.list()) == 2, "list without filter returns all")
    check(op.SubscriptionStore(tempfile.mkdtemp() + "/absent").list() == [],
          "empty/absent store lists nothing")


def test_render_card_confirm_and_auto():
    rec, _ = op.validate_draft(_draft())
    confirm = op.render_card(rec)
    check("Confirm this observation policy" in confirm
          and f"policy {rec['policy_id']} activate" in confirm,
          "confirm card carries the decision grammar")
    check("≤ 2 evals/day (default cap)" in confirm,
          "cost line shows the CAP, not usage (resolved decision)")
    check("```a2ui" not in confirm, "a2ui block is opt-in (default off)")
    auto = op.render_card(rec, auto_activated=True)
    check("auto-activated per your standing approval" in auto
          and "cancel` to undo" in auto,
          "VISIBILITY — auto-activation always announces itself + offers undo")
    check("Confirm" not in auto, "auto card is an announcement, not a question")


def test_render_card_a2ui_real_contract():
    rec, _ = op.validate_draft(_draft())
    card_text = op.render_card(rec, include_a2ui=True)
    payload = json.loads(card_text.split("```a2ui")[1].split("```")[0])
    comp = payload["components"][0]
    check(comp["type"] == "choice-group"
          and [c["value"] for c in comp["choices"]] == ["activate", "edit", "cancel"],
          "a2ui card uses the REAL renderer contract (choice-group components)")
    check(all(c["action"] == {"event": "space.ag2.ha.answer", "field": "choice"}
              for c in comp["choices"]),
          "choices carry the agreed custom-event convention (space.ag2.ha.answer)")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"# {name}")
            fn()
    if FAILS:
        print(f"\nFAILED ({len(FAILS)})")
        return 1
    print("\nPASS — observe policy core")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
