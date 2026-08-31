#!/usr/bin/env python3
"""Owner CLI for explicit room pins: list/pin/unpin room -> worker bindings.

Thin dispatch over PoolLead's pin API (the affinity table's single owner);
mutations take the same lock the lead daemon uses, so a pin cannot be lost
to a concurrent assignment sweep.

Usage:
  python3 scripts/pool-bind.py list
  python3 scripts/pool-bind.py pin '<channel-or-room-id>' core-2 [--dedicated]
  python3 scripts/pool-bind.py unpin '<channel-or-room-id>'
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from workspace_default import resolve_workspace  # noqa: E402
from pool_lead import PoolLead  # noqa: E402


def _lead(workspace=None) -> PoolLead:
    ws = Path(workspace) if workspace else resolve_workspace()
    return PoolLead(ws / "tasks", ws / "state",
                    followers_fn=lambda: [], alive_fn=lambda _i: False)


def main(argv, workspace=None) -> int:
    if not argv or argv[0] not in ("list", "pin", "unpin"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    lead = _lead(workspace)
    if argv[0] == "list":
        print(json.dumps(lead.bindings(), indent=2, sort_keys=True))
        return 0
    if argv[0] == "pin":
        dedicated = "--dedicated" in argv
        argv = [a for a in argv if a != "--dedicated"]
        if len(argv) != 3:
            print("usage: pool-bind.py pin <channel> <instance> [--dedicated]",
                  file=sys.stderr)
            return 2
        channel, instance = argv[1], argv[2]
        beat = lead.state_dir / "cores" / f"{instance}.alive"
        if not beat.exists():
            print(f"warning: no liveness beat for {instance}; pinning anyway",
                  file=sys.stderr)
        print(json.dumps(
            {channel: lead.pin_room(channel, instance, dedicated=dedicated)}))
        return 0
    if len(argv) != 2:
        print("usage: pool-bind.py unpin <channel>", file=sys.stderr)
        return 2
    if lead.unpin_room(argv[1]):
        print("unpinned")
        return 0
    print("no pin on that channel")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
