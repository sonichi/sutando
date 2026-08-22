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

    # unconsumed_manifests: a manifest with no registered room is a surface nothing
    # exercises, so a regression in it would surface to nobody.
    import tempfile
    with tempfile.TemporaryDirectory() as md:
        for t in ("piperoom", "inboxroom"):
            open(os.path.join(md, f"{t}.json"), "w").write("{}")
        rooms = [{"room": "!a", "type": "inboxroom"}]
        check(bl.unconsumed_manifests(rooms, md) == ["piperoom"],
              "unconsumed: names the manifest with no registered room")
        both = [{"room": "!a", "type": "inboxroom"}, {"room": "!b", "type": "piperoom"}]
        check(bl.unconsumed_manifests(both, md) == [],
              "unconsumed: silent when every manifest has a room")
        # Control: without this, returning [] unconditionally passes the line above.
        check(bl.unconsumed_manifests([], md) == ["inboxroom", "piperoom"],
              "unconsumed: empty registry names every manifest, sorted")
        check(bl.unconsumed_manifests(rooms, os.path.join(md, "nope")) == [],
              "unconsumed: unreadable manifest dir degrades to empty, never raises")
        # A registry entry with no 'type' key must not crash the set difference.
        check(bl.unconsumed_manifests([{"room": "!a"}], md) == ["inboxroom", "piperoom"],
              "unconsumed: type-less registry row does not raise")

    # TypeEngine must resolve credentials through the TYPE'S OWN module. It used to
    # hardcode skills/piperoom/piperoom-command.py, which made every type load piperoom.
    class _Stub:
        def __init__(self, tok): self._tok = tok
        def resolve_token(self, repo): return ("u-" + self._tok, "s-" + self._tok)
    loaded = []
    def _fake_loader(rel, name):
        loaded.append(rel)
        return _Stub("mine")
    orig = bl._load_module
    try:
        bl._load_module = _fake_loader
        man = {"type": "caseroom", "analyze": {"module": "skills/caseroom/x.py",
                                               "function": "resolve_token"}}
        eng = bl.TypeEngine(man)
        check(eng.url == "u-mine" and eng.secret == "s-mine",
              "engine: credentials come from the type's own module")
        check(loaded == ["skills/caseroom/x.py"],
              f"engine: loads ONLY the manifest-named module, got {loaded}")
        check(not any("piperoom" in r for r in loaded),
              "engine: never loads a concrete skill the manifest did not name")
        # A module missing resolve_token must fail LOUDLY, not silently fall back.
        class _NoTok:
            def analyze(self, d): return []
        bl._load_module = lambda rel, name: _NoTok()
        try:
            bl.TypeEngine({"type": "caseroom",
                           "analyze": {"module": "skills/caseroom/x.py", "function": "analyze"}})
            check(False, "engine: missing resolve_token must raise")
        except TypeError as ex:
            check("caseroom" in str(ex) and "resolve_token" in str(ex),
                  "engine: the error names the manifest and the missing helper")
    finally:
        bl._load_module = orig

    # END-TO-END: a room type the repo has never heard of must work as a DECLARATION
    # alone. This pins Track 13's central claim; the assertions above pin one seam.
    import tempfile
    _MOD = '''
def resolve_token(repo):
    return ("http://local.invalid", "tok")
def load_pipeline(url, secret, room):
    return {"cases": [{"name": "Acme", "severity": "high", "updated": "2026-08-01"},
                      {"name": "Refund", "severity": "low", "updated": "2026-08-19"}]}
def save_pipeline(url, secret, room, data):
    return True
def cases_needing_attention(data, today="2026-08-20", stale_days=3):
    out = []
    for c in data.get("cases", []):
        if c.get("severity") == "high":
            out.append({"name": c["name"], "reasons": ["high-severity"]})
    return out
'''
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "mod"))
        open(os.path.join(td, "mod", "case.py"), "w").write(_MOD)
        os.makedirs(os.path.join(td, "manifests"))
        json.dump({"type": "caseroom", "version": 1, "entity": "Case",
                   "data_object": {"folder": "caseroom", "filename": "cases.json"},
                   "analyze": {"module": "mod/case.py", "function": "cases_needing_attention"},
                   "autonomy_default": "safe", "cooldown_s": 1,
                   "tiers": {"safe": {"reasons": [], "actions": []},
                             "advance": {"reasons": ["high-severity"],
                                         "verbs": {"high-severity": "`escalate {name}`"}}}},
                  open(os.path.join(td, "manifests", "caseroom.json"), "w"))
        json.dump([{"room": "!c:ag2.space", "type": "caseroom"}],
                  open(os.path.join(td, "registry.json"), "w"))
        json.dump({}, open(os.path.join(td, "state.json"), "w"))
        _orig_repo = bl._REPO
        try:
            bl._REPO = td  # manifest module paths resolve against the repo root
            rep = dict(bl.run(os.path.join(td, "registry.json"),
                              os.path.join(td, "state.json"),
                              dry_run=True, force=True,
                              manifest_dir=os.path.join(td, "manifests")))
        finally:
            bl._REPO = _orig_repo
        body = rep.get("!c:ag2.space", "")
        check("escalate Acme" in body,
              f"e2e: a NEW type works from a manifest alone — got {body[:90]!r}")
        # Guard the guard: a bare `"Refund" not in body` passes VACUOUSLY when the
        # type fails to load at all, so require the proposal to be present too.
        check("escalate Acme" in body and "Refund" not in body,
              "e2e: the new type's own tier config filters, not a shipped default")

    # The autonomy override lives in the ROOM's data object, which room members can
    # write. Pin the direction: a room may only ever REDUCE its own automation.
    for room_val, dflt, want_auto in [("propose", "safe", False), ("safe", "safe", True),
                                      (None, "safe", True), ("anything-else", "safe", False),
                                      ("safe", "propose", True)]:
        eff = room_val if room_val is not None else dflt
        auto, prop = bl.split_actions([{"name": "x", "reasons": ["r"]}], eff, ["r"])
        check(bool(auto) == want_auto,
              f"autonomy: room={room_val!r} default={dflt!r} -> auto={bool(auto)} (want {want_auto})")
    # The last case is the one that matters: a manifest default of "propose" does NOT
    # hold, because the room can name "safe" and re-enable auto-exec on itself.
    check(bool(bl.split_actions([{"name": "x", "reasons": ["r"]}], "safe", ["r"])[0]),
          "autonomy: a 'propose' manifest default is NOT a guard — the room overrides it")

    print("\n" + ("PASS — all checks green" if not _fails else f"FAIL — {len(_fails)} failing"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
