#!/usr/bin/env python3
"""The slack-bridge remedy must name every step the host actually needs.

"connected but events not arriving" is measured correctly, but the prescribed
fix — enable Event Subscriptions — is only the whole fix when an owner is
already enrolled. In TOFU state (`access.json` absent) `slack-bridge.py` also
gates enrollment behind a startup code, so following the remedy leaves Slack
dead while the operator believes it is fixed.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass

failures = []


def check(cond, label):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        failures.append(label)


# The log shape the probe keys on: a zero-event warning with nothing after it.
TAIL = ["[slack-bridge] connected", "[slack-bridge] 60s elapsed with zero events"]

enrolled = hc.bridge_log_content_status("slack-bridge", "ok", TAIL, slack_state="enrolled")
tofu = hc.bridge_log_content_status("slack-bridge", "ok", TAIL, slack_state="unconfigured")

check(enrolled is not None and enrolled[0] == "warn", "enrolled host still warns")
check(tofu is not None and tofu[0] == "warn", "TOFU host still warns (status unchanged)")

check("Event Subscriptions" in enrolled[1], "enrolled remedy names Event Subscriptions")
check("enrollment code" not in enrolled[1] and "DM the bot" not in enrolled[1],
      "enrolled remedy does NOT add a step that host does not need")

check("Event Subscriptions" in tofu[1], "TOFU remedy still names Event Subscriptions")
check("DM the bot" in tofu[1], "TOFU remedy names the enrollment step")
check("BOTH" in tofu[1], "TOFU remedy says both are required")
check("access.json absent" in tofu[1], "TOFU remedy states the evidence it branched on")
check(enrolled[1] != tofu[1], "the two hosts get materially different remedies")

# The third and fourth states: `.exists()` could not tell these from
# `enrolled`, so a locked-down workspace was told to enable Event Subscriptions.
locked = hc.bridge_log_content_status("slack-bridge", "ok", TAIL, slack_state="locked")
unknown = hc.bridge_log_content_status("slack-bridge", "ok", TAIL, slack_state="unknown")

check(locked is not None and locked[0] == "warn", "locked-down host still warns")
check("allows nobody" in locked[1], "locked remedy states the evidence it branched on")
check("add an allowed user id" in locked[1], "locked remedy names the step that unblocks it")
check("DM the bot" not in locked[1],
      "locked remedy does NOT tell the operator to use a TOFU code that is closed")
check(locked[1] != enrolled[1] and locked[1] != tofu[1],
      "locked gets its own remedy, not one of the other two")

# An unreadable record still must not manufacture an enrollment instruction —
# and Event Subscriptions alone cannot fix it either, so it gets its own remedy.
check(unknown is not None and unknown[0] == "warn", "unreadable record still warns")
check("DM the bot" not in unknown[1],
      "unreadable remedy does NOT invent a TOFU step (the original fail-safe)")
check(unknown[1] != enrolled[1],
      "and it is no longer the enrolled remedy, which leaves Slack silent here")
check("unreadable or malformed" in unknown[1],
      "unreadable remedy states the evidence it branched on")
check("allowFrom must be a list of user-id strings" in unknown[1],
      "and names the repair that actually unblocks it")
check(unknown[1] not in (locked[1], tofu[1]),
      "unreadable gets its own remedy, not one of the other three")

# A host with events flowing after the warning must not be flagged at all.
ok_tail = TAIL + ["[slack-bridge] Wrote task-abc.txt"]
check(hc.bridge_log_content_status("slack-bridge", "ok", ok_tail, slack_state="unconfigured") is None,
      "events after the warning -> no override, regardless of enrollment")

# Other bridges must be untouched by this branch.
check(hc.bridge_log_content_status("telegram-bridge", "ok", TAIL, slack_state="unconfigured") is None,
      "the slack branch does not fire for telegram")

# Default path: omitting the flag must still work. Patch the resolver so this
# case is hermetic — the ambient host config would otherwise decide the branch.
import tempfile
import pathlib
_orig_cap = hc.channel_access_path
with tempfile.TemporaryDirectory() as _td:
    hc.channel_access_path = lambda ch: pathlib.Path(_td) / ch / "access.json"
    try:
        r = hc.bridge_log_content_status("slack-bridge", "ok", TAIL)
    finally:
        hc.channel_access_path = _orig_cap
check(r is not None and r[0] == "warn" and "DM the bot" in r[1],
      "default (uninjected) call resolves enrollment itself: absent access.json -> TOFU remedy")

# The fail-safe: a resolver that RAISES must not fail the check, and must
# degrade to the enrolled remedy (the quieter of the two), never to TOFU.
def _raises(_ch):
    raise OSError("resolver unavailable")
hc.channel_access_path = _raises
try:
    r = hc.bridge_log_content_status("slack-bridge", "ok", TAIL)
finally:
    hc.channel_access_path = _orig_cap
check(r is not None and r[0] == "warn", "a raising resolver does not fail the check")
check("DM the bot" not in r[1],
      "a raising resolver degrades to the enrolled remedy, not the TOFU one")

# The remedy must name the log THIS check read, not a literal workspace path.
custom = pathlib.Path("/srv/some where/ws/logs/slack-bridge.log")
r = hc.bridge_log_content_status("slack-bridge", "ok", TAIL, slack_state="unconfigured",
                                 log_path=custom)
check("/srv/some where/ws/logs/slack-bridge.log" in r[1].replace("'", ""),
      "remedy names the RESOLVED log path on a custom workspace")
check("'/srv/some where/ws/logs/slack-bridge.log'" in r[1],
      "the path is shell-quoted, so a space cannot split the grep argument")
check("workspace/logs/slack-bridge.log" not in r[1].replace(
          "/srv/some where/ws/logs/slack-bridge.log", ""),
      "the hardcoded literal is gone")

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all slack-remedy assertions passed")
