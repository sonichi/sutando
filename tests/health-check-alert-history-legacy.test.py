#!/usr/bin/env python3
"""
Tests that a failure-alert dedup file this build cannot interpret degrades to
"no dedup history" instead of raising.

An older build stored `{hash: {"last": ms, "streak": n}}` per entry. The
current schema stores `{hash: ms}`, and the 24h pruning compares each value
against an int cutoff — so a file left by the older build raised
`TypeError: '>=' not supported between instances of 'dict' and 'int'`. The
raise escapes into main(), which kills the entire health check, including the
launchd fallback whose whole purpose is to alert when the rest of Sutando is
down. Worse, the send happens BEFORE the prune, so each 300s tick re-sent the
same alert and never recorded the dedup.

Covers:
  a) legacy-schema file      → every alert path completes and rewrites state
  b) legacy-schema file      → alert sends ONCE, not once per tick
  c) non-dict top level      → treated as empty, no raise
  d) unreadable / absent     → treated as empty, no raise
  e) a valid current file    → still honoured (the loader is not a wipe)
  f) `_last_hash` of the wrong type → dropped, not returned as a hash

Run: python3 tests/health-check-alert-history-legacy.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

LEGACY = {"7e6c08241b7e0100": {"last": 1787699677257, "streak": 1},
          "c09f9d326c61e4b1": {"last": 1787913689309, "streak": 2}}
DOWN = [{"name": "slack-bridge", "status": "down", "detail": "port closed"}]


def alert_paths(tmp: Path):
    """(label, callable taking a state_file) for every path that dedups."""
    return [
        ("emit_task", lambda p: hc.emit_task_for_failures(DOWN, state_file=p, tasks_dir=tmp / "tasks")),
        ("notify", lambda p: hc.notify_for_failures(DOWN, state_file=p, notify_cmd=["true"])),
        ("slack", lambda p: hc.notify_slack_for_failures(DOWN, state_file=p, sender=lambda t: True)),
        ("gateway", lambda p: hc.notify_gateway_for_failures(DOWN, state_file=p, sender=lambda t: True)),
    ]


def case_a_legacy_file_does_not_raise() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for label, fn in alert_paths(tmp):
            state = tmp / f"{label}.json"
            state.write_text(json.dumps(LEGACY))
            try:
                fn(state)
            except Exception as e:
                fails.append(f"a) {label} raised {type(e).__name__}: {e}")
                continue
            try:
                after = json.loads(state.read_text())
            except Exception as e:
                fails.append(f"a) {label} left unreadable state: {e}")
                continue
            if hc._LAST_HASH_KEY not in after:
                fails.append(f"a) {label} completed but never recorded dedup state")
            if any(isinstance(v, dict) for v in after.values()):
                fails.append(f"a) {label} carried a legacy entry forward")
    return fails


def case_b_legacy_file_alerts_once_not_per_tick() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "slack.json"
        state.write_text(json.dumps(LEGACY))
        sent: list[str] = []
        for _ in range(3):
            hc.notify_slack_for_failures(DOWN, state_file=state,
                                         sender=lambda t: (sent.append(t) or True))
        if len(sent) != 1:
            fails.append(f"b) three ticks over a legacy file sent {len(sent)} alerts, expected 1")
    return fails


def case_c_non_dict_top_level() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        for i, payload in enumerate(["[]", '"a string"', "3", "null"]):
            state = Path(td) / f"c{i}.json"
            state.write_text(payload)
            try:
                got = hc._load_alert_history(state)
            except Exception as e:
                fails.append(f"c) {payload} raised {type(e).__name__}: {e}")
                continue
            if got != {}:
                fails.append(f"c) {payload} loaded as {got!r}, expected {{}}")
    return fails


def case_d_absent_and_unparseable() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "nope.json"
        if hc._load_alert_history(missing) != {}:
            fails.append("d) an absent file did not load as empty")
        broken = Path(td) / "broken.json"
        broken.write_text("{not json")
        if hc._load_alert_history(broken) != {}:
            fails.append("d) an unparseable file did not load as empty")
    return fails


def case_e_current_schema_survives() -> list[str]:
    """The loader must not be a wipe — a good file has to come back intact."""
    fails = []
    good = {"aaaaaaaaaaaaaaaa": 1788000000000, hc._LAST_HASH_KEY: "aaaaaaaaaaaaaaaa"}
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "good.json"
        state.write_text(json.dumps(good))
        got = hc._load_alert_history(state)
        if got != good:
            fails.append(f"e) a valid file loaded as {got!r}, expected {good!r}")
        # and the dedup it encodes must still suppress
        sent: list[str] = []
        hc.notify_slack_for_failures(DOWN, state_file=state,
                                     sender=lambda t: (sent.append(t) or True))
        hash_key = hc.hashlib.sha256(b"slack-bridge").hexdigest()[:16]
        state.write_text(json.dumps({hash_key: 1788000000000, hc._LAST_HASH_KEY: hash_key}))
        sent.clear()
        hc.notify_slack_for_failures(DOWN, state_file=state,
                                     sender=lambda t: (sent.append(t) or True))
        if sent:
            fails.append("e) a recorded dedup no longer suppresses the repeat send")
    return fails


def case_f_last_hash_of_wrong_type_is_dropped() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "f.json"
        state.write_text(json.dumps({hc._LAST_HASH_KEY: {"was": "a dict"}}))
        got = hc._load_alert_history(state)
        if hc._LAST_HASH_KEY in got:
            fails.append(f"f) a non-string _last_hash survived as {got[hc._LAST_HASH_KEY]!r}")
    return fails


def main() -> int:
    cases = [
        ("a", case_a_legacy_file_does_not_raise),
        ("b", case_b_legacy_file_alerts_once_not_per_tick),
        ("c", case_c_non_dict_top_level),
        ("d", case_d_absent_and_unparseable),
        ("e", case_e_current_schema_survives),
        ("f", case_f_last_hash_of_wrong_type_is_dropped),
    ]
    all_failures: list[str] = []
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
    print("\nAn uninterpretable alert-history file degrades, it does not raise.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
