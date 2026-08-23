#!/usr/bin/env python3
"""
The Discord bridge must not claim a proactive file addressed to another bridge.

It claims every `results/proactive-*.txt`, then calls `int()` on the `[channel:]`
value. A Matrix room id raises, the owner-DM fallback re-parses the same value and
raises again, and the file is parked in `undelivered/` — so the gateway, the only
consumer that can deliver it, never sees the file. Observed twice on 2026-08-17:

    [proactive] failed to DM 1022910063620390932:
      invalid literal for int() with base 10: '!PrxhizfLysTYrYDcnw:ag2.space'
    [proactive] send failure -> parked: proactive-1786941735.txt

`slack-bridge.py` and `telegram-bridge.py` already gate before claiming. This pins
the same rule for Discord and the ordering that makes it work: the shape check has
to run BEFORE the int().

The `\\d{17,20}` literal this file used to grep for now lives once, in
`proactive_routing`: three adapters spelling it privately is exactly how two of
them ended up recognising ONLY Discord, so a Matrix room read as unaddressed. The
assertions moved to the delegation and every property they protected — existence,
ordering, three-adapter parity — is still here, plus a behavioural check that the
delegate is not a no-op and a scan that no private copy survived.
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "discord-bridge.py"
fail = 0


def check(cond, label):
    global fail
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        fail = 1


text = SRC.read_text()

# Scope to the proactive poll block, so a match elsewhere in this 5k-line file
# cannot satisfy the assertions below.
start = text.find('if f.name.startswith("proactive-")')
end = text.find("[proactive] send failure", start)
check(start != -1 and end > start, "proactive claim block is locatable")
block = text[start:end]

# Post-5b the release goes through the claim fence, whose release() is
# behaviorally pinned (fence suite: "release restores the .txt").
check("_proactive_fence().release" in block,
      "the proactive block can release a claim back to the polling stream")

GATE = "redirect_target_is_foreign("
check(GATE in block,
      "a foreign-target check exists in the proactive claim block")
# Behavioural, not merely present: a grep is satisfied by a no-op delegate.
sys.path.insert(0, str(REPO / "src"))
from proactive_routing import redirect_target_is_foreign  # noqa: E402
check(redirect_target_is_foreign("!PrxhizfLysTYrYDcnw:ag2.space", "discord")
      and not redirect_target_is_foreign("1022910063620390932", "discord"),
      "and the delegate rejects a Matrix room while accepting a snowflake")

# Ordering IS the fix: a check after owner resolution has already done work
# for a file this bridge is not handling.
gi = block.find(GATE)
ii = block.find("owner_id is None")
check(gi != -1 and ii != -1 and gi < ii,
      "the shape check runs BEFORE owner resolution — a foreign file needs no owner")

# The release must be reached by the foreign branch, not only by the empty-text
# branch that already existed.
foreign = block[gi:ii] if (gi != -1 and ii != -1) else ""
check("_proactive_fence().release" in foreign and "continue" in foreign,
      "the foreign-target branch releases the claim and stops processing")

# Parity: not new policy, and now through the SAME module, so the three adapters
# cannot drift apart again — which is how two of them ended up Discord-only.
PRIVATE = re.compile(r"\\d\{17,20\}")
for sib, channel in (("slack-bridge.py", "slack"), ("telegram-bridge.py", "telegram")):
    q = REPO / "src" / sib
    src = q.read_text() if q.is_file() else ""
    check(f'proactive_body_guard(f.name, peek, "{channel}")' in src,
          f"{sib} gates through proactive_routing (parity, not new policy)")
    check(not PRIVATE.search(src),
          f"{sib} keeps NO private copy of the id grammar")

# release_claim must return the file to .txt — parking or unlinking would still
# keep it away from the gateway.
rc = (REPO / "src" / "proactive_recovery.py").read_text()
check('with_suffix(".txt")' in rc,
      "release_claim returns the claim to a .txt the other bridge will poll")

if fail:
    print("FAIL: discord proactive foreign-target")
    sys.exit(1)
print("PASS: a foreign-targeted proactive file is left for its own bridge.")
