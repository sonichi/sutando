#!/usr/bin/env python3
"""A policy-refused attachment must never be dropped silently.

`[file:]` markers have FOUR outcomes, and two of the Discord bridge's four
attachment loops named only two of them:

    if _is_path_sendable(fpath):   ...send...
    elif not os.path.isfile(fpath): ...log "file not found"...

A path that EXISTS but fails the allowlist matches neither branch, so the file
is dropped with no send, no log, and no exception. From the outside that is
byte-identical to a successful attach -- which is how an operator ends up
telling someone a file was delivered when authorization refused it.

Measured 2026-08-25 on the live bridge: a proactive channel-redirect send
logged `sent <name> to channel <id>` and nothing about the attachment either
way, so whether the file went was unknowable from the record.

The classification now lives once, beside the authorization predicate it
extends, in `policy.egress.attachment`.
"""
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
fail = 0


def check(cond, label):
    global fail
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        fail = 1


from policy.egress.attachment import (  # noqa: E402
    classify_attachment, ATTACH_SEND, ATTACH_EMPTY, ATTACH_MISSING, ATTACH_REFUSED)
from workspace_default import resolve_workspace  # noqa: E402

# --- behaviour: all four outcomes are distinguishable -------------------------
# notes/ is an allowed root; the workspace ROOT itself deliberately is not.
ws = pathlib.Path(resolve_workspace()) / "notes"
ws.mkdir(parents=True, exist_ok=True)
fd, inside = tempfile.mkstemp(suffix=".txt", dir=str(ws))
os.close(fd)
fd, outside = tempfile.mkstemp(suffix=".txt", dir="/tmp")
os.close(fd)
try:
    check(classify_attachment(inside)[0] == ATTACH_SEND,
          "a file under an allowed root (notes/) classifies SEND")
    # THE REGRESSION: exists, but not permitted. Must be its own outcome.
    check(classify_attachment(outside)[0] == ATTACH_REFUSED,
          "a file that EXISTS but fails the allowlist classifies REFUSED, not MISSING")
    check(classify_attachment("/tmp/definitely-absent-xyzzy-9182")[0] == ATTACH_MISSING,
          "an absent path classifies MISSING")
    check(classify_attachment("   ")[0] == ATTACH_EMPTY,
          "a blank marker value classifies EMPTY")
    # Control: the probe can produce a NEGATIVE. If SEND and REFUSED ever
    # collapse to one value this whole file passes by construction.
    check(ATTACH_SEND != ATTACH_REFUSED, "control: SEND and REFUSED are distinct values")
finally:
    os.unlink(inside)
    os.unlink(outside)

# --- wiring: neither redirect loop may keep the two-branch shape --------------
src = (REPO / "src" / "discord-bridge.py").read_text()
for tag in ("[proactive channel-redirect]", "[dm-fallback channel-redirect]"):
    check(src.find(tag) > 0, f"{tag} block is present")

# Completeness, not proximity: a window-based check fails when the code gets
# MORE correct, which is how the EMPTY branch broke this assertion.
for outcome in ("_ATTACH_SEND", "_ATTACH_MISSING", "_ATTACH_REFUSED", "_ATTACH_EMPTY"):
    check(src.count(outcome) >= 3,
          f"{outcome} is imported AND handled at both migrated loops "
          f"(found {src.count(outcome)}, want >=3)")

# An outcome used but never imported is a NameError the branch coverage hides.
import ast
tree = ast.parse(src)
imported = {a.asname or a.name for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) for a in n.names}
used = {x.id for x in ast.walk(tree)
        if isinstance(x, ast.Name) and x.id.startswith("_ATTACH_")}
check(not (used - imported),
      f"every _ATTACH_* name used is imported (unresolved: {sorted(used - imported)})")

print("\nRESULT:", "FAIL" if fail else "PASS")
sys.exit(fail)
