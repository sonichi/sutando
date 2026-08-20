#!/usr/bin/env python3
"""Tests for inbox-command.py pure logic (parse_command / apply_command / render)."""
import importlib.util
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ic", os.path.join(_here, "inbox-command.py"))
ic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ic)
_fails = []


def check(c, label):
    print(("  ok  " if c else "  FAIL ") + label)
    if not c:
        _fails.append(label)


def q():
    return {"name": "Support Inbox", "items": []}


def main():
    p = ic.parse_command
    n = p("/new Refund request from Acme !high @alice")
    check(n["action"] == "new" and n["title"] == "Refund request from Acme"
          and n["priority"] == "high" and n["owner"] == "alice", "parse: new w/ !priority + @owner")
    check(p("new Password reset")["priority"] == "normal", "parse: default priority normal")
    check(p("triage Refund")["action"] == "triage", "parse: triage")
    a = p("assign Refund to bob")
    check(a["action"] == "assign" and a["who"] == "bob", "parse: assign X to Y")
    pr = p("prioritize Refund low")
    check(pr["action"] == "prioritize" and pr["priority"] == "low", "parse: prioritize X low")
    check(p("prioritize Refund")["priority"] == "high", "parse: prioritize default → high")
    check(p("resolve Refund")["action"] == "resolve", "parse: resolve")
    check(p("done Refund")["action"] == "resolve", "parse: 'done' → resolve")
    check(p("escalate Refund")["action"] == "escalate", "parse: escalate")
    check(p("note Refund: called back")["action"] == "note", "parse: note")
    check(p("show")["action"] == "show", "parse: show")
    check(p("queue")["action"] == "show", "parse: 'queue' → show")
    check(p("hi") is None, "parse: non-command → None")

    b = q()
    r = ic.apply_command(b, p("/new Refund from Acme !high @alice"))
    check(len(b["items"]) == 1 and b["items"][0]["status"] == "unread"
          and b["items"][0]["priority"] == "high" and b["items"][0]["owner"] == "alice", "apply: new → unread")
    r = ic.apply_command(b, p("triage Refund"))
    check(b["items"][0]["status"] == "triaged", "apply: triage → triaged")
    r = ic.apply_command(b, p("assign Refund to bob"))
    check(b["items"][0]["status"] == "assigned" and b["items"][0]["owner"] == "bob", "apply: assign → assigned + owner")
    r = ic.apply_command(b, p("prioritize Refund low"))
    check(b["items"][0]["priority"] == "low", "apply: prioritize low")
    r = ic.apply_command(b, p("escalate Refund"))
    check(b["items"][0]["priority"] == "high" and "escalated" in b["items"][0]["notes"], "apply: escalate → high + note")
    r = ic.apply_command(b, p("resolve Refund"))
    check(b["items"][0]["status"] == "done", "apply: resolve → done (drained)")
    r = ic.apply_command(b, p("triage Ghost"))
    check("no item matches" in r, "apply: unknown item → legible error")

    # render: high-priority sorts first within a state; empty Done hidden
    b2 = q()
    ic.apply_command(b2, p("new Low thing !low"))
    ic.apply_command(b2, p("new Urgent thing !high"))
    out = ic.render(b2)
    check(out.index("Urgent thing") < out.index("Low thing"), "render: high-priority first")
    check("Done" not in out, "render: empty Done hidden")
    check("2 open" in out, "render: open count in header")

    # ── agent-behavior primitive: items_needing_attention (drain-queue) ───────
    check(ic._days_between("2026-07-01", "2026-07-12") == 11, "days_between: 11 days")
    check(ic._days_between("x", "2026-07-12") is None, "days_between: bad → None")
    qq = {"name": "Support Inbox", "items": [
        {"id": "i1", "title": "Fresh triaged", "status": "assigned", "owner": "bob",
         "priority": "normal", "updated": "2026-07-12"},                      # clean → no attention
        {"id": "i2", "title": "New ticket", "status": "unread", "priority": "normal",
         "owner": "", "updated": "2026-07-12"},                               # unread
        {"id": "i3", "title": "Unrouted", "status": "triaged", "priority": "normal",
         "owner": "", "updated": "2026-07-12"},                              # unassigned
        {"id": "i4", "title": "Urgent open", "status": "assigned", "owner": "amy",
         "priority": "high", "updated": "2026-07-12"},                        # high-priority
        {"id": "i5", "title": "Rotting", "status": "triaged", "owner": "cara",
         "priority": "normal", "updated": "2026-06-20"},                      # stale
        {"id": "i6", "title": "Resolved", "status": "done", "owner": "", "priority": "high",
         "updated": "2026-01-01"},                                            # done → excluded
    ]}
    items = ic.items_needing_attention(qq, today="2026-07-12", stale_days=3)
    ids = [it["id"] for it in items]
    check("i1" not in ids, "attention: clean assigned item excluded")
    check("i6" not in ids, "attention: done item excluded (even if high+stale)")
    check(set(ids) == {"i2", "i3", "i4", "i5"}, "attention: flags exactly the 4 needy items")
    check(items[0]["id"] == "i4", "attention: high-priority sorts first")
    check("unread" in next(it for it in items if it["id"] == "i2")["reasons"], "attention: unread tagged")
    check("unassigned" in next(it for it in items if it["id"] == "i3")["reasons"], "attention: triaged+no-owner → unassigned")
    check(any(r.startswith("stale_") for r in next(it for it in items if it["id"] == "i5")["reasons"]), "attention: stale tagged")
    check("queue clear" in ic.render_attention([], "X"), "render_attention: empty → clear")
    check("4 items need attention" in ic.render_attention(items, "Y"), "render_attention: counts")
    check("handle now" in ic.render_attention(items, "Y") and "triage" in ic.render_attention(items, "Y"), "render_attention: shows recommended actions (handle now / triage)")

    print("\n" + ("PASS — all checks green" if not _fails else f"FAIL — {len(_fails)} failing"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
