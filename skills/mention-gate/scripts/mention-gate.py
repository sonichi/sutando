#!/usr/bin/env python3
"""mention-gate CLI — let messages that @-tag the OWNER reach the fleet.

Usage:
    python3 skills/mention-gate/scripts/mention-gate.py on [--for 2h|30m|1d]
    python3 skills/mention-gate/scripts/mention-gate.py off
    python3 skills/mention-gate/scripts/mention-gate.py status

Default (off) is today's behavior: in a requireMention channel, a message
tagging the owner but not the bot is never ingested. `on` makes an owner-tag
count as a bot mention, so such messages become tasks; each one is audit-
logged. `--for` adds an auto-expiry that flips the gate back off. State lives
at `<workspace>/state/mention-gate.json`; the policy is `src/mention_gate.py`.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Canonical workspace resolution. workspace_default lives in <repo>/src; this
# script is at <repo>/skills/mention-gate/scripts/, so parents[3] is <repo>.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from workspace_default import resolve_workspace  # noqa: E402
import mention_gate  # noqa: E402

_DURATION_RE = re.compile(r"^(\d+)([mhd])$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def _parse_duration(value: str) -> timedelta:
    m = _DURATION_RE.match(value.strip())
    if not m:
        raise SystemExit(f"mention-gate: bad --for value '{value}' (want e.g. 30m, 2h, 1d)")
    return timedelta(seconds=int(m.group(1)) * _UNIT_SECONDS[m.group(2)])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mention-gate", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    on = sub.add_parser("on", help="owner-tagged messages count as bot mentions (ingested)")
    on.add_argument("--for", dest="duration", default=None,
                    help="auto-expiry, e.g. 30m, 2h, 1d")
    sub.add_parser("off", help="back to today's behavior (owner tags do not trigger)")
    sub.add_parser("status", help="show the gate state + how many messages it pulled in")
    args = parser.parse_args(argv)

    workspace = resolve_workspace()

    if args.command == "on":
        until = None
        if args.duration:
            expiry = datetime.now(timezone.utc) + _parse_duration(args.duration)
            until = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
        mention_gate.write_state(workspace, mentions_enabled=True, until=until)
        tail = f" until {until} (then back off)" if until else ""
        print(f"mention-gate: ON for THIS HOST — messages @-tagging the owner now "
              f"count as bot mentions and are ingested{tail}.")
        print("Free-listen (requireMention:false) channels already ingest everything "
              "and are unaffected.")
        print(f"Scope: this host only ({workspace}/state/ is per-host and is not "
              "carried by vault sync). Other hosts keep their own setting — run "
              "this command on each host you want changed.")
        return 0

    if args.command == "off":
        mention_gate.write_state(workspace, mentions_enabled=False, until=None)
        print("mention-gate: OFF for THIS HOST — today's behavior: owner-tagged "
              "messages without a bot mention are not ingested here.")
        print("Scope: this host only. Another host with the gate ON still ingests "
              "owner tags — run this command there too.")
        return 0

    state = mention_gate.read_state(workspace)
    active = mention_gate.owner_tag_triggers_ingest(workspace)
    if active:
        tail = f" until {state['until']}" if state["until"] else ""
        print(f"mention-gate: ON (this host) — owner tags trigger ingestion{tail}.")
    elif state["mentions_enabled"] and state["until"]:
        print(f"mention-gate: OFF (was on; expired at {state['until']}).")
    else:
        print("mention-gate: OFF (default) on this host — owner-tagged messages "
              "without a bot mention are not ingested.")
    print(f"pulled in so far: {mention_gate.gated_ingest_count(workspace)} message(s) "
          "(audit: state/mention-gate-ingested.jsonl).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
