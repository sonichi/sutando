#!/usr/bin/env python3
"""A bridge must decline a destination it cannot address.

Five proactive messages addressed to ag2.space rooms were claimed by the Discord
bridge, failed to send, and were quarantined into results/undelivered/. The
gateway bridge has a takeover path for abandoned proactives, but it scans
results/ root only — so quarantining removes the file from the one directory
that could have recovered it. Declining before claiming is what keeps the
takeover reachable.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from proactive_routing import can_route, explicit_target  # noqa: E402

DISCORD = r"\d{17,20}"
SLACK = r"[CDG][A-Z0-9]+"
AG2SPACE = r"![A-Za-z0-9]+:[a-z0-9.]+"

failures = []


def check(cond, label):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


# The actual body that was lost, verbatim in shape.
LOST = "[channel: !PrxhizfLysTYrYDcnw:ag2.space]\n**Sublist #54** — 13 new / 3 carried"

check(explicit_target(LOST) == "!PrxhizfLysTYrYDcnw:ag2.space",
      "the ag2.space room id is extracted as the explicit target")
check(not can_route(explicit_target(LOST), DISCORD),
      "Discord DECLINES the ag2.space target (the whole point)")
check(can_route(explicit_target(LOST), AG2SPACE),
      "the gateway's grammar DOES accept it, so declining strands nothing")
check(not can_route(explicit_target(LOST), SLACK),
      "Slack declines it too")

# A Discord-addressed body must still be claimed — this must narrow nothing.
D = "[channel: 1530802402603700415]\nreply goes to #dev"
check(explicit_target(D) == "1530802402603700415", "a Discord id is extracted")
check(can_route(explicit_target(D), DISCORD), "Discord still claims its own targets")

# No marker at all is the common case: routing picks, unchanged.
check(explicit_target("plain proactive body, no marker") is None,
      "a body with no redirect has no explicit target")
check(can_route(None, DISCORD) and can_route(None, SLACK) and can_route(None, AG2SPACE),
      "no target -> every bridge still routable (behavior unchanged)")

# dm-only suppresses the redirect: it must read as 'no target', never as an
# unroutable one, or the privacy guard would strand the body everywhere.
DM = "[dm-only]\n[channel: !PrxhizfLysTYrYDcnw:ag2.space]\nprivate calendar contents"
check(explicit_target(DM) is None,
      "dm-only suppresses the redirect, so no target is reported")
check(can_route(explicit_target(DM), DISCORD),
      "a dm-only body stays claimable — the guard must not strand it")

# Anchoring: a target that merely CONTAINS digits must not pass as a snowflake.
check(not can_route("!abc1530802402603700415:ag2.space", DISCORD),
      "match is anchored — an embedded snowflake does not make it Discord's")

# --- WIRING: the helper passing proves nothing if Discord never calls it ------
# ORDER is the invariant: declining after the rename still quarantines.
_src = (REPO / "src" / "discord-bridge.py").read_text()
check("can_route" in _src and "explicit_target" in _src,
      "discord-bridge imports the shared routability policy")
check("DISCORD_CHANNEL_ID_RE" in _src,
      "discord-bridge declares its own id grammar (the module names none)")

_decline = _src.find("if not can_route(_target, DISCORD_CHANNEL_ID_RE)")
_claim = _src.find('claim = f.with_suffix(".sending")')
check(_decline != -1, "the decline branch is present")
check(_claim != -1, "the claim-by-rename is present")
check(_decline != -1 and _claim != -1 and _decline < _claim,
      f"decline precedes claim-by-rename (decline@{_decline} < claim@{_claim})")

# It must LEAVE the file, not quarantine it -- the gateway's takeover scans
# results/ root, so a rename anywhere makes recovery impossible.
_between = _src[_decline:_claim] if (_decline != -1 and _claim != -1) else ""
check("continue" in _between and "undelivered" not in _between,
      "declining leaves the file in results/ (no rename, no quarantine)")

# The regex must not be re-declared anywhere else in the bridge.
check(_src.count('r"\\d{17,20}"') <= 1,
      "the snowflake grammar is declared once, not copied")

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all routability assertions passed")
