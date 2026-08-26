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
    i = src.find(tag)
    check(i > 0, f"{tag} block is present")
    # Window around the block's attachment loop.
    j = max(0, i - 2500)
    window = src[j:i + 2500]
    check("_ATTACH_REFUSED" in window,
          f"{tag} handles the REFUSED outcome explicitly")

check(src.count("_ATTACH_REFUSED") >= 2,
      "both redirect loops route through the shared classifier")

print("\nRESULT:", "FAIL" if fail else "PASS")
sys.exit(fail)
