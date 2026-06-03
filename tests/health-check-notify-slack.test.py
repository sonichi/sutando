#!/usr/bin/env python3
"""
Tests for the remote Slack-DM health watchdog (`notify_slack_for_failures`
and `_slack_failures`) in src/health-check.py.

Motivated by the 2026-06-02 incident: the core session wedged on the
1M-context usage-credit API error and looped silently. The core heartbeat
process kept ticking, so `_any_core_alive()` stayed True and the existing
emit-task surface stayed quiet; Slack just went dark. This watchdog DMs the
owner directly — a remote surface that does NOT depend on the core agent and
is NOT gated on core liveness.

Covers:
  a) all-ok input            → no send, no state file
  b) on-demand-only warns    → no send (benign steady state, would spam)
  c) filter keeps stuck-loop warn + hard-down, drops on-demand warn
  d) first call              → sends, records dedup state
  e) same set < 1h           → cooldown suppresses duplicate send
  f) different set           → new hash, new send
  g) failed send             → dedup NOT recorded (retries next tick)
  h) 24h pruning             → history file doesn't grow unboundedly

Run: python3 tests/health-check-notify-slack.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


def make_checks(*statuses_names_details):
    """[(status, name, detail), ...] → list of check dicts."""
    return [{"name": n, "status": s, "detail": d} for (s, n, d) in statuses_names_details]


def recording_sender():
    sent: list[str] = []
    return sent, (lambda text: (sent.append(text) or True))


def failing_sender(text):
    return False


def case_a_all_ok_no_send() -> list[str]:
    fails = []
    sent, send = recording_sender()
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "slack.json"
        hc.notify_slack_for_failures(
            make_checks(("ok", "svcA", "port 1"), ("ok", "svcB", "port 2")),
            state_file=state, sender=send,
        )
        if sent:
            fails.append("a) all-ok input triggered a Slack DM")
        if state.exists():
            fails.append("a) all-ok input wrote dedup state")
    return fails


def case_b_on_demand_only_no_send() -> list[str]:
    fails = []
    sent, send = recording_sender()
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "slack.json"
        hc.notify_slack_for_failures(
            make_checks(
                ("warn", "discord-voice", "not running (on-demand)"),
                ("warn", "conversation-server", "not running (on-demand)"),
            ),
            state_file=state, sender=send,
        )
        if sent:
            fails.append("b) on-demand-only warns triggered a Slack DM (spam risk)")
    return fails


def case_c_filter_keeps_actionable() -> list[str]:
    fails = []
    checks = make_checks(
        ("warn", "core-proactive-loop", "running for 900s on 'triage' — last heartbeat > 600s ago"),
        ("warn", "discord-voice", "not running (on-demand)"),
        ("warn", "task-queue", "5 tasks queued, oldest 700s — watcher or core may be stuck"),
        ("down", "voice-agent", "port 9900"),
        ("ok", "web-client", "port 3100"),
    )
    got = sorted(c["name"] for c in hc._slack_failures(checks))
    want = ["core-proactive-loop", "task-queue", "voice-agent"]
    if got != want:
        fails.append(f"c) filter returned {got}, want {want}")
    return fails


def case_d_first_send_records_state() -> list[str]:
    fails = []
    sent, send = recording_sender()
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "slack.json"
        hc.notify_slack_for_failures(
            make_checks(("down", "voice-agent", "port 9900")),
            state_file=state, sender=send,
        )
        if len(sent) != 1:
            fails.append(f"d) first call should send once, sent {len(sent)}")
        if not state.exists():
            fails.append("d) first successful send did not record dedup state")
    return fails


def case_e_cooldown_suppresses() -> list[str]:
    fails = []
    sent, send = recording_sender()
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "slack.json"
        checks = make_checks(("down", "voice-agent", "port 9900"))
        hc.notify_slack_for_failures(checks, state_file=state, sender=send)
        hc.notify_slack_for_failures(checks, state_file=state, sender=send)
        if len(sent) != 1:
            fails.append(f"e) within-cooldown second call sent again (total {len(sent)})")
    return fails


def case_f_different_set_sends() -> list[str]:
    fails = []
    sent, send = recording_sender()
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "slack.json"
        hc.notify_slack_for_failures(
            make_checks(("down", "voice-agent", "x")), state_file=state, sender=send)
        hc.notify_slack_for_failures(
            make_checks(("down", "discord-bridge", "x"), ("warn", "task-queue", "stuck")),
            state_file=state, sender=send)
        if len(sent) != 2:
            fails.append(f"f) two distinct sets should send twice, sent {len(sent)}")
    return fails


def case_g_failed_send_not_recorded() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "slack.json"
        hc.notify_slack_for_failures(
            make_checks(("down", "voice-agent", "x")),
            state_file=state, sender=failing_sender,
        )
        if state.exists():
            fails.append("g) failed send recorded dedup state — would suppress retry for 1h")
        # And a subsequent successful send for the same set must go through.
        sent, send = recording_sender()
        hc.notify_slack_for_failures(
            make_checks(("down", "voice-agent", "x")),
            state_file=state, sender=send,
        )
        if len(sent) != 1:
            fails.append("g) retry after a failed send did not go through")
    return fails


def case_h_history_pruned_after_24h() -> list[str]:
    fails = []
    sent, send = recording_sender()
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "slack.json"
        now_ms = int(time.time() * 1000)
        state.write_text(json.dumps({
            "stale_hash_aaaaaaaa": now_ms - (25 * 3600 * 1000),
            "fresh_hash_bbbbbbbb": now_ms - (10 * 60 * 1000),
        }))
        hc.notify_slack_for_failures(
            make_checks(("down", "new-failure", "x")),
            state_file=state, sender=send,
        )
        history = json.loads(state.read_text())
        if "stale_hash_aaaaaaaa" in history:
            fails.append("h) 25h-old entry was not pruned")
        if "fresh_hash_bbbbbbbb" not in history:
            fails.append("h) 10min-old entry was wrongly pruned")
    return fails


def main() -> int:
    cases = [
        ("a", case_a_all_ok_no_send),
        ("b", case_b_on_demand_only_no_send),
        ("c", case_c_filter_keeps_actionable),
        ("d", case_d_first_send_records_state),
        ("e", case_e_cooldown_suppresses),
        ("f", case_f_different_set_sends),
        ("g", case_g_failed_send_not_recorded),
        ("h", case_h_history_pruned_after_24h),
    ]
    all_failures = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            all_failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    print("\nAll Slack-watchdog invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
