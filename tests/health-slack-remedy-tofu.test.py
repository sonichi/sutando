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

enrolled = hc.bridge_log_content_status("slack-bridge", "ok", TAIL, slack_enrolled=True)
tofu = hc.bridge_log_content_status("slack-bridge", "ok", TAIL, slack_enrolled=False)

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

# A host with events flowing after the warning must not be flagged at all.
ok_tail = TAIL + ["[slack-bridge] Wrote task-abc.txt"]
check(hc.bridge_log_content_status("slack-bridge", "ok", ok_tail, slack_enrolled=False) is None,
      "events after the warning -> no override, regardless of enrollment")

# Other bridges must be untouched by this branch.
check(hc.bridge_log_content_status("telegram-bridge", "ok", TAIL, slack_enrolled=False) is None,
      "the slack branch does not fire for telegram")

# Default path: omitting the flag must still work (it reads the real resolver).
r = hc.bridge_log_content_status("slack-bridge", "ok", TAIL)
check(r is not None and r[0] == "warn" and "Event Subscriptions" in r[1],
      "default (uninjected) call still returns a warn with a remedy")

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("all slack-remedy assertions passed")
