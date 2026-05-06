#!/usr/bin/env python3
"""Unit tests for src/learn-mining.py — the deterministic primitives the
CURATE skill orchestrates. Each primitive is tested in isolation per
`feedback_unit_test_copied_helpers.md`.

Same shape as `tests/health-check-emit-task.test.py` (PR #610): standalone
script, exits 0 on pass, no test runner dependency. Run via:

    python3 tests/curate-mining.test.py

CI picks it up via `npm test` (PR #611 wires `tests/*.test.py`).
"""

from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Load src/learn-mining.py as `lm` (filename has a hyphen, can't import directly).
spec = importlib.util.spec_from_file_location("learn_mining", REPO / "src" / "learn-mining.py")
lm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lm)


# ---------------------------------------------------------------------------
# Cursor management


def case_a_cursor_missing_returns_empty() -> list[str]:
    """load_cursor on missing file returns {}, not error."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "nope.json"
        result = lm.load_cursor(p)
        if result != {}:
            fails.append(f"a) missing file should return empty dict, got {result!r}")
    return fails


def case_a_cursor_corrupt_returns_empty() -> list[str]:
    """load_cursor on corrupt JSON returns {}, doesn't crash."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "corrupt.json"
        p.write_text("not valid json {{{")
        result = lm.load_cursor(p)
        if result != {}:
            fails.append(f"a-corrupt) corrupt JSON should return empty dict, got {result!r}")
    return fails


def case_a_cursor_save_atomic() -> list[str]:
    """save_cursor + load_cursor round-trips. tmp file is cleaned up."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sub" / "cursor.json"  # nested dir to test mkdir
        lm.save_cursor(p, {"foo": 42, "bar": "baz"})
        result = lm.load_cursor(p)
        if result != {"foo": 42, "bar": "baz"}:
            fails.append(f"a-save) round-trip mismatch: {result!r}")
        # tmp file should be cleaned up after replace()
        if (Path(td) / "sub" / "cursor.json.tmp").exists():
            fails.append("a-save) tmp file not cleaned up after replace")
    return fails


# ---------------------------------------------------------------------------
# mine_dir


def case_b_mine_dir_empty_no_events() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        cursor = {}
        events = lm.mine_dir(Path(td), "tasks", cursor)
        if events:
            fails.append(f"b-empty) empty dir produced {len(events)} events, expected 0")
        if "tasks" in cursor:
            fails.append("b-empty) empty dir advanced cursor")
    return fails


def case_b_mine_dir_one_new_file_one_event() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        (td_p / "task-1.txt").write_text("hello")
        cursor = {}
        events = lm.mine_dir(td_p, "tasks", cursor)
        if len(events) != 1:
            fails.append(f"b-one) expected 1 event, got {len(events)}")
        if "tasks" not in cursor:
            fails.append("b-one) cursor not advanced")
    return fails


def case_b_mine_dir_cursor_advances_no_replay() -> list[str]:
    """Files seen in pass 1 don't re-emit in pass 2."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        (td_p / "task-1.txt").write_text("a")
        cursor = {}
        first = lm.mine_dir(td_p, "tasks", cursor)
        # Second pass — no new files, cursor advanced to first pass max.
        second = lm.mine_dir(td_p, "tasks", cursor)
        if len(first) != 1 or len(second) != 0:
            fails.append(f"b-noreplay) first={len(first)} second={len(second)}, expected 1/0")
    return fails


# ---------------------------------------------------------------------------
# mine_log


def case_c_mine_log_empty_no_events() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "voice-agent.log"
        log.write_text("")
        events = lm.mine_log(log, {})
        if events:
            fails.append(f"c-empty) empty log produced {len(events)} events")
    return fails


def case_c_mine_log_match_emits_event() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "voice-agent.log"
        log.write_text(
            "ok normal line\n"
            "[VoiceSession] transport connected and setup complete\n"
            "more normal\n"
            "ERROR: something broke\n"
        )
        events = lm.mine_log(log, {})
        # Should emit 1 transport + 1 error = 2 events
        kinds = sorted(e["kind"] for e in events)
        if kinds != ["error", "transport"]:
            fails.append(f"c-match) expected ['error','transport'], got {kinds}")
    return fails


def case_c_mine_log_cursor_no_replay() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "voice-agent.log"
        log.write_text("ERROR x\n")
        cursor = {}
        first = lm.mine_log(log, cursor)
        second = lm.mine_log(log, cursor)
        if len(first) != 1 or len(second) != 0:
            fails.append(f"c-cursor) first={len(first)} second={len(second)}, expected 1/0")
    return fails


def case_c_mine_log_rotation_detected() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "voice-agent.log"
        log.write_text("ERROR x\nERROR y\n")
        cursor = {}
        lm.mine_log(log, cursor)  # advance cursor to EOF
        # Now truncate the log (rotation)
        log.write_text("ERROR z\n")
        events = lm.mine_log(log, cursor)
        kinds = [e["kind"] for e in events]
        if "rotated" not in kinds:
            fails.append(f"c-rotated) no rotated event after truncation; got {kinds}")
        if "error" not in kinds:
            fails.append(f"c-rotated) no error event for new content; got {kinds}")
    return fails


def case_c_mine_log_no_filters_match_no_events() -> list[str]:
    """Log file whose name doesn't match any filter suffix → no events."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "unrelated.log"
        log.write_text("ERROR something\n")
        events = lm.mine_log(log, {})
        if events:
            fails.append(f"c-nofilter) unmatched log filename emitted {len(events)} events")
    return fails


# ---------------------------------------------------------------------------
# mine_jsonl


def case_d_mine_jsonl_emits_per_line() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        jsonl = Path(td) / "session.jsonl"
        jsonl.write_text('{"a":1}\n{"b":2}\n{"c":3}\n')
        events = lm.mine_jsonl(jsonl, {})
        if len(events) != 3:
            fails.append(f"d) expected 3 events, got {len(events)}")
    return fails


def case_d_mine_jsonl_cursor_subkey_per_uuid() -> list[str]:
    """Per-UUID cursor (gap #3 resolution)."""
    fails = []
    with tempfile.TemporaryDirectory() as td:
        jsonl = Path(td) / "uuid1.jsonl"
        jsonl.write_text('{"a":1}\n{"b":2}\n')
        cursor = {}
        first = lm.mine_jsonl(jsonl, cursor, cursor_subkey="uuid1")
        # cursor should be nested
        abs_key = str(jsonl.resolve())
        if not isinstance(cursor.get(abs_key), dict):
            fails.append(f"d-sub) cursor not nested: {cursor!r}")
        elif "uuid1" not in cursor[abs_key]:
            fails.append(f"d-sub) subkey missing: {cursor!r}")
        # Second pass should be empty
        second = lm.mine_jsonl(jsonl, cursor, cursor_subkey="uuid1")
        if len(second) != 0:
            fails.append(f"d-sub) replay on cursored read: {len(second)}")
    return fails


# ---------------------------------------------------------------------------
# audit_memory_pointers


def case_e_audit_pointers_clean() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        (td_p / "feedback_x.md").write_text("body")
        (td_p / "MEMORY.md").write_text("- [feedback_x](feedback_x.md) — desc\n")
        findings = lm.audit_memory_pointers(td_p)
        if findings:
            fails.append(f"e-clean) clean dir produced {len(findings)} findings: {findings}")
    return fails


def case_e_audit_pointers_broken() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        (td_p / "MEMORY.md").write_text("- [missing](missing.md) — desc\n")
        findings = lm.audit_memory_pointers(td_p)
        kinds = [f["kind"] for f in findings]
        if "broken-pointer" not in kinds:
            fails.append(f"e-broken) expected broken-pointer, got {kinds}")
    return fails


def case_e_audit_pointers_orphan() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        (td_p / "feedback_orphan.md").write_text("body")
        (td_p / "MEMORY.md").write_text("# index\n")  # nothing referenced
        findings = lm.audit_memory_pointers(td_p)
        kinds = [f["kind"] for f in findings]
        if "orphan-file" not in kinds:
            fails.append(f"e-orphan) expected orphan-file, got {kinds}")
    return fails


# ---------------------------------------------------------------------------
# audit_notes_staleness


def case_f_audit_notes_threshold() -> list[str]:
    fails = []
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        fresh = td_p / "fresh.md"
        stale = td_p / "stale.md"
        fresh.write_text("a")
        stale.write_text("b")
        # Backdate stale's mtime to 60 days ago.
        old = time.time() - (60 * 86400)
        import os as _os
        _os.utime(stale, (old, old))
        findings = lm.audit_notes_staleness(td_p, threshold_days=30)
        names = [Path(f["data"]["path"]).name for f in findings]
        if "stale.md" not in names:
            fails.append(f"f) expected stale.md flagged, got {names}")
        if "fresh.md" in names:
            fails.append(f"f) fresh.md should not be flagged, got {names}")
    return fails


# ---------------------------------------------------------------------------
# Cooldown


def case_g_cooldown_within_window_suppresses() -> list[str]:
    fails = []
    state = {}
    h = lm.hash_finding(["correction", "voice-agent.log", "user said no"])
    # First call — not in cooldown, registers.
    if lm.dedup_against_cooldown(h, state, ttl_h=6.0, now_s=1000.0):
        fails.append("g-first) first call should NOT be deduped")
    # Second call within window.
    if not lm.dedup_against_cooldown(h, state, ttl_h=6.0, now_s=1000.0 + 100):
        fails.append("g-second) second call within TTL should be deduped")
    # Third call past TTL.
    if lm.dedup_against_cooldown(h, state, ttl_h=6.0, now_s=1000.0 + 7 * 3600):
        fails.append("g-past) call past TTL should NOT be deduped")
    return fails


def case_g_prune_cooldown() -> list[str]:
    fails = []
    state = {
        "fresh": time.time() - 3600,       # 1h old, kept
        "stale": time.time() - 100 * 3600, # 100h old, dropped
    }
    pruned = lm.prune_cooldown(state, max_age_h=24.0)
    if pruned != 1:
        fails.append(f"g-prune) expected 1 pruned, got {pruned}")
    if "stale" in state:
        fails.append("g-prune) stale entry not removed")
    if "fresh" not in state:
        fails.append("g-prune) fresh entry wrongly removed")
    return fails


# ---------------------------------------------------------------------------
# Driver


def main() -> int:
    cases = [
        ("a-missing", case_a_cursor_missing_returns_empty),
        ("a-corrupt", case_a_cursor_corrupt_returns_empty),
        ("a-save",    case_a_cursor_save_atomic),
        ("b-empty",   case_b_mine_dir_empty_no_events),
        ("b-one",     case_b_mine_dir_one_new_file_one_event),
        ("b-noreplay", case_b_mine_dir_cursor_advances_no_replay),
        ("c-empty",   case_c_mine_log_empty_no_events),
        ("c-match",   case_c_mine_log_match_emits_event),
        ("c-cursor",  case_c_mine_log_cursor_no_replay),
        ("c-rotated", case_c_mine_log_rotation_detected),
        ("c-nofilter", case_c_mine_log_no_filters_match_no_events),
        ("d-emit",    case_d_mine_jsonl_emits_per_line),
        ("d-sub",     case_d_mine_jsonl_cursor_subkey_per_uuid),
        ("e-clean",   case_e_audit_pointers_clean),
        ("e-broken",  case_e_audit_pointers_broken),
        ("e-orphan",  case_e_audit_pointers_orphan),
        ("f",         case_f_audit_notes_threshold),
        ("g-cooldown", case_g_cooldown_within_window_suppresses),
        ("g-prune",   case_g_prune_cooldown),
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
    print(f"\nAll {len(cases)} curate-mining primitives pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
