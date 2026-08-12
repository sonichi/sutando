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


def test_policy_id_traversal_locked():
    # Review P1: policy_id is LLM-adjacent AND a filename component —
    # `../core-status` passed validation and escaped state/observe/.
    d = tempfile.mkdtemp()
    store = op.SubscriptionStore(d)
    rec, errs = op.validate_draft(_draft(policy_id="../core-status"))
    check(errs == [] and op._POLICY_ID_RE.match(rec["policy_id"]) is not None,
          "validator regenerates any id outside the obs_<hex> shape")
    rec2, _ = op.validate_draft(_draft(policy_id=rec["policy_id"]))
    check(rec2["policy_id"] == rec["policy_id"],
          "validator round-trips a well-shaped id (edit flow unaffected)")
    try:
        store.save({"policy_id": "../core-status", "status": "draft"})
        check(False, "save with traversal id must raise, not write")
    except ValueError:
        check(True, "save with traversal id raises (never touches disk)")
    check(store.get("../../etc/passwd") is None,
          "get with traversal id resolves to nothing")
    check(store.transition("../core-status", "active") is False,
          "transition with traversal id transitions nothing")
    check(not os.path.exists(os.path.join(os.path.dirname(d), "core-status.json")),
          "nothing escaped the store directory")


def test_cost_cap_non_dict_is_validation_error():
    # Review P1: cost_cap "cheap" crashed validate_draft (.get on a str) —
    # malformed LLM output must come back as a validation ERROR, normalized
    # to the default cap, never an exception.
    rec, errs = op.validate_draft(_draft(cost_cap="cheap"))
    check(any("cost_cap" in e for e in errs),
          "non-dict cost_cap → validation error (no AttributeError)")
    check(rec["cost_cap"] == {"evals_per_day": op.DEFAULT_EVALS_PER_DAY},
          "non-dict cost_cap normalizes to the default cap")
    rec2, errs2 = op.validate_draft(_draft(cost_cap=[3]))
    check(any("cost_cap" in e for e in errs2)
          and rec2["cost_cap"] == {"evals_per_day": op.DEFAULT_EVALS_PER_DAY},
          "list cost_cap handled the same way")
    # Review follow-up: `or {}` silently blessed FALSY non-dicts — cost_cap=[]
    # produced no errors and standing approval then auto-activated the policy.
    # Present-but-malformed must error (forcing the explicit card); absent/None
    # stay error-free defaults.
    for falsy in ([], "", 0, False):
        _, e = op.validate_draft(_draft(cost_cap=falsy))
        check(any("cost_cap" in x for x in e),
              f"falsy non-dict cost_cap {falsy!r} → validation error, not silent default")
    _, e_absent = op.validate_draft(_draft())
    _, e_none = op.validate_draft(_draft(cost_cap=None))
    check(e_absent == [] and e_none == [],
          "absent/None cost_cap stays a clean default (no error)")


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


def test_store_lock_serializes_a_SEPARATE_PROCESS():
    """The lock must exclude another PROCESS — a nested in-process call passes by
    design (re-entrancy), so the property is asserted across a real fork."""
    import json as _json
    import subprocess
    import textwrap
    d = tempfile.mkdtemp()
    child_src = textwrap.dedent(f"""
        import sys, json, time
        sys.path.insert(0, {os.path.dirname(op.__file__)!r})
        import observe_policy as op
        t0 = time.monotonic()
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
          "A: with the lock free, another process ACQUIRES it (control — the probe can say yes)")

    with op.store_lock(d):
        check(probe() == "REFUSED",
              "B: while this process holds the lock, another process is REFUSED")

    # C: the shipped mutating path must hold it, not just an explicit `with`.
    store = op.SubscriptionStore(d)
    rec = {"policy_id": "obs_" + "a" * 16, "status": "draft",
           "room_id": "!r:ag2.space", "mode": sorted(op.STANDING_MODES)[0],
           "cost_cap": {"evals_per_day": 1}}
    store.save(rec)
    seen = {}
    orig_save = op.SubscriptionStore.save

    def probing_save(self, r):
        seen.setdefault("r", probe())
        return orig_save(self, r)

    op.SubscriptionStore.save = probing_save
    try:
        store.transition(rec["policy_id"], "cancelled", note="probe")
    finally:
        op.SubscriptionStore.save = orig_save
    check(seen.get("r") == "REFUSED",
          f"C: transition() itself holds the lock against another process (got {seen.get('r')!r})")


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
