#!/usr/bin/env python3
"""Tests for piperoom-command.py pure logic (parse_command / apply_command / render / presets).
Network paths (prep_get/prep_put/context/message) are not exercised here."""
import importlib.util
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("prc", os.path.join(_here, "piperoom-command.py"))
prc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prc)

_fails = []


def check(cond, label):
    print(("  ok  " if cond else "  FAIL ") + label)
    if not cond:
        _fails.append(label)


def main():
    p = prc.parse_command
    # ── parse: add (generic item nouns) ──────────────────────────────────────
    a = p("add deal Acme Corp $25000 jane@acme.com")
    check(a and a["action"] == "add" and a["name"] == "Acme Corp" and a["value"] == 25000
          and a["contact"] == "jane@acme.com", "parse: add deal w/ $value + email")
    check(p("add candidate Jane Doe")["name"] == "Jane Doe", "parse: 'add candidate' (hiring noun)")
    check(p("add investor Sequoia")["name"] == "Sequoia", "parse: 'add investor' (fundraising noun)")
    check(p("add target BigCo")["name"] == "BigCo", "parse: 'add target' (gtm noun)")
    check(p("add deal Globex $12k")["value"] == 12000, "parse: '$12k' → 12000")
    check(p("add deal Foo")["value"] == 0, "parse: no value → 0")
    # ── parse: move / win / lose / note / show ───────────────────────────────
    m = p("move Acme to Negotiation")
    check(m["action"] == "move" and m["deal"] == "Acme" and m["stage"] == "Negotiation", "parse: move X to Y")
    check(p("win Acme")["action"] == "win" and p("win Acme")["deal"] == "Acme", "parse: win")
    check(p("lose Globex")["action"] == "lose", "parse: lose")
    n = p("note Acme: sent proposal")
    check(n["action"] == "note" and n["deal"] == "Acme" and n["text"] == "sent proposal", "parse: note X: text")
    check(p("show pipeline")["action"] == "show", "parse: show pipeline")
    check(p("pipeline")["action"] == "show", "parse: bare 'pipeline'")
    check(p("@sutando-qingyun-001:ag2.space show")["action"] == "show", "parse: leading mention tolerated")
    check(p("hello") is None, "parse: non-command → None")

    # ── presets / new_pipeline ───────────────────────────────────────────────
    for kind in ("sales", "hiring", "fundraising", "gtm"):
        pl = prc.new_pipeline(kind)
        check(pl["kind"] == kind and pl["deals"] == [] and len(pl["stages"]) >= 5,
              f"preset: new_pipeline('{kind}') has stages + empty items")
    check(prc.new_pipeline("hiring")["won_stage"] == "Hired", "preset: hiring won_stage = Hired")
    check(prc.new_pipeline("fundraising")["item_noun"] == "investor", "preset: fundraising item = investor")

    # ── apply_command (mutation) ─────────────────────────────────────────────
    pl = prc.new_pipeline("sales")
    r = prc.apply_command(pl, p("add deal Acme $25000 jane@acme.com"))
    check(len(pl["deals"]) == 1 and pl["deals"][0]["stage"] == "Lead In" and "added" in r,
          "apply: add → item in first stage")
    r = prc.apply_command(pl, p("move Acme to Demo Scheduled"))
    check(pl["deals"][0]["stage"] == "Demo Scheduled" and "moved" in r, "apply: move (fuzzy name+stage)")
    r = prc.apply_command(pl, p("note Acme: booked Thu"))
    check("booked Thu" in pl["deals"][0]["notes"], "apply: note appends")
    r = prc.apply_command(pl, p("win Acme"))
    check(pl["deals"][0]["stage"] == "Won", "apply: win → Won")
    r = prc.apply_command(pl, p("move Ghost to Won"))
    check("no deal matches" in r and len(pl["deals"]) == 1, "apply: unknown deal → legible error, no mutation")
    r = prc.apply_command(pl, p("add deal Beta"))
    r = prc.apply_command(pl, p("move Beta to Nowhere"))
    check("no stage matches" in r, "apply: unknown stage → legible error")
    # hiring terminal labels
    ph = prc.new_pipeline("hiring")
    prc.apply_command(ph, p("add candidate Jane"))
    r = prc.apply_command(ph, p("win Jane"))
    check(ph["deals"][0]["stage"] == "Hired", "apply: hiring 'win' → Hired (preset terminal)")
    r = prc.apply_command(ph, p("lose Jane"))
    check(ph["deals"][0]["stage"] == "Rejected", "apply: hiring 'lose' → Rejected")

    # ── owner slash commands: /new /update /enrich /close ────────────────────
    check(p("/new Acme $25000 jane@acme.com")["action"] == "add"
          and p("/new Acme $25000 jane@acme.com")["name"] == "Acme"
          and p("/new Acme $25000 jane@acme.com")["value"] == 25000, "parse: /new")
    check(p("/update Acme: sent contract")["action"] == "update", "parse: /update X: text")
    e = p("/enrich Acme")
    check(e["action"] == "enrich" and e["deal"] == "Acme", "parse: /enrich X")
    cl = p("/close Acme")
    check(cl["action"] == "close" and cl["outcome"] == "won", "parse: /close default → won")
    check(p("/close Acme as lost")["outcome"] == "lost", "parse: /close X as lost")
    pl2 = prc.new_pipeline("sales")
    prc.apply_command(pl2, p("/new Beta $5000"))
    check(pl2["deals"][0]["name"] == "Beta" and pl2["deals"][0]["value"] == 5000, "apply: /new adds entry")
    r = prc.apply_command(pl2, p("/update Beta: called them"))
    check("updated" in r and "called them" in pl2["deals"][0]["notes"]
          and "2026-07-12:" in pl2["deals"][0]["notes"], "apply: /update appends dated note")
    r = prc.apply_command(pl2, p("/enrich Beta"))
    check(pl2["deals"][0].get("needs_enrich") is True and "enrichment queued" in r, "apply: /enrich flags for agent")
    r = prc.apply_command(pl2, p("/close Beta"))
    check(pl2["deals"][0]["stage"] == "Won", "apply: /close → Won")
    r = prc.apply_command(pl2, p("/close Beta as lost"))
    check(pl2["deals"][0]["stage"] == "Lost", "apply: /close as lost → Lost")
    # fuzzy name select (owner: "select a name very easily")
    r = prc.apply_command(pl2, p("/update bet: quick note"))
    check("updated" in r, "apply: fuzzy name match ('bet' → 'Beta')")

    # ── render ───────────────────────────────────────────────────────────────
    out = prc.render(pl)
    check("Sales Pipeline" in out and "Won" in out and "Beta" in out, "render: shows name + non-empty stages")
    check("Lost" not in out, "render: hides empty terminal stage (Lost)")

    # ── agent-behavior primitive: items_needing_attention (detect-stale) ──────
    check(prc._days_between("2026-07-01", "2026-07-12") == 11, "days_between: 11 days")
    check(prc._days_between("bad", "2026-07-12") is None, "days_between: unparseable → None")
    pl3 = prc.new_pipeline("gtm")
    pl3["deals"] = [
        {"id": "d1", "name": "FreshFull", "stage": "Contacted", "contact": "a@x.com",
         "notes": "researched", "updated": "2026-07-12"},                       # clean → no attention
        {"id": "d2", "name": "Enrichme", "stage": "Identified", "contact": "b@x.com",
         "notes": "n", "updated": "2026-07-12", "needs_enrich": True},           # needs_enrich
        {"id": "d3", "name": "Stalebob", "stage": "Engaged", "contact": "c@x.com",
         "notes": "old", "updated": "2026-06-01"},                               # stale
        {"id": "d4", "name": "Thinny", "stage": "Identified", "contact": "",
         "notes": "", "updated": "2026-07-12"},                                  # missing_contact + thin
        {"id": "d5", "name": "ClosedWon", "stage": "Advocate", "contact": "",
         "notes": "", "updated": "2026-01-01"},                                  # terminal → excluded
    ]
    items = prc.items_needing_attention(pl3, today="2026-07-12", stale_days=7)
    ids = [it["id"] for it in items]
    check("d1" not in ids, "attention: clean+fresh entry excluded")
    check("d5" not in ids, "attention: terminal-stage (Advocate/won) entry excluded")
    check(set(ids) == {"d2", "d3", "d4"}, "attention: flags exactly the 3 needy entries")
    check(items[0]["id"] == "d2", "attention: needs_enrich sorts first (most urgent)")
    d3 = next(it for it in items if it["id"] == "d3")
    check(any(r.startswith("stale_") for r in d3["reasons"]), "attention: stale entry tagged stale_<n>d")
    d4 = next(it for it in items if it["id"] == "d4")
    check("missing_contact" in d4["reasons"] and "thin_no_notes" in d4["reasons"],
          "attention: thin entry tagged missing_contact + thin_no_notes")
    # track_contact:false (competitor-style pipeline) → missing_contact suppressed
    pl4 = prc.new_pipeline("gtm"); pl4["track_contact"] = False
    pl4["deals"] = [{"id": "c1", "name": "Rival", "stage": "Identified", "contact": "",
                     "notes": "researched", "updated": "2026-07-12"}]
    check(prc.items_needing_attention(pl4, today="2026-07-12") == [],
          "attention: track_contact=false suppresses missing_contact (no noise on competitor rooms)")
    pl4["deals"][0]["needs_enrich"] = True
    check(prc.items_needing_attention(pl4, today="2026-07-12")[0]["reasons"] == ["needs_enrich"],
          "attention: track_contact=false still flags real reasons (needs_enrich)")
    check("nothing needs attention" in prc.render_attention([], "P"), "render_attention: empty → all-clear")
    check("3 entries need attention" in prc.render_attention(items, "GTM"), "render_attention: counts entries")
    check("research & fill" in prc.render_attention(items, "GTM"), "render_attention: shows recommended action (needs_enrich→research & fill)")

    print("\n" + ("PASS — all checks green" if not _fails else f"FAIL — {len(_fails)} failing"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
