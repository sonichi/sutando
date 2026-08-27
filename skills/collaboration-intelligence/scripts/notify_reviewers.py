#!/usr/bin/env python3
"""Rule 9 as a tool: notify reviewers by their Sutando STAND, from the map.

The rule failed in prose because acting happens from momentum while rules
load on invocation — so the correct path must be the easy path. This script
resolves each reviewer through the collaboration-intelligence roster and
refuses everything rule 9 forbids: unknown names (no guessing), bare human
mentions (a person-mention triggers no Stand), and known-off-allowlist
sends (a bounced mention notifies no one).

Usage:
  notify_reviewers.py --reviewers rui,kewei --message "re-review #3303" [--send]

Without --send it prints the exact room_ops commands (plan mode). A refused
entry never starves the batch: resolvable reviewers are still notified and
the worst refusal becomes the exit — 0 all resolved; 2 unknown reviewer;
3 entry unusable (no stand/room); 4 allowlist known-false (route via owner).

Roster: <workspace>/data/collaboration-intelligence/reviewer-stands.json
  {"rui": {"human": "@rui:ag2.space", "stand": "@sutando-rui:ag2.space",
           "room": "!triage:ag2.space", "allowlisted": true, "gh": "john-the-dev"}}
`allowlisted` is evidence, not hope: true (a mention has triggered this
Stand), false (it bounced), null/absent (never observed — send, then record).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO / "src"))


def roster_path() -> Path:
    override = os.environ.get("SUTANDO_SCI_ROSTER")
    if override:
        return Path(override)
    from workspace_default import resolve_workspace
    return (Path(resolve_workspace()) / "data" / "collaboration-intelligence"
            / "reviewer-stands.json")


def load_roster() -> dict:
    p = roster_path()
    if not p.is_file():
        raise SystemExit(f"no roster at {p} — seed it from the map before "
                         "notifying (never guess Stand identities)")
    data = json.loads(p.read_text())
    if not isinstance(data, dict):
        raise SystemExit(f"roster at {p} is not an object")
    return data


def resolve(names: "list[str]", roster: dict) -> "tuple[list[dict], int]":
    """(targets, refusal_rc): one bad entry must never starve the rest of the
    batch — resolvable reviewers are still notified, the worst refusal code
    is carried to the exit so the caller sees somebody was skipped."""
    out, worst = [], 0
    for name in names:
        entry = roster.get(name)
        if entry is None:
            print(f"UNKNOWN reviewer '{name}' — not in {roster_path()}; "
                  "add them from the map, do not guess", file=sys.stderr)
            worst = max(worst, 2)
            continue
        stand, room = entry.get("stand"), entry.get("room")
        if not stand or not room:
            # a human id alone cannot be a target: person-mentions trigger no Stand
            print(f"UNUSABLE entry '{name}': needs both 'stand' and 'room' "
                  f"(human-only = not Stand addressing)", file=sys.stderr)
            worst = max(worst, 3)
            continue
        if entry.get("allowlisted") is False:
            print(f"OFF-ALLOWLIST '{name}': {stand} bounced a mention before —"
                  " route through the owner instead of re-sending",
                  file=sys.stderr)
            worst = max(worst, 4)
            continue
        out.append({"name": name, "stand": stand, "room": room,
                    "human": entry.get("human")})
    return out, worst


def command_for(target: dict, message: str) -> "list[str]":
    body = message
    if target.get("human") and target["human"] not in body:
        body = f"{body} (cc {target['human']})"
    return ["python3", str(_REPO / "skills" / "agent-room-ops" / "room_ops.py"),
            "mention", target["stand"], body, target["room"]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reviewers", required=True,
                    help="comma-separated roster keys")
    ap.add_argument("--message", required=True)
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    names = [n.strip() for n in a.reviewers.split(",") if n.strip()]
    targets, refusal_rc = resolve(names, load_roster())
    failures = 0
    for t in targets:
        argv = command_for(t, a.message)
        if not a.send:
            print("PLAN:", " ".join(argv))
            continue
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        ok, event, reason = False, "", ""
        try:
            payload = json.loads(p.stdout)
        except ValueError:
            payload = None
        # A non-object payload has no .get, and a non-string reason breaks the
        # substring test below — both crash the notifier instead of reporting.
        if isinstance(payload, dict):
            ok = bool(payload.get("ok"))
            event = str(payload.get("event_id") or "")
            reason = str(payload.get("reason") or "")
        else:
            reason = "unparseable room_ops output"
        # room_ops reports refusals in-band: rc 0, empty stderr, ok:false + reason.
        # Printing stderr alone renders every such refusal as a blank line.
        if ok:
            print(f"{t['name']}: ok=True event={event[:24]}")
        else:
            detail = reason or p.stderr.strip()[:120] or "no reason reported"
            print(f"{t['name']}: ok=False reason={detail}", file=sys.stderr)
            if "no gateway configured" in reason:
                print("  -> the ag2space env is not loaded in this process. Run:\n"
                      '     set -a; . "$CLAUDE_CONFIG_DIR/channels/ag2space/.env"; set +a',
                      file=sys.stderr)
            failures += 1
    if failures:
        return 1
    return refusal_rc


if __name__ == "__main__":
    sys.exit(main())
