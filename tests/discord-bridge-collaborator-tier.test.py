#!/usr/bin/env python3
"""
Regression test for the per-channel team-collaborator "engage" path.

Background: Discord tier resolution is GLOBAL — a team-tier sender is
sandboxed via `codex exec --sandbox read-only` (or NO-REPLY'd) everywhere.
That's wrong for a designated channel collaborator (e.g. a co-worker the
owner wants engaged substantively in one specific channel). This change
adds a first-class, per-channel `collaborators` list: a team sender listed
under the SERVING channel's `collaborators` gets the `team-collaborator`
rulebook (engage in-channel, fold in their input) instead of the codex /
NO-REPLY team rulebook — WITHOUT elevating them to global owner. The
authority boundary is unchanged: irreversible / system-mutating actions
still require the owner.

This test guards the wiring structurally (matching the sibling
discord-bridge-access-tier.test.py — the real Discord flow needs a live
bridge + mocked discord.py objects, out of scope here). It asserts:

  1. `is_collaborator` defaults False (fail-closed).
  2. The collaborator check is keyed on the SERVING channel id
     (message.channel.id) — per-channel, not global.
  3. The codex-preamble branch excludes collaborators (`and not
     is_collaborator`) — they are engaged directly, not sandboxed.
  4. The silent-escalate branch excludes collaborators too.
  5. A `team-collaborator` rulebook key exists and preserves the authority
     boundary (mentions the owner-only / no-commit constraint).
  6. The task-file assembly selects the `team-collaborator` rulebook for a
     collaborator while keeping `access_tier: team` on the wire (existing
     team consumers unchanged) plus a `collaborator: true` marker.

Run: python3 tests/discord-bridge-collaborator-tier.test.py
Exit code: 0 on pass, 1 on fail.
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"


def fail(msg: str, ctx: str = "") -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("---context---", file=sys.stderr)
        print(ctx, file=sys.stderr)
    return 1


def main() -> int:
    if not BRIDGE.exists():
        return fail(f"{BRIDGE} not found")

    src = BRIDGE.read_text()

    # 1. is_collaborator defaults False (fail-closed), before the tier checks.
    if not re.search(r"is_collaborator\s*=\s*False", src):
        return fail("is_collaborator should default to False (fail-closed)")

    # 2. The collaborator check is keyed on the SERVING channel id.
    #    Must read the serving channel's own group config (message.channel.id)
    #    and set is_collaborator = True from its `collaborators` list.
    serving_check = re.search(
        r"groups[^\n]*message\.channel\.id[\s\S]{0,240}?collaborators[\s\S]{0,120}?is_collaborator\s*=\s*True",
        src,
    )
    if not serving_check:
        return fail(
            "collaborator check must be keyed on the SERVING channel "
            "(message.channel.id) and set is_collaborator = True from that "
            "channel's `collaborators` list"
        )

    # 3. Codex-preamble branch excludes collaborators.
    if not re.search(
        r'if\s+access_tier\s+in\s+\("team",\s*"other"\)\s+and\s+not\s+is_collaborator\s*:',
        src,
    ):
        return fail(
            "codex-preamble branch must exclude collaborators "
            '(`if access_tier in ("team", "other") and not is_collaborator:`) '
            "so they are engaged directly, not sandboxed via codex"
        )

    # 4. Silent-escalate branch excludes collaborators.
    if not re.search(
        r'elif\s+access_tier\s+in\s+\("team",\s*"other"\)\s+and\s+not\s+is_collaborator\s*:',
        src,
    ):
        return fail(
            "silent-escalate branch must exclude collaborators "
            '(`elif access_tier in ("team", "other") and not is_collaborator:`)'
        )

    # 5. team-collaborator rulebook key exists and preserves the authority boundary.
    if not re.search(r'"team-collaborator"\s*:', src):
        return fail("tier_instructions must define a 'team-collaborator' key")
    # Extract the rulebook body as the region between the team-collaborator key
    # and the next dict key (`"team":`). Paren-matching fails here because the
    # rulebook text itself contains a nested "(email, posts, DMs)," paren.
    rb = re.search(
        r'"team-collaborator"\s*:\s*\(([\s\S]*?)\)\s*,\s*\n\s*"team"\s*:',
        src,
    )
    if not rb:
        return fail("could not extract the team-collaborator rulebook body (expected it before the 'team' key)")
    body = rb.group(1)
    if "SUTANDO SYSTEM INSTRUCTIONS" not in body:
        return fail("team-collaborator rulebook must carry the in-band SYSTEM INSTRUCTIONS fence", body)
    # Authority boundary must be reasserted (owner still required for mutations).
    # Match tolerant of string-literal splits in the source (e.g. `require the "`
    # + `"OWNER`): require both an owner reference and an authority-boundary cue.
    if "OWNER" not in body or "authority boundary" not in body:
        return fail("team-collaborator rulebook must reassert the owner-only authority boundary", body)
    if not re.search(r"commit|push|merge|irreversible|system-mutating", body):
        return fail("team-collaborator rulebook must enumerate the owner-only (no-mutation) constraint", body)

    # 6. Task-file assembly: collaborator selects the team-collaborator rulebook,
    #    keeps access_tier=team on the wire, adds collaborator: true marker.
    if not re.search(
        r'rulebook_key\s*=\s*"team-collaborator"\s+if\s+is_collaborator\s+else\s+access_tier',
        src,
    ):
        return fail(
            "task-file assembly must select rulebook_key = 'team-collaborator' "
            "when is_collaborator else access_tier"
        )
    if not re.search(r'collaborator_line\s*=\s*"collaborator:\s*true\\n"\s+if\s+is_collaborator', src):
        return fail("task-file assembly must emit a `collaborator: true` marker line when is_collaborator")
    if not re.search(r"tier_instructions\.get\(\s*rulebook_key", src):
        return fail("task-file write must look up tier_instructions by rulebook_key (not access_tier)")
    # access_tier: {access_tier} must remain unchanged on the wire (team stays team).
    if not re.search(r'f"access_tier:\s*\{access_tier\}\\n"', src):
        return fail("access_tier line must still serialize {access_tier} verbatim (collaborators stay team)")

    print("PASS: discord-bridge.py team-collaborator engage path is wired correctly.")
    print("  - is_collaborator defaults False (fail-closed)")
    print("  - collaborator check keyed on the SERVING channel (per-channel)")
    print("  - codex-preamble + silent-escalate branches exclude collaborators")
    print("  - team-collaborator rulebook exists + reasserts owner-only authority")
    print("  - task file: collaborator rulebook selected, access_tier stays team, collaborator marker added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
