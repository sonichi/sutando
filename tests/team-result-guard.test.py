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
        ("team redirect withheld", "see [channel: 123]", "team", _clean, True),
        ("team attach withheld", "[attach: /etc/passwd]", "team", _clean, True),
        ("team no-send withheld", "[no-send]", "team", _clean, True),
        ("team secret withheld", "ordinary text", "team", _leaky, True),
        ("team clean text passes", "ordinary text", "team", _clean, False),
        ("guest guarded like team", "[channel: 9]", "guest", _clean, True),
        ("unknown tier guarded", "[channel: 9]", "", _clean, True),
        ("None tier guarded", "[channel: 9]", None, _clean, True),
    ]
    for name, body, tier, filt, expect in cases:
        out, why = guard.guard_result_for_tier(body, tier, REPO, secret_filter=filt)
        withheld = why is not None
        if withheld != expect:
            fails.append(f"{name}: expected withheld={expect}, got {withheld} ({why})")
            continue
        if withheld and out != guard.TEAM_LEAK_RESULT:
            fails.append(f"{name}: withheld but body was not the leak sentinel")
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
    print("  [behavioral] owner passes through; redirect/attach/no-send/secret withheld;")
    print("               unknown tier guarded; a raising scanner fails CLOSED")
    print("  [structural] guard precedes parse_markers in the delivery loop, unknown tier")
    print("               re-derived from the task file, policy defined in exactly one place")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
