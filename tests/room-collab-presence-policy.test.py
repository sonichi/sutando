#!/usr/bin/env python3
"""Direct coverage for packages/room-collab/presence_policy.py.

The module is pure, so every case is in-memory: no sockets, no files, no
clock. `now` is an argument, which is what makes the 30-minute rule testable
at its boundary instead of by waiting.
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "packages" / "room-collab" / "presence_policy.py"

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILS.append(label)


spec = importlib.util.spec_from_file_location("presence_policy", MODULE)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

NOW = 1_000_000.0


def want(room, kind="markdown", summoned_at=NOW):
    return {"room": room, "kind": kind, "identity": "@a:x", "name": "A",
            "summoned_at": summoned_at}


def held(room, kind="markdown", last_activity=NOW, state=policy.CONNECTED, since=NOW):
    return {"room": room, "kind": kind, "state": state,
            "since": since, "last_activity": last_activity}


def keys(entries):
    return sorted(policy.key_of(e) for e in entries)


print("── joining ──")
p = policy.plan([want("!a")], [], NOW)
check("a summoned surface with nothing live is connected", keys(p["connect"]) == [("!a", "markdown")])
check("...and nothing is dropped", p["drop"] == [])

p = policy.plan([want("!a")], [held("!a")], NOW)
check("one already held is left alone", p["connect"] == [] and p["drop"] == [])

check(
    "the same room is two surfaces when the kind differs",
    keys(policy.plan([want("!a", "markdown"), want("!a", "board")], [], NOW)["connect"])
    == [("!a", "board"), ("!a", "markdown")],
)

print("── leaving ──")
p = policy.plan([], [held("!a")], NOW)
check("a surface no longer summoned is dropped as `left`", p["drop"] == [(("!a", "markdown"), "left")])

print("── the 30-minute idle rule ──")
IDLE = policy.IDLE_SECONDS
check("the owner's timeout is 30 minutes", IDLE == 1800.0)

p = policy.plan([want("!a")], [held("!a", last_activity=NOW - IDLE + 1)], NOW)
check("one second short of the timeout is still held", p["drop"] == [])

p = policy.plan([want("!a")], [held("!a", last_activity=NOW - IDLE)], NOW)
check("exactly at the timeout it is dropped as `idle`", p["drop"] == [(("!a", "markdown"), "idle")])
# ⚠ The bug this pins: still CONNECTED in the record `plan` was given, so it
# was waved back in on the SAME pass — and the drop assertion passed anyway.
check("...and NOT reconnected in the same pass", p["connect"] == [])

# The rule is worth nothing if the next pass rejoins what it just dropped.
p = policy.plan([want("!a", summoned_at=NOW - 5000)],
                [held("!a", state=policy.IDLE, since=NOW - 100)], NOW)
check("an idled surface does NOT rejoin on the next pass", p["connect"] == [])

p = policy.plan([want("!a", summoned_at=NOW)],
                [held("!a", state=policy.IDLE, since=NOW - 100)], NOW)
check("...but a NEW summon brings it back", keys(p["connect"]) == [("!a", "markdown")])

print("── the cap ──")
check("the fuse is 16", policy.MAX_CONNECTIONS == 16)

many = [want(f"!r{n}", summoned_at=NOW - n) for n in range(5)]
p = policy.plan(many, [], NOW, cap=3)
check("no more than the cap are connected at once", len(p["connect"]) == 3)
check(
    "the newest summons win the slots",
    keys(p["connect"]) == [("!r0", "markdown"), ("!r1", "markdown"), ("!r2", "markdown")],
)

# A cap lowered below what is held must shed, and shed the quietest.
p = policy.plan(
    [want("!a"), want("!b"), want("!c")],
    [held("!a", last_activity=NOW - 300), held("!b", last_activity=NOW - 10),
     held("!c", last_activity=NOW - 600)],
    NOW, cap=2,
)
check("a lowered cap sheds the least recently active", p["drop"] == [(("!c", "markdown"), "capped")])

# ⚠ #4646 review: at the cap, over = 0 and room = 0 queued a fresh summon forever.
full = [held(f"!h{n}", last_activity=NOW - 100 - n) for n in range(3)]
p = policy.plan(
    [want(f"!h{n}", summoned_at=NOW - 500) for n in range(3)] + [want("!new", summoned_at=NOW)],
    full, NOW, cap=3,
)
check("at the cap, a fresh summon evicts the least recently active",
      p["drop"] == [(("!h2", "markdown"), "capped")])
check("...and that summon is the one admitted", keys(p["connect"]) == [("!new", "markdown")])

# The mirror: a summon OLDER than every holder's last activity waits its turn.
p = policy.plan(
    [want(f"!h{n}", summoned_at=NOW - 500) for n in range(3)] + [want("!old", summoned_at=NOW - 900)],
    full, NOW, cap=3,
)
check("a summon older than the quietest holder does NOT evict it", p["drop"] == [])
check("...and nothing is admitted over the cap", p["connect"] == [])

# `capped` and `idle` are different states for a reason: only one needs a new
# summon to come back.
p = policy.plan([want("!a", summoned_at=NOW - 5000)],
                [held("!a", state=policy.CAPPED, since=NOW - 100)], NOW)
check("a capped surface returns without a new summon", keys(p["connect"]) == [("!a", "markdown")])

print("── malformed input ──")
p = policy.plan([{"room": "!a"}, {"kind": "markdown"}, want("!b")], [], NOW)
check("an entry missing room or kind is ignored, not connected", keys(p["connect"]) == [("!b", "markdown")])

p = policy.plan([want("!a")], [{"room": "!a", "kind": "markdown", "state": "connected"}], NOW)
check("a live entry with no timestamp reads as idle, not as fresh", p["drop"] == [(("!a", "markdown"), "idle")])


print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all presence-policy checks ok'}")
sys.exit(1 if FAILS else 0)
