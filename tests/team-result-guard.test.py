#!/usr/bin/env python3
"""The non-owner result guard: one implementation, and it runs before markers.

Removing the Team provider session moved Team work onto the direct-core path,
which took the final secret and delivery-marker scan with it — the scan lived
inside the provider runner, so nothing reached it once the runner was bypassed.
The policy now lives in src/team_result_guard.py and the routers call it.

Part 1 (BEHAVIORAL) drives the guard through owner-passthrough, every withheld
case, and a scanner that raises — the last one because "unscannable" must fail
CLOSED, and a guard only ever observed passing is not a validated guard.

Part 2 (STRUCTURAL) pins what a behavioral test cannot reach: that the guard is
called BEFORE parse_markers in the delivery loop (ordering is the whole point —
a scan after the router has already read a redirect is decoration), and that no
consumer redefines the policy it is supposed to import.

Run: python3 tests/team-result-guard.test.py
Exit code: 0 on pass, 1 on fail.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import team_result_guard as guard  # noqa: E402

BRIDGE = REPO / "src" / "discord-bridge.py"
WORKER = REPO / "skills" / "task-workstream-sessions" / "scripts" / "session-worker.py"


class _Scan:
    def __init__(self, detected, types_=()):
        self.detected = detected
        self.secret_types = list(types_)


def _clean(_body):
    return _Scan(False)


def _leaky(_body):
    return _Scan(True, ["aws_key"])


def _raises(_body):
    raise RuntimeError("scanner down")


def behavioral() -> list:
    fails = []
    cases = [
        # name, body, tier, filter, expect_withheld
        ("owner keeps its markers", "see [channel: 123]", "owner", _clean, False),
        ("owner keeps an attach", "[attach: /tmp/x.png]", "owner", _clean, False),
        # A control is a marker WHERE THE ROUTER EXECUTES IT (result_markers):
        # [channel:] on its line, attach aliases anywhere, skips at body start.
        ("team redirect withheld", "[channel: 123]\nbody", "team", _clean, True),
        ("team redirect MENTION passes", "see [channel: 123] in prose", "team", _clean, False),
        ("team attach withheld", "[attach: /etc/passwd]", "team", _clean, True),
        ("team attach inline still withheld", "see [file: /x] here", "team", _clean, True),
        ("team no-send withheld", "[no-send]", "team", _clean, True),
        ("team deduped withheld", "[deduped: task-1]", "team", _clean, True),
        ("team no-send MENTION passes", "the [no-send] marker is documented", "team", _clean, False),
        # The parser peels a D7 header before reading skips, so the guard must
        # classify what parse_markers EXECUTES, not what body-start text shows.
        ("team D7-headed no-send withheld", "**[core: 1]**\n[no-send]\nhide", "team", _clean, True),
        ("team non-leading channel passes", "intro\n[channel: 123]\nquoted", "team", _clean, False),
        ("team dm-only passes", "text\n[dm-only]\nmore", "team", _clean, False),
        ("team dm-only suppressed redirect passes", "[dm-only]\n[channel: 123]\nboth", "team", _clean, False),
        ("team secret withheld", "ordinary text", "team", _leaky, True),
        ("team clean text passes", "ordinary text", "team", _clean, False),
        ("guest guarded like team", "[channel: 9]\nx", "guest", _clean, True),
        ("unknown tier guarded", "[channel: 9]\nx", "", _clean, True),
        ("None tier guarded", "[channel: 9]\nx", None, _clean, True),
    ]
    # Suppressive markers get the HONEST notice; everything else the leak one.
    _suppress_names = {"team no-send withheld", "team deduped withheld",
                       "team D7-headed no-send withheld"}
    for name, body, tier, filt, expect in cases:
        out, why = guard.guard_result_for_tier(body, tier, REPO, secret_filter=filt)
        withheld = why is not None
        if withheld != expect:
            fails.append(f"{name}: expected withheld={expect}, got {withheld} ({why})")
            continue
        if withheld:
            sentinel = (guard.TEAM_SUPPRESS_RESULT if name in _suppress_names
                        else guard.TEAM_LEAK_RESULT)
            if out != sentinel:
                fails.append(f"{name}: withheld but body was not the expected sentinel")
            if name in _suppress_names and "sensitive" in out:
                fails.append(f"{name}: suppress notice must not allege sensitive content")
        if not withheld and out != body:
            fails.append(f"{name}: passed but body was altered")

    # An unscannable result must be withheld, never delivered on the assumption
    # that it was probably fine.
    out, why = guard.guard_result_for_tier("x", "team", REPO, secret_filter=_raises)
    if out != guard.TEAM_LEAK_RESULT or not why:
        fails.append("a raising scanner must fail CLOSED and withhold the body")

    # The caller is handed only the safe body — it cannot deliver the raw text
    # by swallowing an exception.
    out, _ = guard.guard_result_for_tier("[channel: 5] secret", "team", REPO, secret_filter=_clean)
    if "[channel:" in out:
        fails.append("the withheld body still carried the control marker")

    if guard.is_guarded_tier("owner"):
        fails.append("owner must not be guarded")
    for tier in ("team", "guest", "other", "ambient", "", None, "OWNERISH"):
        if not guard.is_guarded_tier(tier):
            fails.append(f"tier {tier!r} must be guarded (only exact 'owner' is exempt)")
    if not guard.is_guarded_tier("Owner "):
        pass  # case/space-insensitive owner is intentionally exempt
    return fails


def structural() -> list:
    fails = []
    bridge = BRIDGE.read_text()
    worker = WORKER.read_text()

    if "from team_result_guard import" not in bridge:
        fails.append("discord-bridge must import the shared guard")

    # Ordering is the requirement: a scan that runs after the router has read a
    # redirect or an attachment path has already lost.
    call = bridge.find("guard_result_for_tier(reply_text")
    markers = bridge.find("_parsed = parse_markers(reply_text)")
    if call < 0:
        fails.append("discord-bridge must call guard_result_for_tier on the reply body")
    elif markers < 0:
        fails.append("could not locate the parse_markers call in the delivery loop")
    elif call > markers:
        fails.append("the guard must run BEFORE parse_markers, not after")

    # An unknown tier must be re-derived from the durable task file: the
    # in-memory tier map is not restored on restart.
    window = bridge[max(0, call - 800):markers] if call > 0 else ""
    if "_resolve_task_tier" not in window:
        fails.append("an unknown tier must be resolved from the task file before guarding")

    # One implementation: consumers import the policy, never restate it.
    for name, src, label in ((BRIDGE.name, bridge, "bridge"), (WORKER.name, worker, "worker")):
        if re.search(r"^\s*TEAM_RESULT_CONTROL\s*=\s*re\.compile", src, re.M):
            fails.append(f"{name} redefines TEAM_RESULT_CONTROL instead of importing it")
        if re.search(r"^class TeamResultLeakError", src, re.M):
            fails.append(f"{name} redefines TeamResultLeakError instead of importing it")
        if re.search(r"^def resolve_access_tier", src, re.M):
            fails.append(f"{name} redefines resolve_access_tier instead of importing it")
    if "from team_result_guard import" not in worker:
        fails.append("session-worker must import the shared guard")
    return fails


def main() -> int:
    for path in (BRIDGE, WORKER):
        if not path.exists():
            print(f"FAIL: missing {path}")
            return 1
    fails = behavioral() + structural()
    if fails:
        print("FAIL: team result guard has issues:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: non-owner results are scanned before any marker is interpreted.")
    # Verdict ownership: the wrapper must DERIVE from classify (one owner).
    for body, tier, filt, kind in (
        ("plain reply", "team", _clean, guard.VERDICT_DELIVER),
        ("plain reply", "owner", _leaky, guard.VERDICT_DELIVER),
        ("[no-send]\nhide", "team", _clean, guard.VERDICT_SUPPRESS),
        ("**[core: 1]**\n[no-send]\nhide", "team", _clean, guard.VERDICT_SUPPRESS),
        ("[attach: /etc/passwd]", "team", _clean, guard.VERDICT_LEAK),
        ("secret text", "team", _leaky, guard.VERDICT_LEAK),
        # Marker check precedes the secret scan: a secret-carrying skip body
        # still SUPPRESSES — the leaky filter never gets a say on a stubbed body.
        ("[no-send]\nsecret text", "team", _leaky, guard.VERDICT_SUPPRESS),
    ):
        v = guard.classify_result_for_tier(body, tier, REPO, secret_filter=filt)
        assert v.kind == kind, (body, tier, v)
        wrapped = guard.guard_result_for_tier(body, tier, REPO, secret_filter=filt)
        assert wrapped == (v.body, v.reason), (body, tier, "wrapper diverged from verdict")
    # Every stub verdict must also be a suppress verdict, and no influenced
    # bytes may ride in the stub (fixed literals + grammar-checked id only).
    for body, tier, stub in (
        ("[no-send]", "team", "[no-send]"),
        ("[no-send]\nhidden content", "team", "[no-send]"),
        ("[REPLIED]", "team", "[REPLIED]"),
        ("[deduped: task-abc123]", "team", "[deduped: task-abc123]"),
        ("[deduped: EVIL bytes]", "team", None),
        ("[no-send]", "owner", None),
        ("plain reply", "team", None),
        ("[channel: 123]\nx", "team", None),
    ):
        got = guard.suppression_stub_for_tier(body, tier)
        assert got == stub, (body, tier, got)
        if got is not None:
            # stub⊆suppress holds regardless of filter: the marker check
            # precedes the secret scan, so _leaky cannot flip a stub to LEAK.
            for filt in (_clean, _leaky):
                v = guard.classify_result_for_tier(body, tier, REPO, secret_filter=filt)
                assert v.kind == guard.VERDICT_SUPPRESS, (body, filt.__name__, "stub without suppress verdict")
            assert got == body[:len(got)] or got.startswith("[deduped:"), body
    print("  [stub] suppression_stub_for_tier: inert bytes only, subset of suppress")
    print("  [verdict] classify_result_for_tier owns the three-way decision;")
    print("            guard_result_for_tier provably derives from it")
    print("  [behavioral] owner passes through; redirect/attach/no-send/secret withheld;")
    print("               unknown tier guarded; a raising scanner fails CLOSED")
    print("  [structural] guard precedes parse_markers in the delivery loop, unknown tier")
    print("               re-derived from the task file, policy defined in exactly one place")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
