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

`slack-bridge.py` and `telegram-bridge.py` already gate on `\\d{17,20}` before
claiming. This pins the same rule for Discord and the ordering that makes it work:
the shape check has to run BEFORE the int().
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

check("release_claim" in block,
      "the proactive block can release a claim back to the polling stream")

shape = re.search(r'fullmatch\(r"\\d\{17,20\}"', block) or re.search(r"\\d\{17,20\}", block)
check(shape is not None,
      "a Discord-id shape check exists in the proactive claim block")

# Ordering IS the fix: a check after owner resolution has already done work
# for a file this bridge is not handling.
gi = block.find(r"\d{17,20}")
ii = block.find("owner_id is None")
check(gi != -1 and ii != -1 and gi < ii,
      "the shape check runs BEFORE owner resolution — a foreign file needs no owner")

# The release must be reached by the foreign branch, not only by the empty-text
# branch that already existed.
foreign = block[gi:ii] if (gi != -1 and ii != -1) else ""
check("release_claim" in foreign and "continue" in foreign,
      "the foreign-target branch releases the claim and stops processing")

# Parity: the rule is not new policy, it is what the sibling bridges already do.
for sib in ("slack-bridge.py", "telegram-bridge.py"):
    p = REPO / "src" / sib
    check(p.is_file() and r"\d{17,20}" in p.read_text(),
          f"{sib} already gates on the same shape (parity, not new policy)")

# release_claim must return the file to .txt — parking or unlinking would still
# keep it away from the gateway.
rc = (REPO / "src" / "proactive_recovery.py").read_text()
check('with_suffix(".txt")' in rc,
      "release_claim returns the claim to a .txt the other bridge will poll")

if fail:
    print("FAIL: discord proactive foreign-target")
    sys.exit(1)
print("PASS: a foreign-targeted proactive file is left for its own bridge.")
