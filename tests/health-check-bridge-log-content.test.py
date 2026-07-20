#!/usr/bin/env python3
"""Tests for bridge_log_content_status() — slack-bridge "60s elapsed" false positive.

Health-check flagged slack-bridge as "connected but events not arriving" even
while the bridge was actively writing tasks from live Slack DMs. Root cause:
the "60s elapsed with zero events" hint fires once at startup (before the
first event ever arrives) and is never cleared from the log — a naive
substring scan over the log tail keeps finding it forever, even after dozens
of successful events. The fix only treats it as a live warning when no
"Wrote task-" line appears after it in the tail.

Run: python3 tests/health-check-bridge-log-content.test.py
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules["health_check"] = hc
spec.loader.exec_module(hc)

failures = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ── slack-bridge: startup hint with no events since → real warning ──────────

startup_only = [
    "Slack bridge started. Socket Mode connecting...",
    "⚡️ Bolt app is running!",
    "[Slack] HINT: 60s elapsed with zero events received.",
    "  Bridge is connected to Slack's edge, but events are not arriving.",
]
result = hc.bridge_log_content_status("slack-bridge", "ok", startup_only)
check("startup hint with no events after it → warns", result is not None and result[0] == "warn")

# ── slack-bridge: startup hint followed by real activity → false positive fixed ──

with_activity = startup_only + [
    "  Wrote task-1784553750056 from Slack DM @Bassil Khilo",
    "  Replied to D0B5L7X2TK2: ...",
    "  Wrote task-1784555474341 from Slack DM @Bassil Khilo",
    "  Replied to D0B5L7X2TK2: ...",
]
result2 = hc.bridge_log_content_status("slack-bridge", "ok", with_activity)
check("startup hint but events arrived afterward → no override (stays ok)", result2 is None)

# ── slack-bridge: no hint at all → no override ──────────────────────────────

clean = ["Slack bridge started. Socket Mode connecting...", "⚡️ Bolt app is running!"]
result3 = hc.bridge_log_content_status("slack-bridge", "ok", clean)
check("no hint in log → no override", result3 is None)

# ── slack-bridge: only overrides when incoming status is "ok" ──────────────

result4 = hc.bridge_log_content_status("slack-bridge", "warn", startup_only)
check("status already non-ok (e.g. stale) → does not override", result4 is None)

# ── slack-bridge: a second startup hint after activity (bridge restarted) ──
# re-warns because there's no "Wrote task-" line after the LAST hint occurrence.

restarted = with_activity + ["[Slack] HINT: 60s elapsed with zero events received."]
result5 = hc.bridge_log_content_status("slack-bridge", "ok", restarted)
check("bridge restarted (fresh hint after old activity) → warns again", result5 is not None and result5[0] == "warn")

# ── discord-bridge: LoginFailure always overrides regardless of status ──────

login_failure = ["discord.errors.LoginFailure: Improper token has been passed."]
result6 = hc.bridge_log_content_status("discord-bridge", "ok", login_failure)
check("discord LoginFailure → fail status", result6 is not None and result6[0] == "fail")

result7 = hc.bridge_log_content_status("discord-bridge", "warn", login_failure)
check("discord LoginFailure overrides even non-ok incoming status", result7 is not None and result7[0] == "fail")

# ── discord-bridge: healthy log → no override ───────────────────────────────

result8 = hc.bridge_log_content_status("discord-bridge", "ok", ["Logged in as SutandoBot#1234"])
check("discord healthy log → no override", result8 is None)

if failures:
    sys.exit(1)
print("PASS — bridge_log_content_status tests")
