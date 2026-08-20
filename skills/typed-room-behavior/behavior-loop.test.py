#!/usr/bin/env python3
"""Tests for behavior-loop.py pure logic (manifest-driven split/verbs/cooldown) + manifest sanity."""
import importlib.util
import json
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("bl", os.path.join(_here, "behavior-loop.py"))
bl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bl)
_fails = []


def check(c, label):
    print(("  ok  " if c else "  FAIL ") + label)
    if not c:
        _fails.append(label)


def main():
    pipe = bl.load_manifest("piperoom")
    inbox = bl.load_manifest("inboxroom")
    check(pipe and pipe["type"] == "piperoom" and pipe["state_model"] == "linear-stages",
          "manifest: piperoom loads (linear-stages)")
    check(inbox and inbox["state_model"] == "drain-queue", "manifest: inboxroom loads (drain-queue)")
    check(bl.load_manifest("no-such-type") is None, "manifest: unknown type → None")
    for m in (pipe, inbox):
        check(all(k in m for k in ("type", "version", "entity", "state_model", "data_object", "analyze", "tiers")),
              f"manifest {m['type']}: required keys present")
        for a in m["tiers"]["safe"].get("actions", []):
            check(a.get("effect") == "additive", f"manifest {m['type']}: safe actions are additive-only")
    check(inbox["tiers"]["safe"]["actions"] == [], "manifest inboxroom: no safe auto-actions in v1")

    # ── split_actions (manifest safe-reason set) ─────────────────────────────
    SAFE = pipe["tiers"]["safe"]["reasons"]
    items = [
        {"id": "a", "reasons": ["needs_enrich"]},
        {"id": "b", "reasons": ["missing_contact", "thin_no_notes"]},
        {"id": "c", "reasons": ["stale_9d"]},
        {"id": "d", "reasons": ["needs_enrich", "stale_12d"]},
    ]
    auto, prop = bl.split_actions(items, "safe", SAFE)
    check([i["id"] for i in auto] == ["a", "b"], "split: safe-only items auto")
    check([i["id"] for i in prop] == ["c", "d"], "split: advance + mixed propose")
    auto, prop = bl.split_actions(items, "propose", SAFE)
    check(auto == [] and len(prop) == 4, "split: propose → nothing auto")
    auto, prop = bl.split_actions(items, "full", SAFE)
    check(auto == [] and len(prop) == 4, "split: 'full' does NOT auto in v1 (deliberate)")
    auto, prop = bl.split_actions([{"id": "x", "reasons": []}], "safe", SAFE)
    check(auto == [], "split: empty reasons never auto")

    # ── proposal_verb (manifest verb templates, room's own grammar) ──────────
    V = pipe["tiers"]["advance"]["verbs"]
    v = bl.proposal_verb({"name": "Acme", "reasons": ["stale_9d"]}, V)
    check("/update Acme" in v and "move Acme to" in v, "verbs: pipe stale_* prefix template")
    v = bl.proposal_verb({"name": "Acme", "reasons": ["missing_contact"]}, V)
    check("/enrich Acme" in v, "verbs: pipe default template")
    VI = inbox["tiers"]["advance"]["verbs"]
    check("triage Refund" in bl.proposal_verb({"title": "Refund", "reasons": ["unread"]}, VI),
          "verbs: inbox unread → triage")
    check("assign Refund to" in bl.proposal_verb({"title": "Refund", "reasons": ["unassigned"]}, VI),
          "verbs: inbox unassigned → assign")

    # ── linkedin_url_from_notes ──────────────────────────────────────────────
    n = "fit 10. Evidence: https://x.com/p/1 | profile https://www.linkedin.com/in/vukovicvl"
    check(bl.linkedin_url_from_notes(n) == "https://www.linkedin.com/in/vukovicvl", "extracts linkedin url")
    check(bl.linkedin_url_from_notes("none here") is None, "no url → None")

    # ── should_run_room cooldown (manifest cooldown_s) ───────────────────────
    cd = pipe["cooldown_s"]
    check(bl.should_run_room({}, "!r", now=1000, cooldown=cd) is True, "cooldown: never-run → run")
    check(bl.should_run_room({"!r": 1000}, "!r", now=1000 + cd - 1, cooldown=cd) is False, "cooldown: within → skip")
    check(bl.should_run_room({"!r": 1000}, "!r", now=1000 + cd, cooldown=cd) is True, "cooldown: past → run")
    # v2 state schema (dict with last_run) reads identically — the dedup
    # upgrade must not break cooldown arithmetic for migrated rooms.
    check(bl.should_run_room({"!r": {"last_run": 1000, "proposed_fps": []}}, "!r",
                             now=1000 + cd - 1, cooldown=cd) is False, "cooldown: v2 dict within → skip")
    check(bl.should_run_room({"!r": {"last_run": 1000, "proposed_fps": []}}, "!r",
                             now=1000 + cd, cooldown=cd) is True, "cooldown: v2 dict past → run")

    # ── proposal dedup (the re-post-every-cycle spam fix, 2026-07-20) ────────
    items = [{"name": "Acme", "reasons": ["missing_contact"]},
             {"name": "Globex", "reasons": ["stale"]}]
    # First pass: nothing stored → both are new.
    st = {}
    new1, fps1 = bl.filter_new_proposals(st, "!r", items, V)
    check(len(new1) == 2 and len(fps1) == 2, "dedup: first pass proposes both")
    st["!r"] = {"last_run": 1000, "proposed_fps": fps1}
    # Second pass, same analysis → zero new (the observed spam case).
    new2, fps2 = bl.filter_new_proposals(st, "!r", items, V)
    check(new2 == [] and fps2 == fps1, "dedup: unchanged analysis re-posts nothing")
    # One item resolves; stored fps REPLACED by current → if it reappears later,
    # it must re-propose (staleness pruning).
    st["!r"] = {"last_run": 1000, "proposed_fps": fps2}
    only_acme = [items[0]]
    new3, fps3 = bl.filter_new_proposals(st, "!r", only_acme, V)
    check(new3 == [] and len(fps3) == 1, "dedup: resolved item drops from stored fps")
    st["!r"] = {"last_run": 1000, "proposed_fps": fps3}
    new4, _fps4 = bl.filter_new_proposals(st, "!r", items, V)
    check(len(new4) == 1 and new4[0]["name"] == "Globex", "dedup: reappearing issue re-proposes")
    # Changed reasons on a known entity = a DIFFERENT ask → proposes.
    changed = [{"name": "Acme", "reasons": ["stale"]}]
    new5, _ = bl.filter_new_proposals(st, "!r", changed, V)
    check(len(new5) == 1, "dedup: same entity, new reasons → new proposal")
    # v1 bare-stamp state (pre-upgrade) → nothing stored, everything proposes.
    new6, _ = bl.filter_new_proposals({"!r": 1000}, "!r", items, V)
    check(len(new6) == 2, "dedup: v1 bare-stamp state treated as no prior proposals")

    print("\n" + ("PASS — all checks green" if not _fails else f"FAIL — {len(_fails)} failing"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
