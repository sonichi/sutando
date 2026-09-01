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
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

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

# ── slack-bridge: the remedy RANKS causes, it does not assert one (#3230) ──────
# slack_state injected: uninjected resolves the host's access.json, so CI differs.
_enrolled = hc.bridge_log_content_status("slack-bridge", "ok", startup_only,
                                         slack_state="enrolled")
msg = _enrolled[1] if _enrolled else ""
check("remedy states what was measured, not a presumed cause",
      "since this bridge started" in msg, msg)
check("remedy offers the benign restart explanation first",
      "restarted" in msg, msg)
check("remedy still names the config cause as the conditional one",
      "Event Subscriptions" in msg, msg)
check("remedy does not read as a bare config imperative",
      not msg.startswith("connected but events not arriving — enable"), msg)

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

# ── run_all_checks() integration: exercise the actual call site ─────────────
# The unit tests above cover bridge_log_content_status() in isolation, but the
# call site inside run_all_checks() (fetching `tail`, invoking the function,
# applying the override to `status`/`detail`) is separate code that needs its
# own coverage. Fakes just the slack-bridge pgrep result (a real PID would
# make Check 4/5's ps/lsof calls meaningfully diverge; a fake one just makes
# them no-op safely, which is what we want here) and points WORKSPACE_DIR /
# claude_home_path at a temp tree so the log content is fully controlled.

_orig_subprocess_run = subprocess.run


def _fake_pgrep_slack(cmd, *args, **kwargs):
    if isinstance(cmd, list) and len(cmd) >= 3 and cmd[0] == "/usr/bin/pgrep" and "slack-bridge" in cmd[2]:
        class _Result:
            returncode = 0
            stdout = "999999\n"
        return _Result()
    return _orig_subprocess_run(cmd, *args, **kwargs)


def _run_all_checks_with_slack_log(log_contents: str) -> "dict | None":
    with tempfile.TemporaryDirectory() as tmpws, tempfile.TemporaryDirectory() as tmphome:
        tmpws = Path(tmpws)
        (tmpws / "logs").mkdir(parents=True)
        (tmpws / "logs" / "slack-bridge.log").write_text(log_contents)
        channel_dir = Path(tmphome) / "channels" / "slack"
        channel_dir.mkdir(parents=True)
        (channel_dir / ".env").write_text("SLACK_BOT_TOKEN=xoxb-test\n")

        _orig_chp = hc.claude_home_path

        def _fake_chp(*sub):
            if sub and sub[0] == "channels":
                return Path(tmphome).joinpath(*sub)
            return _orig_chp(*sub)

        for _k in ("SKIP_TELEGRAM", "SKIP_DISCORD", "SKIP_SLACK"):
            os.environ.pop(_k, None)

        with patch.object(hc, "WORKSPACE_DIR", tmpws), \
             patch.object(hc, "claude_home_path", side_effect=_fake_chp), \
             patch.object(subprocess, "run", side_effect=_fake_pgrep_slack):
            checks = hc.run_all_checks()
        return next((c for c in checks if c["name"] == "slack-bridge"), None)


_stale_hint_only = (
    "Slack bridge started. Socket Mode connecting...\n"
    "⚡️ Bolt app is running!\n"
    "[Slack] HINT: 60s elapsed with zero events received.\n"
)
_hint_with_activity = _stale_hint_only + "  Wrote task-1784555474341 from Slack DM @Bassil Khilo\n"

_check_warn = _run_all_checks_with_slack_log(_stale_hint_only)
check("run_all_checks: hint-only log → slack-bridge warns",
      _check_warn is not None and _check_warn["status"] == "warn", str(_check_warn))

_check_ok = _run_all_checks_with_slack_log(_hint_with_activity)
check("run_all_checks: hint + later activity → slack-bridge stays ok (the fix)",
      _check_ok is not None and _check_ok["status"] == "ok", str(_check_ok))

if failures:
    sys.exit(1)
print("PASS — bridge_log_content_status tests")
