#!/usr/bin/env python3
"""The tier-map migration publishers must not promote a umask-created temp.

Both bridges write a sibling temp then `os.replace()` it over the live
`access.json`. If that temp is created by `write_text()`, the mode comes from the
process umask — so under a permissive umask the migration publishes a
world-writable access-control file. The failure is invisible in review because
the atomic-replace comment says "chmod it" and the chmod is what went missing.

Structural, not behavioural, and deliberately so: these two call sites live
behind a one-shot legacy-migration branch that is awkward to drive end-to-end,
and #2356's review point was that helper-only coverage cannot see call sites
that bypass the helper. So assert on the call sites themselves.
"""
from __future__ import annotations

import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# A temp that is os.replace()d into place must never be filled by a bare
# write_text(): that is the umask-created path.
for rel in ("src/discord-bridge.py", "src/slack-bridge.py", "src/telegram-bridge.py"):
    lines = (REPO / rel).read_text().splitlines()
    offenders = []
    for i, line in enumerate(lines[:-2]):
        if ".write_text(" in line and "write_private_text" not in line:
            following = "\n".join(lines[i + 1:i + 3])
            # Scoped to ACCESS_FILE: that is what this check is about. Two other
            # bridges promote a umask-created temp into WELCOMED_USERS_FILE and
            # OWNER_ACTIVITY_FILE — both PRE-EXISTING on main (verified) and a
            # different concern, so they are noted rather than gated here.
            if "os.replace" in following and "ACCESS_FILE" in following:
                offenders.append(f"{rel}:{i + 1} {line.strip()[:58]}")
    check(f"{rel}: no bare write_text feeds an os.replace into ACCESS_FILE",
          not offenders, "; ".join(offenders))

# Discord routes through access_store's shared locked writer; slack still
# has its own inline tmp/os.replace. Check each publisher on its actual path.
src = (REPO / "src/slack-bridge.py").read_text()
block = None
for m in re.finditer(r"tmp = ACCESS_FILE\.with_suffix\(.*?\n(.*?)os\.replace\(tmp, ACCESS_FILE\)",
                     src, re.S):
    if "grandfathered" in src[m.end():m.end() + 400]:
        block = m.group(1)
        break
check("src/slack-bridge.py: tier-map migration temp is written via write_private_text",
      block is not None and "write_private_text(tmp," in block,
      "block not found" if block is None else block.strip()[:70])

dsrc = (REPO / "src/discord-bridge.py").read_text()
check("src/discord-bridge.py: tier-map migration routes through access_store.mutate_access_file",
      "mutate_access_file(ACCESS_FILE, _mutator" in dsrc,
      "mutate_access_file call not found in ensure_tier_map_seeded")

# That shared owner must itself never promote a umask-created temp: the temp
# has to be os.open()'d 0600 (O_CREAT|O_EXCL) directly, not write_text()+chmod.
asrc = (REPO / "src/access_store.py").read_text()
m = re.search(r"def _atomic_write_owner_only\(.*?\n(.*?)\ndef ", asrc, re.S)
block = m.group(1) if m else None
check("src/access_store.py: _atomic_write_owner_only creates the temp born owner-only (no write_text)",
      block is not None and "os.O_CREAT" in block and "0o600" in block and ".write_text(" not in block,
      "function not found" if block is None else block.strip()[:120])

print()
if failures:
    print(f"FAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("PASS — tier-map migration publishers write owner-only temps")
