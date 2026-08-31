#!/usr/bin/env python3
"""An empty `[channel:]` target must take the DEFAULT route, end to end.

This PR makes `[channel:]` parse like `[channel: ]` (its spaced twin). Emitting
a redirect action with value "" composed two individually-deliberate policies
into a release loop: `body_claimable_by` keeps a malformed target claimable
(deliver > strand), while the default sink's `redirect_target_is_foreign("")`
refuses anything not POSITIVELY its own — so Discord claimed the file, released
it for an "own bridge" that does not exist, and re-claimed it next poll,
forever. Slack/Telegram never claim a default-Discord file, and `int("")`
raised in both Discord conversion sites had the release not happened first.

The pinned semantics: an empty target is NOT a target. The recognised marker
is stripped, NO redirect action is emitted, and the body delivers on the
default route. The foreign gate and the int() conversions become unreachable
for this input BY CONSTRUCTION — asserted here through the parser, the claim
policy, and the shape of the Discord sink gate, not the parser alone.
"""
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "discord-bridge.py"
fail = 0


def check(cond, label):
    global fail
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        fail = 1


sys.path.insert(0, str(REPO / "src"))
from result_markers import parse_markers  # noqa: E402
from proactive_routing import (  # noqa: E402
    body_claimable_by, body_target_channel, redirect_target_is_foreign)

# --- 1. Parser: both twins strip the marker and emit NO redirect action. ---
for marker in ("[channel:]", "[channel: ]", "[channel:  ]"):
    r = parse_markers(f"{marker}\nthe briefing body")
    check(all(a.kind != "redirect" for a in r.actions),
          f"{marker!r} emits no redirect action")
    check("channel" not in r.body and "the briefing body" in r.body,
          f"{marker!r} is stripped and the body survives")

# Preservation: a real target still redirects (the PR's feature is untouched).
r = parse_markers("[channel: 1485653767402553457]\nbody")
check(any(a.kind == "redirect" and a.value == "1485653767402553457"
          for a in r.actions),
      "a real numeric target still emits its redirect action")

# dm-only + empty marker: still no redirect, privacy action intact.
r = parse_markers("[dm-only]\n[channel:]\nbody")
kinds = [a.kind for a in r.actions]
check("dm-only" in kinds and "redirect" not in kinds,
      "dm-only + empty marker yields dm-only only")

# --- 2. Claim policy: the empty-marker body stays on the default route. ---
check(body_target_channel("[channel:]\nbody") is None,
      "router classifies the empty-marker body as no-redirect")
check(body_claimable_by("[channel:]\nbody", "discord"),
      "the default bridge still claims it (deliver > strand)")

# --- 3. The sink gate stays strict AND unreachable for this input. ---
# The gate's strictness is WHY the parser must not emit "": document it.
check(redirect_target_is_foreign("", "discord"),
      "the foreign gate itself still refuses an empty value (strict form kept)")
# The Discord gate only fires on a present action; pin that None-guard, since
# dropping it would re-expose the loop the moment any classifier returns None.
text = SRC.read_text()
start = text.find('if f.name.startswith("proactive-")')
end = text.find("[proactive] send failure", start)
check(start != -1 and end > start, "proactive claim block is locatable")
block = text[start:end]
check("_early_redirect is not None and redirect_target_is_foreign(" in block,
      "the sink's foreign gate is guarded on a PRESENT redirect action")

# --- 4. int("") is unreachable: no action carries an empty value. ---
for body in ("[channel:]\nbody", "[channel: ]\nbody",
             "[dm-only]\n[channel:]\nbody"):
    acts = parse_markers(body).actions
    check(all(a.value != "" or a.kind == "dm-only" for a in acts),
          f"no empty-valued routable action escapes the parser for {body.split(chr(10))[0]!r}")

sys.exit(fail)
