#!/usr/bin/env python3
"""room_ops `members`: the client half of a gateway op that always existed.

Pins the two things a caller depends on: every member the gateway names comes
back (the whole point — history only shows who SPOKE, not who is PRESENT), and
`kind` is derived here rather than reported, so its rules are testable.
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "agent-room-ops"))

spec = importlib.util.spec_from_file_location(
    "members", REPO / "skills" / "agent-room-ops" / "members.py")
mem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mem)

fails = []


def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        fails.append(msg)


print("1. classify_member — the naming heuristic, stated as rules")
# Every id below was observed in a real 20-member room, so these are the
# populations the rule actually has to separate, not invented shapes.
for uid in ("@yixuan-desktop.agent:ag2.space",
            "@liususan091219-susan-s-bot.agent:ag2.space",
            "@ruiwangwarm-sutando-rui-codex.agent:ag2.space"):
    check(mem.classify_member(uid) == "agent", f"`.agent:` suffix -> agent ({uid})")
for uid in ("@sutando-rui:ag2.space", "@sutando-bassil:ag2.space",
            "@sutando-qingyun-001:ag2.space"):
    check(mem.classify_member(uid) == "agent", f"`@sutando-` prefix -> agent ({uid})")
for uid in ("@qingyun:ag2.space", "@chi:ag2.space", "@vidhu:ag2.space"):
    check(mem.classify_member(uid) == "human", f"bare localpart -> human ({uid})")
check(mem.classify_member("") == "human", "empty id does not crash (defaults human)")
# `sutando` inside a localpart is not the prefix: the rule is anchored, so a
# human who happens to be named for the product is not reclassified.
check(mem.classify_member("@notsutando-x:ag2.space") == "human",
      "`sutando-` mid-localpart is NOT the agent prefix")


def _run(payload, *, raises=None):
    """Drive room_members with the gateway stubbed at the http_json seam."""
    saved_g, saved_h = mem.gateway, mem.http_json
    try:
        mem.gateway = lambda: ("https://example.invalid", {})
        if raises is not None:
            def _boom(*a, **k):
                raise raises
            mem.http_json = _boom
        else:
            mem.http_json = lambda *a, **k: (200, payload)
        return mem.room_members("!r:ag2.space")
    finally:
        mem.gateway, mem.http_json = saved_g, saved_h


print("2. response shaping")
r = _run({"ok": True, "members": [
    {"user_id": "@a:ag2.space", "display_name": "A"},
    {"user_id": "@sutando-b:ag2.space", "display_name": "B"}]})
check(r["ok"] and len(r["members"]) == 2, "both members returned")
check(r["members"][0]["kind"] == "human" and r["members"][1]["kind"] == "agent",
      "kind is attached per member")
check(all("display_name" in m for m in r["members"]), "display_name preserved")

r = _run({"ok": True, "members": [
    {"user_id": "@a:ag2.space"}, {"display_name": "no id"}, "not-a-dict", {"user_id": ""}]})
check(len(r["members"]) == 1 and r["members"][0]["display_name"] == "",
      "entries without a usable user_id are dropped, not crashed on")

print("3. failure paths never return a partial-looking success")
check(_run({"ok": False, "reason": "declined"})["ok"] is False, "gateway ok:false -> ok false")
check(_run({"error": "boom"})["ok"] is False, "gateway error -> ok false")
check(_run("not a dict")["ok"] is False, "malformed response -> ok false")
r = _run(None, raises=TimeoutError("slow"))
check(r["ok"] is False and "network" in (r["reason"] or ""), "timeout -> network reason")
check(_run({"ok": True})["members"] == [], "missing members key -> empty list, ok true")

saved = mem.gateway
try:
    mem.gateway = lambda: (None, {})
    check(mem.room_members("!r:ag2.space")["reason"] == "no gateway configured",
          "no gateway configured is named, not a crash")
finally:
    mem.gateway = saved

if fails:
    print(f"\n{len(fails)} FAILURE(S)")
    raise SystemExit(1)
print("\nALL PASS — members enumerates presence, and `kind` is a stated rule")
